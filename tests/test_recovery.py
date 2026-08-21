from pathlib import Path

import conftest
from bearpond import types
from bearpond.client import server_client
from bearpond.server import metadata_store
from bearpond.server import repository
from bearpond.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin_uploaded_txn(repo: repository.Repository, files: dict[str, bytes]) -> transaction.Transaction:
    # a transaction staged exactly as a client would leave it right before committing
    added: list[types.FileMetadata] = [conftest.file_meta(path, content) for path, content in files.items()]
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(added=added))
    for path, content in files.items():
        conftest.upload_bytes(txn, path, content)
    return txn


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_retries_cleanly_after_partial_object_placement(repo: repository.Repository) -> None:
    # crash frozen mid-placement: one object moved into the store, the other still staged. A retried commit must
    # finish the job — placement is idempotent because an object's name is its content.
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT, OTHER_PATH: OTHER_CONTENT})
    repo.object_store.insert(conftest.sha256_hex(CONTENT), txn.txn_dir / HIVE_PATH)

    result: types.CommitResponse = txn.commit()

    assert result.seq == 1
    assert conftest.object_path(repo, CONTENT).read_bytes() == CONTENT
    assert conftest.object_path(repo, OTHER_CONTENT).read_bytes() == OTHER_CONTENT
    assert not txn.txn_dir.exists()

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_record_commit_is_idempotent_by_txn_uuid(repo: repository.Repository) -> None:
    # crash frozen after the metadata commit but before staging cleanup — a retry must return the original
    # record, not fail on the duplicate txn_uuid or bump the seq
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    declared: dict[str, transaction.FileMeta] = txn.load_declared()

    first: metadata_store.CommitRecord = repo.record_commit(txn.txn_uuid, declared, {})
    second: metadata_store.CommitRecord = repo.record_commit(txn.txn_uuid, declared, {})

    assert second == first
    assert repo.metadata_store.get_current_seq() == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_orphaned_object_is_harmless(repo: repository.Repository) -> None:
    # crash frozen after placement but before the metadata commit: the object sits unreferenced, and the lake
    # simply doesn't know about it — a future GC reclaims it, nothing breaks in the meantime
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    repo.object_store.insert(conftest.sha256_hex(CONTENT), txn.txn_dir / HIVE_PATH)
    txn.abort()

    assert repo.object_store.contains_address(conftest.sha256_hex(CONTENT))
    assert conftest.current_manifest(repo) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_startup_recovers_interrupted_exports(repo: repository.Repository) -> None:
    # end to end: a server started over a lake whose export was interrupted re-creates the export through the app
    # lifespan, and a client connecting afterwards reads the manifest served from the store
    conftest.commit_files(repo, {HIVE_PATH: CONTENT, OTHER_PATH: OTHER_CONTENT})
    records: list[metadata_store.CommitRecord] = repo.metadata_store.get_commit_record_list()

    (repo.manifest_root / "manifest-00000001.json").unlink()
    repo.latest_pointer.unlink()
    (repo.manifest_root / "commits" / f"{records[0].txn_uuid}.json").unlink()

    with conftest.running_server() as url:
        with server_client.ServerClient(url, conftest.REPO_NAME) as client:
            manifest: types.Manifest = client.get_manifest()

    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]
    assert (repo.manifest_root / "manifest-00000001.json").is_file()
    assert repo.latest_pointer.is_file()
