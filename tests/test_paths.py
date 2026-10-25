"""Project paths, the socket length limit and the private directory checks."""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from dromon.paths import (
    MAX_SOCKET_PATH_BYTES,
    RUNTIME_DIR_ENV,
    PathError,
    ensure_private_dir,
    project_paths,
)


def test_defaults_live_under_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RUNTIME_DIR_ENV, raising=False)
    short = Path(os.path.realpath(tempfile.gettempdir()))
    paths = project_paths(short)
    assert paths.state_dir == short / ".dromon"
    assert paths.event_log == short / ".dromon" / "events.sqlite"
    assert paths.socket == short / ".dromon" / "run" / "dromond.sock"


def test_the_environment_moves_the_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = Path(tempfile.gettempdir()) / "dr-env"
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(configured))
    assert project_paths(tmp_path).runtime_dir == configured.resolve()


def test_an_explicit_runtime_directory_overrides_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(Path(tempfile.gettempdir()) / "dr-env"))
    explicit = Path(tempfile.gettempdir()) / "dr-explicit"
    assert project_paths(tmp_path, explicit).runtime_dir == explicit.resolve()


def test_a_socket_path_over_the_limit_is_refused_with_advice(tmp_path: Path) -> None:
    deep = Path(tempfile.gettempdir()) / ("d" * MAX_SOCKET_PATH_BYTES)
    with pytest.raises(PathError, match=RUNTIME_DIR_ENV):
        project_paths(tmp_path, deep)


def test_a_missing_project_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PathError, match="not a directory"):
        project_paths(tmp_path / "absent")


def test_a_private_directory_is_created_with_mode_700(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o777])
def test_a_directory_open_to_others_is_refused(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "open"
    target.mkdir()
    target.chmod(mode)
    with pytest.raises(PathError, match="Restrict it to 700"):
        ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == mode


def test_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(PathError, match="not a plain directory"):
        ensure_private_dir(link)


def test_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("")
    with pytest.raises(PathError):
        ensure_private_dir(target)
