"""Behaviour of the seed's placeholder commands, including their error paths."""

from collections.abc import Callable, Sequence

import pytest

from dromon import __version__, cli, daemon


@pytest.mark.parametrize("command", list(cli.SUBCOMMANDS))
def test_each_planned_subcommand_reports_not_implemented(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([command]) == cli.EXIT_NOT_IMPLEMENTED
    assert f"dromon {command}: not implemented" in capsys.readouterr().err


def test_dromon_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2


def test_dromon_rejects_an_unknown_subcommand() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["no-such-command"])
    assert raised.value.code == 2


def test_the_placeholder_status_differs_from_a_usage_error() -> None:
    assert cli.EXIT_NOT_IMPLEMENTED not in (0, 1, 2)


@pytest.mark.parametrize("main", [cli.main, daemon.main])
def test_version_flag_prints_the_package_version(
    main: Callable[[Sequence[str] | None], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_dromond_reports_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert daemon.main([]) == cli.EXIT_NOT_IMPLEMENTED
    assert "dromond: not implemented" in capsys.readouterr().err


def test_dromond_rejects_an_unknown_option() -> None:
    with pytest.raises(SystemExit) as raised:
        daemon.main(["--no-such-option"])
    assert raised.value.code == 2
