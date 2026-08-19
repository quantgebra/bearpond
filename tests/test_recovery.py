import os
from pathlib import Path

import pytest

import conftest
from lakesync import types
from lakesync.client import lakesync_client
from lakesync.server import repository
from lakesync.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def begin_uploaded_txn(repo: repository.Repository, files: dict[str, bytes]) -> transaction.Transaction:
    # a transaction staged exactly as a client would leave it right before committing
    added: list[types.FileMeta] = [conftest.file_meta(path, content) for path, content in files.items()]
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(added=added))
    for path, content in files.items():
        conftest.upload_bytes(txn, path, content)
    return txn


# ----------------------------------------------------------------------------------------------------------------------
def write_journal_for(repo: repository.Repository, txn: transaction.Transaction) -> transaction.CommitJournal:
    # the journal commit() would have written — writing it by hand lets a test freeze the crash mid-commit
    journal: transaction.CommitJournal = transaction.CommitJournal(
        txn_id=txn.txn_id, declared=txn.load_declared(), removed=txn.load_removed()
    )
    repo.write_journal(journal)
    return journal


# ----------------------------------------------------------------------------------------------------------------------
def move_staged_file_into_lake(repo_root: Path, txn: transaction.Transaction, rel_path: str) -> None:
    # one step of the interrupted commit, performed by hand
    final_path: Path = repo_root / "data" / rel_path
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(txn.txn_dir / rel_path, final_path)


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_leaves_no_journal_behind(repo: repository.Repository) -> None:
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    txn.commit()
    assert not repo.journal_path.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_without_journal_is_a_noop(repo: repository.Repository) -> None:
    repo.recover()
    assert repo.latest() is None


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_completes_partially_moved_commit(repo: repository.Repository, repo_root: Path) -> None:
    # crash frozen after the journal write and the first of two file moves
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT, OTHER_PATH: OTHER_CONTENT})
    write_journal_for(repo, txn)
    move_staged_file_into_lake(repo_root, txn, HIVE_PATH)

    repo.recover()

    # the second move is replayed, staging and journal are cleaned up, and the commit is fully recorded
    assert (repo_root / "data" / HIVE_PATH).read_bytes() == CONTENT
    assert (repo_root / "data" / OTHER_PATH).read_bytes() == OTHER_CONTENT
    assert not txn.txn_dir.exists()
    assert not repo.journal_path.exists()

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert manifest.seq == 1
    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]

    record: repository.CommitRecord | None = repo.load_commit_record(txn.txn_id)
    assert record is not None
    assert record.seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_finishes_commit_interrupted_after_all_moves(repo: repository.Repository, repo_root: Path) -> None:
    # crash frozen with every file moved but the commit not yet recorded — the staging dir's state files linger
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    write_journal_for(repo, txn)
    move_staged_file_into_lake(repo_root, txn, HIVE_PATH)

    repo.recover()

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert manifest.seq == 1
    assert repo.load_commit_record(txn.txn_id) is not None
    assert not txn.txn_dir.exists()
    assert not repo.journal_path.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_does_not_double_record_a_recorded_commit(repo: repository.Repository) -> None:
    # crash frozen after the commit was recorded but before the journal was deleted — replay must not bump seq
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    committed: types.CommitResponse = txn.commit()

    journal: transaction.CommitJournal = transaction.CommitJournal(
        txn_id=txn.txn_id,
        declared={HIVE_PATH: transaction.FileMeta(size=len(CONTENT), sha256=conftest.sha256_hex(CONTENT))},
        removed={},
    )
    repo.write_journal(journal)

    repo.recover()

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert manifest.seq == committed.seq
    assert not repo.journal_path.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_moves_pending_removals(repo: repository.Repository, repo_root: Path) -> None:
    conftest.write_lake_file(repo_root, HIVE_PATH, CONTENT)
    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(removed=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    write_journal_for(repo, txn)

    repo.recover()

    assert not (repo_root / "data" / HIVE_PATH).exists()
    assert (repo_root / "removed" / HIVE_PATH).read_bytes() == CONTENT

    manifest: types.Manifest | None = repo.latest()
    assert manifest is not None
    assert manifest.files == []


# ----------------------------------------------------------------------------------------------------------------------
def test_recover_fails_loudly_and_keeps_journal_when_commit_cannot_finish(
    repo: repository.Repository,
) -> None:
    # the staged copy is gone and nothing landed in the lake — the commit can't complete, and that must be loud
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT})
    write_journal_for(repo, txn)
    (txn.txn_dir / HIVE_PATH).unlink()

    with pytest.raises(repository.RepositoryError, match="could not finish"):
        repo.recover()

    assert repo.journal_path.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_startup_recovery_makes_interrupted_commit_visible(repo: repository.Repository, repo_root: Path) -> None:
    # end to end: a server started over a crashed lake runs recovery through the app lifespan, and a client
    # connecting afterwards simply sees the finished commit
    txn: transaction.Transaction = begin_uploaded_txn(repo, {HIVE_PATH: CONTENT, OTHER_PATH: OTHER_CONTENT})
    write_journal_for(repo, txn)
    move_staged_file_into_lake(repo_root, txn, HIVE_PATH)

    with conftest.running_server() as url:
        with lakesync_client.LakesyncClient(url) as client:
            manifest: types.Manifest = client.get_manifest()

    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]
    assert not repo.journal_path.exists()
    assert not txn.txn_dir.exists()
