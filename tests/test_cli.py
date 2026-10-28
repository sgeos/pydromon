"""Behaviour of the dromon and dromond commands, including their error paths."""

from collections.abc import Callable, Sequence

import pytest

from pathlib import Path

from dromon import __version__, cli, daemon, exits
from dromon.protocol import Operation, Response


def test_every_planned_subcommand_is_implemented() -> None:
    assert set(cli.IMPLEMENTED) == set(cli.SUBCOMMANDS)


def test_a_subcommand_without_a_handler_reports_not_implemented(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(cli.IMPLEMENTED, "status")
    assert cli.main(["status"]) == exits.NOT_IMPLEMENTED
    assert "dromon status: not implemented" in capsys.readouterr().err


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
    assert "cannot read the line configuration" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("interrupted", "printed"),
    [(True, "interrupted the turn of alpha"), (False, "alpha had no turn in flight")],
)
def test_interrupt_sends_the_operation_and_says_whether_a_turn_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    interrupted: bool,
    printed: str,
) -> None:
    """Item 9 of the live check of 2026-10-26, a scriptable interrupt."""
    sent: list[tuple[Operation, dict[str, object] | None]] = []

    def answer(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        sent.append((operation, args))
        return Response(
            "r", True, result={"line_id": "alpha", "interrupted": interrupted}
        )

    monkeypatch.setattr(cli, "call", answer)
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "interrupt", "alpha"]
    assert cli.main(argv) == exits.OK
    assert sent == [(Operation.INTERRUPT, {"line_id": "alpha"})]
    assert printed in capsys.readouterr().out


def test_interrupt_reports_a_refusal_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket, operation, args
        return Response("r", False, error_message="unknown line nobody")

    monkeypatch.setattr(cli, "call", refuse)
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "interrupt", "nobody"]
    assert cli.main(argv) == exits.FAILURE
    assert "dromon interrupt: unknown line nobody" in capsys.readouterr().err


def test_interrupt_without_a_daemon_reports_not_running(
    tmp_path: Path, short_runtime: Path
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "interrupt", "alpha"]
    assert cli.main(argv) == exits.NOT_RUNNING


type Answer = Callable[[Path, Operation, dict[str, object] | None], Response]


def answering(
    sent: list[tuple[Operation, dict[str, object] | None]], result: dict[str, object]
) -> Answer:
    def answer(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        sent.append((operation, args))
        return Response("r", True, result=result)

    return answer


@pytest.mark.parametrize(
    ("command", "result", "printed"),
    [
        (
            "hibernate",
            {"line_id": "alpha", "hibernated": True, "changed": True, "running": False},
            "hibernated alpha\n",
        ),
        (
            "hibernate",
            {"line_id": "alpha", "hibernated": True, "changed": True, "running": True},
            "hibernated alpha, whose turn in flight finishes first",
        ),
        (
            "hibernate",
            {
                "line_id": "alpha",
                "hibernated": True,
                "changed": False,
                "running": False,
            },
            "alpha was already hibernated",
        ),
        ("awaken", {"line_id": "alpha", "changed": True}, "awakened alpha"),
        ("awaken", {"line_id": "alpha", "changed": False}, "alpha was not hibernated"),
    ],
)
def test_hibernate_and_awaken_send_their_operation_and_say_what_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    result: dict[str, object],
    printed: str,
) -> None:
    sent: list[tuple[Operation, dict[str, object] | None]] = []
    monkeypatch.setattr(cli, "call", answering(sent, result))
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, command, "alpha"]) == 0
    assert sent == [(Operation(command), {"line_id": "alpha"})]
    assert printed in capsys.readouterr().out


def test_hibernate_reports_a_refusal_and_a_missing_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "hibernate", "nobody"]
    assert cli.main(argv) == exits.NOT_RUNNING

    def refuse(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket, operation, args
        return Response("r", False, error_message="no line nobody")

    monkeypatch.setattr(cli, "call", refuse)
    assert cli.main(argv) == exits.FAILURE
    assert "dromon hibernate: no line nobody" in capsys.readouterr().err


@pytest.mark.parametrize(("flag", "expected"), [([], None), (["--priority"], True)])
def test_send_passes_priority_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    flag: list[str],
    expected: bool | None,
) -> None:
    sent: list[tuple[Operation, dict[str, object] | None]] = []
    monkeypatch.setattr(cli, "call", answering(sent, {"message_id": "m-1"}))
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "send", "alpha", "stop", *flag]
    assert cli.main([*argv, "--operator", "ana"]) == exits.OK
    ((operation, args),) = sent
    assert operation is Operation.SEND and args is not None
    assert args.get("priority") is expected


@pytest.mark.parametrize(
    ("priority", "printed"),
    [(False, "message_id m-1\n"), (True, "message_id m-1\npriority mail\n")],
)
def test_send_prints_the_message_id_and_priority_only_when_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    priority: bool,
    printed: str,
) -> None:
    sent: list[tuple[Operation, dict[str, object] | None]] = []
    answer: dict[str, object] = {"message_id": "m-1", "priority": priority}
    monkeypatch.setattr(cli, "call", answering(sent, answer))
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "send", "alpha", "stop"]
    assert cli.main([*argv, "--operator", "ana"]) == exits.OK
    assert capsys.readouterr().out == printed


def test_send_refused_by_the_daemon_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket, operation, args
        return Response("r", False, error_message="priority mail goes to a line")

    monkeypatch.setattr(cli, "call", refuse)
    runtime = ["--runtime-dir", str(short_runtime)]
    argv = ["--project", str(tmp_path), *runtime, "send", "@ben", "stop"]
    assert cli.main([*argv, "--operator", "ana", "--priority"]) == exits.FAILURE
    assert "dromon send: priority mail goes to a line" in capsys.readouterr().err


def test_status_lists_each_line_with_its_hibernate_and_hold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines: list[dict[str, object]] = [
        {
            "line_id": "alpha",
            "condition": "hibernated",
            "hibernated": True,
            "pending": 2,
        },
        {
            "line_id": "beta",
            "condition": "running",
            "hibernated": True,
            "pending": 0,
            "held_by": "q-1",
        },
        {"line_id": "gamma", "condition": "idle", "hibernated": False, "pending": 0},
    ]
    sent: list[tuple[Operation, dict[str, object] | None]] = []
    monkeypatch.setattr(cli, "call", answering(sent, {"pid": 1, "lines": lines}))
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "status"]) == exits.OK
    out = capsys.readouterr().out
    assert "line alpha, hibernated, 2 pending\n" in out
    assert "line beta, running, 0 pending, hibernated, held by q-1\n" in out
    assert "line gamma, idle, 0 pending\n" in out


def test_peer_key_prints_the_pin_and_takes_no_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[Operation, dict[str, object] | None]] = []
    pin = "ab" * 32
    result: dict[str, object] = {
        "pin_sha256": pin,
        "certificate": "/p/.dromon/peers/identity-certificate.pem",
        "made": True,
    }
    monkeypatch.setattr(cli, "call", answering(sent, result))
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "peer"]
    assert cli.main([*base, "key"]) == exits.OK
    assert sent == [(Operation.PEER_KEY, {})]
    out = capsys.readouterr().out
    assert f"pin_sha256 {pin}" in out and "made now" in out
    assert cli.main([*base, "key", "studio"]) == exits.USAGE
    assert "takes no peer name" in capsys.readouterr().err
    assert len(sent) == 1
