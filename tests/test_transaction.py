from pathlib import Path

import pytest

import conftest
from bearpond import types
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
    conftest.upload_bytes(repo, txn.txn_uuid, rel_path, content)
    return repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_rejects_undeclared_path(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="not declared"):
        conftest.upload_bytes(repo, txn.txn_uuid, "year=2024/sneaky.parquet", CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_rejects_sha_mismatch_and_leaves_nothing_behind(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="sha256 mismatch"):
        conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, b"not the declared content")

    assert repo.get_transaction(txn.txn_uuid).uploaded == []
    # the object was never placed in the content-addressed store because the stream did not match
    assert not repo.object_store.contains_address(conftest.sha256_hex(b"not the declared content"))


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_rejects_missing_uploads(repo: repository.Repository) -> None:
    manifest: types.TransactionManifest = types.TransactionManifest(
        added=[conftest.file_meta(HIVE_PATH, CONTENT), conftest.file_meta(OTHER_PATH, OTHER_CONTENT)]
    )
    txn: transaction.Transaction = repo.begin_transaction(manifest)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)

    with pytest.raises(transaction.TransactionValidationError, match="not yet uploaded"):
        repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_lands_object_manifest_and_record(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)
    result: types.CommitResponse = repo.commit(txn.txn_uuid)

    assert result.seq == 1
    assert result.files_added_count == 1
    assert result.files_removed_count == 0

    # the content lives exactly once, under its hash — and the name really is the content
    object_file: Path = conftest.object_path(repo, CONTENT)
    assert object_file.read_bytes() == CONTENT
    assert utils.sha256_file(object_file) == conftest.sha256_hex(CONTENT)
    # the transaction state is deleted once the commit is recorded
    assert repo.metadata_store.get_transaction(txn.txn_uuid) is None

    # the mapping points the path at the object, and the commit is on record
    current: types.FileMetadata | None = repo.metadata_store.get_file_metadata(HIVE_PATH)
    assert current is not None
    assert current.sha256 == conftest.sha256_hex(CONTENT)

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert manifest.seq == 1
    assert [f.path for f in manifest.files] == [HIVE_PATH]

    record: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_txn_uuid(txn.txn_uuid)
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
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)
    conftest.commit_files(repo, {HIVE_PATH: OTHER_CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="different content"):
        repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_conflicts_when_removal_target_vanished_since_begin(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    # another transaction removes the path first
    conftest.remove_files(repo, {HIVE_PATH: CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="no longer exists"):
        repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_removal_unmaps_path_but_keeps_object(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    result: types.CommitResponse = repo.commit(txn.txn_uuid)

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
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})

    with pytest.raises(transaction.TransactionConflictError, match="no-op"):
        repo.commit(txn.txn_uuid)


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
def test_abort_clears_transaction_state_but_leaves_orphan_object(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_add(repo, HIVE_PATH, CONTENT)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)
    repo.abort(txn.txn_uuid)
    # the transaction state is gone but the object remains in the content-addressed store as an orphan
    assert repo.metadata_store.get_transaction(txn.txn_uuid) is None
    assert repo.object_store.contains_address(conftest.sha256_hex(CONTENT))


# ----------------------------------------------------------------------------------------------------------------------
def begin_update(
    repo: repository.Repository, rel_path: str, old_content: bytes, new_content: bytes
) -> transaction.Transaction:
    manifest: types.TransactionManifest = types.TransactionManifest(
        updated=[
            types.UpdatedFileMetadata(
                path=rel_path,
                old_sha256=conftest.sha256_hex(old_content),
                old_size=len(old_content),
                new_sha256=conftest.sha256_hex(new_content),
                new_size=len(new_content),
            )
        ]
    )
    return repo.begin_transaction(manifest)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_replaces_content_at_path(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    txn: transaction.Transaction = begin_update(repo, HIVE_PATH, CONTENT, OTHER_CONTENT)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, OTHER_CONTENT)
    result: types.CommitResponse = repo.commit(txn.txn_uuid)

    assert result.seq == 2
    assert result.files_added_count == 0
    assert result.files_removed_count == 0
    assert result.files_updated_count == 1

    current: types.FileMetadata | None = repo.metadata_store.get_file_metadata(HIVE_PATH)
    assert current is not None
    assert current.sha256 == conftest.sha256_hex(OTHER_CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_preserves_object_history(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)
    txn: transaction.Transaction = begin_update(repo, HIVE_PATH, CONTENT, OTHER_CONTENT)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, OTHER_CONTENT)
    repo.commit(txn.txn_uuid)

    # both the old and new content objects remain stored — the old row is marked removed, the new one is live
    assert conftest.object_path(repo, CONTENT).read_bytes() == CONTENT
    assert conftest.object_path(repo, OTHER_CONTENT).read_bytes() == OTHER_CONTENT

    records: list[types.CommitRecord] = repo.metadata_store.get_commit_record_list()
    assert records[1].updated[0].old_sha256 == conftest.sha256_hex(CONTENT)
    assert records[1].updated[0].new_sha256 == conftest.sha256_hex(OTHER_CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_rejects_wrong_old_content_at_begin(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    with pytest.raises(transaction.TransactionConflictError, match="has changed since it was declared for update"):
        begin_update(repo, HIVE_PATH, OTHER_CONTENT, b"new")


# ----------------------------------------------------------------------------------------------------------------------
def test_update_rejects_missing_path_at_begin(repo: repository.Repository) -> None:
    with pytest.raises(transaction.TransactionValidationError, match="cannot update a path that doesn't exist"):
        begin_update(repo, HIVE_PATH, CONTENT, OTHER_CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_rejects_concurrent_change_at_commit(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    txn: transaction.Transaction = begin_update(repo, HIVE_PATH, CONTENT, OTHER_CONTENT)
    conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, OTHER_CONTENT)

    # another transaction changes the path before this one commits
    interloper_txn: transaction.Transaction = begin_update(repo, HIVE_PATH, CONTENT, b"interloper")
    conftest.upload_bytes(repo, interloper_txn.txn_uuid, HIVE_PATH, b"interloper")
    repo.commit(interloper_txn.txn_uuid)

    with pytest.raises(transaction.TransactionConflictError, match="has changed since it was declared for update"):
        repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_rejects_identical_content(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    with pytest.raises(transaction.TransactionValidationError, match="old and new content are identical"):
        begin_update(repo, HIVE_PATH, CONTENT, CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_update_upload_uses_new_sha256(repo: repository.Repository) -> None:
    add_and_commit(repo, HIVE_PATH, CONTENT)

    txn: transaction.Transaction = begin_update(repo, HIVE_PATH, CONTENT, OTHER_CONTENT)
    with pytest.raises(transaction.TransactionValidationError, match="sha256 mismatch"):
        conftest.upload_bytes(repo, txn.txn_uuid, HIVE_PATH, CONTENT)
