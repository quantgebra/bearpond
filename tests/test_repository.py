from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.server import config
from bearpond.server import metadata_store
from bearpond.server import repository
from bearpond.server import server as server_module
from bearpond.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin(
    repo: repository.Repository,
    added: list[types.FileMetadata] | None = None,
    removed: list[types.FileMetadata] | None = None,
) -> transaction.Transaction:
    return repo.begin_transaction(types.TransactionManifest(added=added or [], removed=removed or []))


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_empty_manifest(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="at least one file"):
        begin(repo)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_csv_in_hive_path(repo: repository.Repository) -> None:
    # hive layout is recommended for subset pulls but no longer required
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("year=2024/data.csv", CONTENT)])
    assert txn.added["year=2024/data.csv"].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_arbitrary_path(repo: repository.Repository) -> None:
    # paths are no longer required to follow hive-style key=value directories
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("trades/2024-08-22/executions.json", CONTENT)])
    assert txn.added["trades/2024-08-22/executions.json"].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_top_level_file(repo: repository.Repository) -> None:
    # a bare filename is also valid
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("part.csv", CONTENT)])
    assert txn.added["part.csv"].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_duplicate_added_path(repo: repository.Repository) -> None:
    meta: types.FileMetadata = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="more than once"):
        begin(repo, added=[meta, meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_duplicate_removed_path(repo: repository.Repository) -> None:
    meta: types.FileMetadata = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="more than once"):
        begin(repo, removed=[meta, meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_add_remove_overlap(repo: repository.Repository) -> None:
    meta: types.FileMetadata = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="both added and removed"):
        begin(repo, added=[meta], removed=[meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_removal_of_missing_path(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="doesn't exist"):
        begin(repo, removed=[conftest.file_meta(HIVE_PATH, CONTENT)])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_removal_with_stale_sha(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    stale: types.FileMetadata = conftest.file_meta(HIVE_PATH, b"content the client saw long ago")
    with pytest.raises(transaction.TransactionConflictError, match="has changed"):
        begin(repo, removed=[stale])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_add_colliding_with_different_content(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    with pytest.raises(transaction.TransactionConflictError, match="different content"):
        begin(repo, added=[conftest.file_meta(HIVE_PATH, b"different bytes entirely")])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_byte_identical_readd(repo: repository.Repository) -> None:
    # re-adding exactly what's already there would commit nothing — a no-op is a client bug, not a success
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    with pytest.raises(transaction.TransactionConflictError, match="no-op"):
        begin(repo, added=[conftest.file_meta(HIVE_PATH, CONTENT)])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_creates_transaction_state(repo: repository.Repository) -> None:
    meta: types.FileMetadata = conftest.file_meta(HIVE_PATH, CONTENT)
    txn: transaction.Transaction = begin(repo, added=[meta])
    assert txn.added[HIVE_PATH].sha256 == meta.sha256

    # the same transaction is reachable by id afterwards, with the same state
    fetched: transaction.Transaction = repo.get_transaction(txn.txn_uuid)
    assert fetched.added.keys() == txn.added.keys()


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_unknown_id_raises(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionNotFoundError):
        repo.get_transaction("no-such-transaction")


# ----------------------------------------------------------------------------------------------------------------------
def test_latest_is_none_on_fresh_repo(repo: repository.Repository) -> None:
    assert conftest.current_manifest(repo) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_multiple_repos_share_one_db_but_remain_isolated(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
        )
    )
    first: repository.Repository = srv.create_repository("first")
    second: repository.Repository = srv.create_repository("second")

    conftest.commit_files(first, {"year=2024/a.csv": b"first repo"})
    conftest.commit_files(second, {"year=2024/b.csv": b"second repo"})

    assert first.metadata_store.get_current_seq() == 1
    assert second.metadata_store.get_current_seq() == 1

    first_manifest: types.Manifest = first.metadata_store.get_manifest(1)
    second_manifest: types.Manifest = second.metadata_store.get_manifest(1)
    assert [f.path for f in first_manifest.files] == ["year=2024/a.csv"]
    assert [f.path for f in second_manifest.files] == ["year=2024/b.csv"]

    assert srv.list_repositories() == ["first", "second"]
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
def test_multiple_repos_share_one_object_store(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
        )
    )
    first: repository.Repository = srv.create_repository("first")
    second: repository.Repository = srv.create_repository("second")

    shared_content: bytes = b"shared bytes"
    conftest.commit_files(first, {"year=2024/shared.csv": shared_content})
    conftest.commit_files(second, {"year=2024/shared.csv": shared_content})

    first_location: Path = first.object_store.get_location_for_address(conftest.sha256_hex(shared_content))
    second_location: Path = second.object_store.get_location_for_address(conftest.sha256_hex(shared_content))
    assert first_location == second_location
    assert first_location.read_bytes() == shared_content

    server_module.close()
