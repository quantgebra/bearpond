import os
from pathlib import Path

import pytest

import conftest
from lakesync import types
from lakesync.server import paths
from lakesync.server import repository
from lakesync.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def test_fsync_dir_flushes_a_directory(tmp_path: Path) -> None:
    paths.fsync_dir(tmp_path)


# ----------------------------------------------------------------------------------------------------------------------
def test_atomic_write_produces_the_file(tmp_path: Path) -> None:
    target: Path = tmp_path / "state.json"
    paths.atomic_write(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    assert not (tmp_path / "state.json.tmp").exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_fsyncs_in_write_ahead_order(repo: repository.Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    # durability itself can't be tested without cutting power, but the discipline can: record every fsync, rename,
    # and unlink during an upload + commit and assert the write-ahead ordering — an upload's content is flushed
    # before it becomes staged, the journal is flushed before any move it describes, and the moves are flushed
    # before the journal comes down
    events: list[tuple[str, str]] = []
    real_fsync = os.fsync
    real_replace = os.replace
    real_unlink = os.unlink

    def recording_fsync(fd: int) -> None:
        events.append(("fsync", ""))
        real_fsync(fd)

    def recording_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        events.append(("replace", str(dst)))
        real_replace(src, dst)

    def recording_unlink(path: object, *args: object, **kwargs: object) -> None:
        events.append(("unlink", str(path)))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "unlink", recording_unlink)

    txn: transaction.Transaction = repo.begin_transaction(
        types.TransactionManifest(added=[conftest.file_meta(HIVE_PATH, CONTENT)])
    )
    conftest.upload_bytes(txn, HIVE_PATH, CONTENT)
    txn.commit()

    # the exact destination strings the rename calls used (safe_join resolves paths, so compute expectations the same way)
    staged_path: str = str(paths.safe_join(txn.txn_dir, HIVE_PATH))
    final_path: str = str(paths.safe_join(repo.data_root, HIVE_PATH))
    journal_path: str = str(repo.journal_path)

    def index_of(kind: str, target: str) -> int:
        return next(i for i, event in enumerate(events) if event == (kind, target))

    def fsynced_between(start: int, end: int) -> bool:
        return any(event[0] == "fsync" for event in events[start:end])

    upload_rename: int = index_of("replace", staged_path)
    journal_write: int = index_of("replace", journal_path)
    first_move: int = index_of("replace", final_path)
    journal_delete: int = index_of("unlink", journal_path)

    assert upload_rename < journal_write < first_move < journal_delete
    assert fsynced_between(0, upload_rename)
    assert fsynced_between(journal_write, first_move)
    assert fsynced_between(first_move, journal_delete)
