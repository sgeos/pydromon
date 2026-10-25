"""Behaviour of the seed's placeholder commands, including their error paths."""

from collections.abc import Callable, Sequence

import pytest

from pathlib import Path

from dromon import __version__, cli, daemon, exits


PLACEHOLDERS = [name for name in cli.SUBCOMMANDS if name not in cli.IMPLEMENTED]


def test_the_lifecycle_subcommands_are_implemented() -> None:
    assert set(cli.IMPLEMENTED) == {"start", "status", "stop"}
    assert set(cli.IMPLEMENTED) <= set(cli.SUBCOMMANDS)


@pytest.mark.parametrize("command", PLACEHOLDERS)
def test_each_unimplemented_subcommand_reports_not_implemented(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([command]) == exits.NOT_IMPLEMENTED
    assert f"dromon {command}: not implemented" in capsys.readouterr().err


def test_dromon_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2


def test_dromon_rejects_an_unknown_subcommand() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["no-such-command"])
    assert raised.value.code == 2


def test_every_exit_status_is_distinct() -> None:
    statuses = [
        exits.OK,
        exits.FAILURE,
        exits.USAGE,
        exits.NOT_IMPLEMENTED,
        exits.NOT_RUNNING,
        exits.ALREADY_RUNNING,
    ]
    assert len(set(statuses)) == len(statuses)


def test_status_without_a_daemon_reports_not_running(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    assert (
        cli.main(["--project", str(tmp_path), *runtime, "status"]) == exits.NOT_RUNNING
    )
    assert "not running" in capsys.readouterr().out


def test_stop_without_a_daemon_reports_not_running(
    tmp_path: Path, short_runtime: Path
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "stop"]) == exits.NOT_RUNNING


def test_a_missing_project_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--project", str(tmp_path / "absent"), "status"]) == exits.FAILURE
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.parametrize("main", [cli.main, daemon.main])
def test_version_flag_prints_the_package_version(
    main: Callable[[Sequence[str] | None], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_dromond_refuses_a_missing_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert daemon.main(["--project", str(tmp_path / "absent")]) == exits.FAILURE
    assert "not a directory" in capsys.readouterr().err


def test_dromond_rejects_an_unknown_option() -> None:
    with pytest.raises(SystemExit) as raised:
        daemon.main(["--no-such-option"])
    assert raised.value.code == 2
