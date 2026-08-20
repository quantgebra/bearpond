from pathlib import Path

import pytest

from bearpond.server import utils


# ----------------------------------------------------------------------------------------------------------------------
def test_safe_join_accepts_paths_inside_base(tmp_path: Path) -> None:
    resolved: Path = utils.safe_join(tmp_path, "year=2024/month=01/part.parquet")
    assert resolved == (tmp_path / "year=2024/month=01/part.parquet").resolve()


# ----------------------------------------------------------------------------------------------------------------------
def test_safe_join_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(utils.InvalidPathError, match="invalid path"):
        utils.safe_join(tmp_path, "../outside.parquet")


# ----------------------------------------------------------------------------------------------------------------------
def test_safe_join_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(utils.InvalidPathError, match="invalid path"):
        utils.safe_join(tmp_path, "/etc/passwd")
