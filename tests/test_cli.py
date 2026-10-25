"""Behaviour of the seed's placeholder commands, including their error paths."""

from collections.abc import Callable, Sequence

import pytest

from pathlib import Path

from dromon import __version__, cli, daemon, exits


PLACEHOLDERS = [name for name in cli.SUBCOMMANDS if name not in cli.IMPLEMENTED]


def test_the_lifecycle_subcommands_are_implemented() -> None:
    assert set(cli.IMPLEMENTED) == {
        "start",
        "status",
        "stop",
        "probe",
        "create",
        "configure",
        "send",
    }
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


def test_probe_refuses_an_api_key_without_starting_the_engine(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "probe"]) == exits.FAILURE
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("login", "expected"),
    [("ana", "ana"), ("Ana.B", "ana.b"), ("j doe", "j-doe"), ("--", "operator")],
)
def test_the_default_operator_is_a_valid_identifier(
    monkeypatch: pytest.MonkeyPatch, login: str, expected: str
) -> None:
    monkeypatch.setattr(cli.getpass, "getuser", lambda: login)
    assert cli.default_operator() == expected


def test_send_without_a_daemon_reports_not_running(
    tmp_path: Path, short_runtime: Path
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "send", "alpha", "hi"]
    assert cli.main(argv) == exits.NOT_RUNNING


def test_tool_options_build_a_configuration(tmp_path: Path) -> None:
    servers = tmp_path / "mcp.json"
    servers.write_text(
        '{"mcpServers": {"fs": {"command": "fs-mcp"}}}', encoding="utf-8"
    )
    parser = cli.build_parser()
    arguments = parser.parse_args(
        [
            "configure",
            "alpha",
            "--tool",
            "Read",
            "--mcp-config",
            str(servers),
            "--allow-tool",
            "mcp__fs__*",
        ]
    )
    document = cli._config_document(arguments)  # pyright: ignore[reportPrivateUsage]
    assert document == {
        "builtin_tools": ["Read"],
        "allowed_tools": ["mcp__fs__*"],
        "tool_policy": "allowlist",
        "inherit_account_mcp": False,
        "mcp_servers": {"mcpServers": {"fs": {"command": "fs-mcp"}}},
    }


def test_no_tool_options_mean_no_configuration() -> None:
    arguments = cli.build_parser().parse_args(["create", "alpha"])
    assert cli._config_document(arguments) is None  # pyright: ignore[reportPrivateUsage]


def test_an_unreadable_mcp_file_fails_before_contacting_the_daemon(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = [
        "--project",
        str(tmp_path),
        *runtime,
        "create",
        "alpha",
        "--mcp-config",
        "/nope",
    ]
    assert cli.main(argv) == exits.FAILURE
    assert "cannot read the MCP configuration" in capsys.readouterr().err
