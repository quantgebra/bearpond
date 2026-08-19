from pathlib import Path

import pytest

import conftest
from lakesync import types
from lakesync.server import repository
from lakesync.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin(
    repo: repository.Repository,
    added: list[types.FileMeta] | None = None,
    removed: list[types.FileMeta] | None = None,
) -> transaction.Transaction:
    return repo.begin_transaction(types.TransactionManifest(added=added or [], removed=removed or []))


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_empty_manifest(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="at least one file"):
        begin(repo)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_non_parquet_path(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="not a parquet file"):
        begin(repo, added=[conftest.file_meta("year=2024/data.csv", CONTENT)])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_non_hive_segment(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="hive partition"):
        begin(repo, added=[conftest.file_meta("2024/month=01/part.parquet", CONTENT)])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_top_level_parquet(repo: repository.Repository) -> None:
    # partition segments are optional — a bare filename is a valid add
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta("part.parquet", CONTENT)])
    assert txn.load_declared()["part.parquet"].size == len(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_duplicate_added_path(repo: repository.Repository) -> None:
    meta: types.FileMeta = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="more than once"):
        begin(repo, added=[meta, meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_duplicate_removed_path(repo: repository.Repository) -> None:
    meta: types.FileMeta = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="more than once"):
        begin(repo, removed=[meta, meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_add_remove_overlap(repo: repository.Repository) -> None:
    meta: types.FileMeta = conftest.file_meta(HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="both added and removed"):
        begin(repo, added=[meta], removed=[meta])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_removal_of_missing_path(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="doesn't exist"):
        begin(repo, removed=[conftest.file_meta(HIVE_PATH, CONTENT)])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_removal_with_stale_sha(repo: repository.Repository, repo_root: Path) -> None:
    conftest.write_lake_file(repo_root, HIVE_PATH, CONTENT)
    stale: types.FileMeta = conftest.file_meta(HIVE_PATH, b"content the client saw long ago")
    with pytest.raises(transaction.TransactionConflictError, match="has changed"):
        begin(repo, removed=[stale])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_rejects_add_colliding_with_different_content(repo: repository.Repository, repo_root: Path) -> None:
    conftest.write_lake_file(repo_root, HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionConflictError, match="different content"):
        begin(repo, added=[conftest.file_meta(HIVE_PATH, b"different bytes entirely")])


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_allows_add_byte_identical_to_existing(repo: repository.Repository, repo_root: Path) -> None:
    # re-adding exactly what's already there is an idempotent no-op, not a conflict
    conftest.write_lake_file(repo_root, HIVE_PATH, CONTENT)
    txn: transaction.Transaction = begin(repo, added=[conftest.file_meta(HIVE_PATH, CONTENT)])
    assert HIVE_PATH in txn.load_declared()


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_creates_staging_dir_and_state(repo: repository.Repository) -> None:
    meta: types.FileMeta = conftest.file_meta(HIVE_PATH, CONTENT)
    txn: transaction.Transaction = begin(repo, added=[meta])
    assert txn.txn_dir.is_dir()
    declared: dict[str, transaction.FileMeta] = txn.load_declared()
    assert declared[HIVE_PATH].sha256 == meta.sha256

    # the same transaction is reachable by id afterwards, with the same state
    fetched: transaction.Transaction = repo.get_transaction(txn.txn_id)
    assert fetched.load_declared().keys() == declared.keys()


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_unknown_id_raises(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionNotFoundError):
        repo.get_transaction("no-such-transaction")


# ----------------------------------------------------------------------------------------------------------------------
def test_latest_is_none_on_fresh_repo(repo: repository.Repository) -> None:
    assert repo.latest() is None


# ----------------------------------------------------------------------------------------------------------------------
def test_latest_raises_on_dangling_pointer(repo: repository.Repository) -> None:
    # a pointer naming a missing manifest is corruption and must be loud — never mistaken for an empty lake
    repo.manifest_root.mkdir(parents=True)
    repo.latest_pointer.write_text("manifest-00000001.json")
    with pytest.raises(repository.RepositoryError, match="dangling"):
        repo.latest()


# ----------------------------------------------------------------------------------------------------------------------
def test_latest_raises_on_corrupt_manifest(repo: repository.Repository) -> None:
    repo.manifest_root.mkdir(parents=True)
    (repo.manifest_root / "manifest-00000001.json").write_text("{not valid json")
    repo.latest_pointer.write_text("manifest-00000001.json")
    with pytest.raises(repository.RepositoryError, match="corrupt"):
        repo.latest()
