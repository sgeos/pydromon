"""``reopen_directories``, which repairs what a killed test left under the pytest base.
**Round 16.**"""

import os
import shutil
from collections.abc import Callable
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


# Round 16 review, point 4. A running pytest's base is left alone.


def base_with_lock(root: Path, name: str, lock: str | None) -> Path:
    """A numbered base holding a closed directory, and a lock file holding ``lock``."""
    base = root / name
    closed = base / "test_x" / "closed"
    closed.mkdir(parents=True)
    closed.chmod(0)
    if lock is not None:
        (base / ".lock").write_text(lock)
    return closed


def dead_pid() -> int:
    """A process identifier of a child that has ended and been waited for."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_lock_names_a_running_pytest_only_while_its_process_lives(
    tmp_path: Path,
) -> None:
    from conftest import owned_by_running_pytest

    base = tmp_path / "pytest-1"
    base.mkdir()
    assert not owned_by_running_pytest(base), "no lock"
    (base / ".lock").write_text(str(os.getpid()))
    assert owned_by_running_pytest(base)
    (base / ".lock").write_text(f"{os.getpid()}\n")
    assert owned_by_running_pytest(base)
    (base / ".lock").write_text(str(dead_pid()))
    assert not owned_by_running_pytest(base)
    for text in ("", "not a pid", "-5", "0"):
        (base / ".lock").write_text(text)
        assert not owned_by_running_pytest(base), text


def test_a_base_of_a_running_pytest_is_not_repaired_and_a_dead_ones_is(
    tmp_path: Path,
) -> None:
    from conftest import owned_by_running_pytest

    running = base_with_lock(tmp_path, "pytest-1", str(os.getpid()))
    dead = base_with_lock(tmp_path, "pytest-2", str(dead_pid()))
    unlocked = base_with_lock(tmp_path, "pytest-3", None)
    try:
        changed = reopen_directories(tmp_path, keep=owned_by_running_pytest)
        assert changed == 2
        assert not os.access(running, os.R_OK), "the running session's is left closed"
        assert os.access(dead, os.R_OK) and os.access(unlocked, os.R_OK)
        # Without the predicate every base is repaired, as before.
        assert reopen_directories(tmp_path) == 1
        assert os.access(running, os.R_OK)
    finally:
        for closed in (running, dead, unlocked):
            if closed.exists():
                closed.chmod(0o700)


def test_the_fixture_keeps_the_running_base_of_a_parallel_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The session fixture's call, made again here on the real base, leaves a sibling
    base with a live lock closed and repairs one with a dead lock."""
    from conftest import REOPEN_DEPTH, owned_by_running_pytest

    current = tmp_path_factory.getbasetemp()
    sibling_running = current.parent / "pytest-test-running-sibling"
    sibling_dead = current.parent / "pytest-test-dead-sibling"
    running = base_with_lock(current.parent, sibling_running.name, str(os.getpid()))
    dead = base_with_lock(current.parent, sibling_dead.name, str(dead_pid()))
    try:
        reopen_directories(
            current.parent,
            REOPEN_DEPTH + 1,
            skip=current,
            keep=owned_by_running_pytest,
        )
        assert not os.access(running, os.R_OK)
        assert os.access(dead, os.R_OK)
    finally:
        for closed, base in ((running, sibling_running), (dead, sibling_dead)):
            closed.chmod(0o700)
            shutil.rmtree(base)


def ignore_registration(function: Callable[..., object], *arguments: object) -> None:
    """Stands in for pytest's registration of cleanups, which a test does not want."""


def test_the_installed_pytest_writes_its_process_identifier_to_the_lock(
    tmp_path: Path,
) -> None:
    """Round 16 follow-up review, point 4. ``owned_by_running_pytest`` rests on a fact
    about pytest, so it is checked against the installed one, which the project pins
    exactly. A base made as the session makes its own holds ``.lock`` with the
    identifier of this process and nothing else, and is then seen as running."""
    from _pytest.pathlib import make_numbered_dir_with_cleanup

    from conftest import owned_by_running_pytest

    base = make_numbered_dir_with_cleanup(
        root=tmp_path,
        prefix="pytest-",
        mode=0o700,
        keep=3,
        lock_timeout=3600,
        register=ignore_registration,
    )
    assert (base / ".lock").read_text() == str(os.getpid())
    assert owned_by_running_pytest(base)
    (base / ".lock").unlink()
    assert not owned_by_running_pytest(base)
