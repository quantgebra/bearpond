from pathlib import Path

import pytest

import conftest
from lakesync import types
from lakesync.server import repository
from lakesync.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin_add(repo: repository.Repository, rel_path: str, content: bytes) -> transaction.Transaction:
    manifest: types.TransactionManifest = types.TransactionManifest(added=[conftest.file_meta(rel_path, content)])
    return repo.begin_transaction(manifest)


# ----------------------------------------------------------------------------------------------------------------------
def add_and_commit(repo: repository.Repository, rel_path: str, content: bytes) -> types.CommitResponse:
    txn: transaction.Transaction = begin_add(repo, rel_path, content)
    conftest.upload_bytes(txn, rel_path, content)
    return txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_rejects_undeclared_path(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="not declared"):
        conftest.upload_bytes(txn, "year=2024/sneaky.parquet", CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_rejects_sha_mismatch_and_leaves_nothing_behind(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="sha256 mismatch"):
        conftest.upload_bytes(txn, HIVE_PATH, b"not the declared content")

    assert txn.load_uploaded() == {}
    assert not (txn.txn_dir / HIVE_PATH).exists()
    assert not (txn.txn_dir / f"{HIVE_PATH}.tmp").exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_rejects_missing_uploads(repo: repository.Repository) -> None:
    manifest: types.TransactionManifest = types.TransactionManifest(
        added=[conftest.file_meta(HIVE_PATH, CONTENT), conftest.file_meta(OTHER_PATH, OTHER_CONTENT)]
    )
    txn: transaction.Transaction = repo.begin_transaction(manifest)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)

    with pytest.raises(transaction.TransactionValidationError, match="not yet uploaded"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_lands_files_manifest_and_record(repo: repository.Repository, repo_root: Path) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    result: types.CommitResponse = txn.commit()

    assert result.seq == 1
    assert result.files_added_count == 1
    assert result.files_removed_count == 0

    # the file is in the lake, the staging dir is gone, and the manifest and commit record both reflect the commit
    assert (repo_root / "data" / HIVE_PATH).read_bytes() == CONTENT
    assert not txn.txn_dir.exists()

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert manifest.seq == 1
    assert [f.path for f in manifest.files] == [HIVE_PATH]

    record_path: Path = repo.manifest_root / "commits" / f"{txn.txn_id}.json"
    assert record_path.is_file()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_bumps_seq_once_per_change(repo: repository.Repository) -> None:
    first: types.CommitResponse = add_and_commit(repo, HIVE_PATH, CONTENT)
    second: types.CommitResponse = add_and_commit(repo, OTHER_PATH, OTHER_CONTENT)
    assert (first.seq, second.seq) == (1, 2)

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert len(manifest.files) == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_existing_file_changed_since_begin(repo: repository.Repository, repo_root: Path) -> None:
    # the file didn't exist at begin time, but different content has landed at the same path by commit time
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    conftest.write_lake_file(repo_root, HIVE_PATH, b"what another commit landed in the meantime")

    with pytest.raises(transaction.TransactionConflictError, match="different content"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_removal_target_changed_since_begin(repo: repository.Repository, repo_root: Path) -> None:
    conftest.write_lake_file(repo_root, HIVE_PATH, CONTENT)
    manifest: types.TransactionManifest = types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    txn: transaction.Transaction = repo.begin_transaction(manifest)
    conftest.write_lake_file(repo_root, HIVE_PATH, b"changed after the transaction opened")

    with pytest.raises(transaction.TransactionConflictError, match="has changed"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_removal_moves_file_into_removed(repo: repository.Repository, repo_root: Path) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    manifest: types.TransactionManifest = types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    txn: transaction.Transaction = repo.begin_transaction(manifest)
    result: types.CommitResponse = txn.commit()

    assert result.seq == 2
    assert result.files_removed_count == 1

    # the file leaves data/ but stays recoverable under removed/, and the manifest drops it
    assert not (repo_root / "data" / HIVE_PATH).exists()
    assert (repo_root / "removed" / HIVE_PATH).read_bytes() == CONTENT

    latest: types.Manifest | None = repo.latest()
    assert latest is not None
    assert latest.files == []


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_byte_identical_readd_keeps_seq(repo: repository.Repository) -> None:
    # re-committing content already in the lake changes nothing, so the manifest version doesn't advance
    first: types.CommitResponse = add_and_commit(repo, HIVE_PATH, CONTENT)
    second: types.CommitResponse = add_and_commit(repo, HIVE_PATH, CONTENT)
    assert second.seq == first.seq


# ----------------------------------------------------------------------------------------------------------------------
def test_abort_clears_staging_dir(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    txn.abort()
    assert not txn.txn_dir.exists()
