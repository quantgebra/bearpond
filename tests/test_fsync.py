from pathlib import Path

import pytest

from bearpond.server import utils


# ----------------------------------------------------------------------------------------------------------------------
def test_fsync_dir_flushes_a_directory(tmp_path: Path) -> None:
    utils.fsync_dir(tmp_path)


# ----------------------------------------------------------------------------------------------------------------------
def test_atomic_write_produces_the_file(tmp_path: Path) -> None:
    target: Path = tmp_path / "state.json"
    utils.atomic_write(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    assert not (tmp_path / "state.json.tmp").exists()

