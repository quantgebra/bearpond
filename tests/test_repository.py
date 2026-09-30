from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.server import config
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
    assert txn.added[0].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_arbitrary_path(repo: repository.Repository) -> None:
    # paths are no longer required to follow hive-style key=value directories
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("trades/2024-08-22/executions.json", CONTENT)])
    assert txn.added[0].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_top_level_file(repo: repository.Repository) -> None:
    # a bare filename is also valid
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("part.csv", CONTENT)])
    assert txn.added[0].size == len(CONTENT)


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
    assert txn.added[0].sha256 == meta.sha256

    # the same transaction is reachable by id afterwards, with the same state
    fetched: transaction.Transaction = repo.get_transaction(txn.txn_uuid)
    assert [f.path for f in fetched.added] == [f.path for f in txn.added]


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_unknown_id_raises(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionNotFoundError):
        repo.get_transaction(types.TxnUuid("no-such-transaction"))


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


OTHER_PATH = "year=2024/month=02/other.parquet"
OTHER_CONTENT = b"other fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_restores_a_removed_file(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})  # seq 1
    conftest.remove_files(repo, {HIVE_PATH: CONTENT})  # seq 2

    result: types.CommitResponse = repo.revert(1, reason="bring it back")

    assert result.seq == 3
    manifest: types.Manifest = repo.metadata_store.get_manifest(3)
    assert [f.path for f in manifest.files] == [HIVE_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_undoes_an_update(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})  # seq 1
    manifest: types.TransactionManifest = types.TransactionManifest(
        updated=[
            types.UpdatedFileMetadata(
                path=HIVE_PATH,
                old_sha256=conftest.sha256_hex(CONTENT),
                old_size=len(CONTENT),
                new_sha256=conftest.sha256_hex(OTHER_CONTENT),
                new_size=len(OTHER_CONTENT),
            )
        ]
    )
    txn: transaction.Transaction = repo.begin_transaction(manifest)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, OTHER_CONTENT)
    repo.commit(txn.txn_uuid)  # seq 2

    result: types.CommitResponse = repo.revert(1)

    assert result.seq == 3
    reverted: types.FileMetadata | None = repo.metadata_store.get_file_metadata(HIVE_PATH)
    assert reverted is not None and reverted.sha256 == conftest.sha256_hex(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_to_current_seq_raises(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    with pytest.raises(transaction.TransactionValidationError, match="at least one file"):
        repo.revert(1)


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_to_invalid_seq_raises(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    with pytest.raises(transaction.TransactionValidationError, match="no such manifest version"):
        repo.revert(99)


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_creates_a_new_commit_chained_to_head(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})  # seq 1
    conftest.commit_files(repo, {OTHER_PATH: OTHER_CONTENT})  # seq 2

    repo.revert(1)  # seq 3: drops OTHER_PATH

    records: list[types.CommitRecord] = repo.metadata_store.get_commit_record_list()
    assert len(records) == 3
    assert records[2].parent_commit_hash == records[1].commit_hash
