from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.server import metadata_store
from bearpond.server import utils
from bearpond.server import repository
from bearpond.server import transaction

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
def test_commit_lands_object_manifest_and_record(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    result: types.CommitResponse = txn.commit()

    assert result.seq == 1
    assert result.files_added_count == 1
    assert result.files_removed_count == 0

    # the content lives exactly once, under its hash — and the name really is the content
    object_file: Path = conftest.object_path(repo, CONTENT)
    assert object_file.read_bytes() == CONTENT
    assert utils.sha256_file(object_file) == conftest.sha256_hex(CONTENT)
    assert not txn.txn_dir.exists()

    # the mapping points the path at the object, and the commit is on record
    current: types.FileMetadata | None = repo.metadata_store.get_file_metadata(HIVE_PATH)
    assert current is not None
    assert current.sha256 == conftest.sha256_hex(CONTENT)

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert manifest.seq == 1
    assert [f.path for f in manifest.files] == [HIVE_PATH]

    record: metadata_store.CommitRecord | None = repo.metadata_store.get_commit_record(txn.txn_uuid)
    assert record is not None
    assert record.seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_bumps_seq_once_per_change(repo: repository.Repository) -> None:
    first: types.CommitResponse = add_and_commit(repo, HIVE_PATH, CONTENT)
    second: types.CommitResponse = add_and_commit(repo, OTHER_PATH, OTHER_CONTENT)
    assert (first.seq, second.seq) == (1, 2)

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert len(manifest.files) == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_path_landed_elsewhere_since_begin(repo: repository.Repository) -> None:
    # the path was free at begin time, but another commit has since landed different content there
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    conftest.commit_files(repo, {HIVE_PATH: OTHER_CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="different content"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_removal_target_vanished_since_begin(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    # another transaction removes the path first
    conftest.remove_files(repo, {HIVE_PATH: CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="no longer exists"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_removal_unmaps_path_but_keeps_object(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    result: types.CommitResponse = txn.commit()

    assert result.seq == 2
    assert result.files_removed_count == 1

    # the mapping is gone but the object stays — retained content is what makes restore-without-reupload possible
    assert repo.metadata_store.get_file_metadata(HIVE_PATH) is None
    assert conftest.object_path(repo, CONTENT).read_bytes() == CONTENT

    latest: types.Manifest | None = conftest.current_manifest(repo)
    assert latest is not None
    assert latest.files == []


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_identical_content_landed_since_begin(repo: repository.Repository) -> None:
    # the path was free at begin time, but another commit has since landed the very same content there —
    # committing now would change nothing, so it's rejected as a no-op rather than recorded
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="no-op"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_readd_after_removal_starts_a_new_lifecycle(repo: repository.Repository) -> None:
    # the same path can live again after removal — the assets table keeps both lifecycles, exactly one of them live
    add_and_commit(repo, HIVE_PATH, CONTENT)
    conftest.remove_files(repo, {HIVE_PATH: CONTENT})
    readded: types.CommitResponse = add_and_commit(repo, HIVE_PATH, OTHER_CONTENT)

    assert readded.seq == 3

    current: types.FileMetadata | None = repo.metadata_store.get_file_metadata(HIVE_PATH)
    assert current is not None
    assert current.sha256 == conftest.sha256_hex(OTHER_CONTENT)

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert [f.path for f in manifest.files] == [HIVE_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_rejects_staged_file_corrupted_after_upload(repo: repository.Repository) -> None:
    # upload verified the content, but the staged file was damaged on disk afterwards — insertion re-verifies,
    # because an object's name is only its content if someone checks
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    (txn.txn_dir / HIVE_PATH).write_bytes(b"corrupted on disk after upload")

    with pytest.raises(transaction.TransactionConflictError, match="does not match its address"):
        txn.commit()


# ----------------------------------------------------------------------------------------------------------------------
def test_abort_clears_staging_dir(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    txn.abort()
    assert not txn.txn_dir.exists()
    assert not repo.object_store.contains_address(conftest.sha256_hex(CONTENT))
