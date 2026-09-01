import conftest
from bearpond import types
from bearpond.server import repository
from bearpond.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin_uploaded_txn(repo: repository.Repository, files: dict[str, bytes]) -> transaction.Transaction:
    # a transaction staged exactly as a client would leave it right before committing
    added: list[types.FileMetadata] = []
    for path, content in files.items():
        added.append(conftest.file_meta(path, content))
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(added=added))
    for path, content in files.items():
        conftest.upload_bytes(repo, txn.txn_uuid, path, content)
    return txn


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_transaction_is_idempotent_by_txn_uuid(repo: repository.Repository) -> None:
    # crash frozen after the metadata commit but before transaction cleanup — a retry must return the original
    # record, not fail on the duplicate txn_uuid or bump the seq
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})

    first: types.CommitRecord = repo.metadata_store.commit_transaction(txn)
    second: types.CommitRecord = repo.metadata_store.commit_transaction(txn)

    assert second == first
    assert repo.metadata_store.get_current_seq() == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_orphaned_object_is_harmless(repo: repository.Repository) -> None:
    # an upload followed by an abort leaves the object in the store unreferenced — a future GC reclaims it, and
    # the lake simply doesn't know about it in the meantime
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    repo.abort(txn.txn_uuid)

    assert repo.object_store.contains_address(conftest.sha256_hex(CONTENT))
    assert conftest.current_manifest(repo) is None
