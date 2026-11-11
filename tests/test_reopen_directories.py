"""``reopen_directories``, which repairs what a killed test left under the pytest base.
**Round 16.**"""

import os
import shutil
from pathlib import Path

import pytest

from conftest import reopen_directories

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")


def closed_tree(root: Path) -> Path:
    """``root/a/b/closed``, holding a file, with ``closed`` and ``a`` unreadable."""
    closed = root / "a" / "b" / "closed"
    closed.mkdir(parents=True)
    (closed / "file").write_text("x")
    closed.chmod(0)
    (root / "a").chmod(0o500)
    return closed


def test_an_unreadable_directory_is_made_deletable(tmp_path: Path) -> None:
    root = tmp_path / "base"
    root.mkdir()
    closed = closed_tree(root)
    try:
        assert not os.access(closed, os.R_OK)
        changed = reopen_directories(root)
        assert changed == 2
        assert os.access(closed, os.R_OK)
        shutil.rmtree(root)
        assert not root.exists()
    finally:
        if closed.exists():
            reopen_directories(root)


def test_a_clean_tree_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "x" / "y").mkdir(parents=True)
    assert reopen_directories(tmp_path) == 0


def test_the_walk_is_bounded_by_depth(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "closed"
    deep.mkdir(parents=True)
    deep.chmod(0)
    try:
        assert reopen_directories(tmp_path, depth=2) == 0
        assert not os.access(deep, os.R_OK)
        assert reopen_directories(tmp_path, depth=4) == 1
        assert os.access(deep, os.R_OK)
    finally:
        deep.chmod(0o700)


def test_a_skipped_directory_is_not_entered(tmp_path: Path) -> None:
    kept = tmp_path / "kept"
    closed = kept / "closed"
    closed.mkdir(parents=True)
    closed.chmod(0)
    try:
        assert reopen_directories(tmp_path, skip=kept) == 0
        assert not os.access(closed, os.R_OK)
    finally:
        closed.chmod(0o700)


def test_a_symbolic_link_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    base = tmp_path / "base"
    base.mkdir()
    (base / "link").symlink_to(outside)
    try:
        assert reopen_directories(base) == 0
        assert (outside.stat().st_mode & 0o777) == 0o500
    finally:
        outside.chmod(0o700)


def test_a_missing_root_is_no_error(tmp_path: Path) -> None:
    assert reopen_directories(tmp_path / "absent") == 0
