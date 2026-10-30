"""Environment inputs shared by the engine adapters."""

from pathlib import Path

import pytest

from dromon.engine_environment import ShellSelectionError, selected_shell


def test_selected_shell_accepts_an_absolute_executable(tmp_path: Path) -> None:
    shell = tmp_path / "bash"
    shell.write_text("#!/bin/sh\nexit 0\n")
    shell.chmod(0o755)
    assert selected_shell({"SHELL": str(shell)}) == str(shell)


@pytest.mark.parametrize(
    ("environment", "reason"),
    [({}, "SHELL is not set"), ({"SHELL": "bin/bash"}, "absolute path")],
)
def test_selected_shell_refuses_missing_or_relative_paths(
    environment: dict[str, str], reason: str
) -> None:
    with pytest.raises(ShellSelectionError, match=reason):
        selected_shell(environment)


def test_selected_shell_refuses_a_non_executable_file(tmp_path: Path) -> None:
    shell = tmp_path / "bash"
    shell.write_text("not executable\n")
    shell.chmod(0o644)
    with pytest.raises(ShellSelectionError, match="executable file"):
        selected_shell({"SHELL": str(shell)})
