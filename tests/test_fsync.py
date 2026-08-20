import os
from collections.abc import Callable
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.server import utils
from bearpond.server import repository
from bearpond.server import transaction

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def test_fsync_dir_flushes_a_directory(tmp_path: Path) -> None:
    utils.fsync_dir(tmp_path)


# ----------------------------------------------------------------------------------------------------------------------
def test_atomic_write_produces_the_file(tmp_path: Path) -> None:
    target: Path = tmp_path / "state.json"
    utils.atomic_write(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    assert not (tmp_path / "state.json.tmp").exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_fsyncs_in_durability_order(repo: repository.Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    # durability itself can't be tested without cutting power, but the discipline can: record every fsync, rename,
    # and unlink during an upload + commit and assert the ordering — an upload's content is flushed before it
    # becomes staged, and an object's placement rename is flushed before the manifest export that publishes it
    events: list[tuple[str, str]] = []
    real_fsync: Callable[[int], None] = os.fsync
    real_replace: Callable[..., None] = os.replace
    real_unlink: Callable[..., None] = os.unlink

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

    # the exact destination strings the rename calls used (safe_join resolves paths, so compute that one the same way)
    staged_path: str = str(utils.safe_join(txn.txn_dir, HIVE_PATH))
    object_path: str = str(repo.object_store.get_location_for_address(conftest.sha256_hex(CONTENT)))
    manifest_path: str = str(repo.manifest_root / "manifest-00000001.json")

    def index_of(kind: str, target: str) -> int:
        return next(i for i, event in enumerate(events) if event == (kind, target))

    def fsynced_between(start: int, end: int) -> bool:
        return any(event[0] == "fsync" for event in events[start:end])

    upload_rename: int = index_of("replace", staged_path)
    object_rename: int = index_of("replace", object_path)
    manifest_write: int = index_of("replace", manifest_path)

    assert upload_rename < object_rename < manifest_write
    assert fsynced_between(0, upload_rename)
    assert fsynced_between(object_rename, manifest_write)
