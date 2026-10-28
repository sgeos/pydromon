"""External agents: their parties, the access policy, the gateway, the agent socket and
the MCP bridge. No test here starts an engine."""

import asyncio
import io
import json
import os
import re
import socket
import stat
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, client, exits
from dromon.agents import (
    DAEMON_TOOLS,
    MAIL_TOOLS,
    TOKEN_ENV,
    AgentError,
    AgentPolicy,
    parse_policy,
    token_digest,
)
from dromon.daemon import MAX_AGENT_TEXT, Daemon
from dromon.eventlog import EventLog
from dromon.mcp import PROTOCOL_VERSIONS, Bridge, serve
from dromon.model import (
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    AgentRevoked,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
    parse_address,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ConfigFileError, parse
from dromon.protocol import ErrorCode, Operation, Request, Response
from dromon.scheduler import LineEngine, LineSpec, Scheduler

OPEN = AgentPolicy(True, AgentAccess.MESSAGE, AgentAccess.OPERATOR)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@contextmanager
def daemon_for(
    tmp_path: Path, short_runtime: Path, policy: AgentPolicy = OPEN
) -> Generator[Daemon]:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", "claude-haiku-5-5", "subscription"))
        log.append(OperatorRegistered("ana"))
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        yield Daemon(paths, log, "2026-10-25T00:00:00Z", scheduler, agents=policy)


def agent_line(op: str, args: dict[str, object]) -> bytes:
    return (json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n").encode()


def agent_call(
    daemon: Daemon, agent: str, tool: str, token: str | None = None, **arguments: object
) -> Response:
    args: dict[str, object] = {"agent": agent, "tool": tool, "arguments": arguments}
    if token is not None:
        args["token"] = token
    return asyncio.run(daemon.respond_agent(agent_line("agent_call", args)))


def text_of(response: Response) -> tuple[str, bool]:
    assert response.ok and response.result is not None, response.error_message
    content = cast(list[dict[str, str]], response.result["content"])
    return content[0]["text"], bool(response.result["is_error"])


def tool_names(daemon: Daemon, agent: str, token: str | None = None) -> set[str]:
    args: dict[str, object] = {"agent": agent}
    if token is not None:
        args["token"] = token
    response = asyncio.run(daemon.respond_agent(agent_line("agent_tools", args)))
    assert response.ok and response.result is not None, response.error_message
    tools = cast(list[dict[str, object]], response.result["tools"])
    return {str(tool["name"]) for tool in tools}


def register(daemon: Daemon, agent: str, access: AgentAccess) -> str:
    response = daemon.execute(
        Request("a", Operation.AGENT_ADD, {"agent": agent, "access": access.value})
    )
    assert response.ok and response.result is not None
    return str(response.result["token"])


# The model.


def test_an_agent_address_is_distinct_from_lines_and_operators() -> None:
    assert parse_address("&claude") == AgentAddress("claude")
    assert str(AgentAddress("claude")) == "&claude"
    assert parse_address("claude") == LineAddress("claude")
    with pytest.raises(InvalidField):
        AgentAddress("Bad Name")


def test_a_token_is_stored_only_as_a_digest() -> None:
    AgentRegistered("probe", AgentAccess.OBSERVE, token_digest("secret"))
    AgentRegistered("probe", AgentAccess.OBSERVE, "")
    with pytest.raises(InvalidField, match="SHA-256"):
        AgentRegistered("probe", AgentAccess.OBSERVE, "secret")


def test_a_revoked_agent_neither_sends_nor_receives_until_registered_again() -> None:
    state = State()
    state.apply(LineCreated("alpha", "claude-haiku-5-5", "subscription"))
    state.apply(AgentRegistered("probe", AgentAccess.MESSAGE, ""))
    probe = AgentAddress("probe")
    state.apply(MessageSent("m1", probe, LineAddress("alpha"), MessageKind.NOTE, "hi"))
    state.apply(AgentRevoked("probe"))
    with pytest.raises(EventRejected, match="no agent probe"):
        state.apply(
            MessageSent("m2", LineAddress("alpha"), probe, MessageKind.NOTE, "hi")
        )
    with pytest.raises(EventRejected, match="no agent probe to revoke"):
        state.apply(AgentRevoked("probe"))
    with pytest.raises(EventRejected, match="no agent ghost"):
        state.apply(AgentRevoked("ghost"))
    state.apply(AgentRegistered("probe", AgentAccess.OBSERVE, ""))
    assert not state.agents["probe"].revoked
    assert state.agents["probe"].access is AgentAccess.OBSERVE


def test_an_agent_cannot_ask_a_question() -> None:
    state = State()
    state.apply(OperatorRegistered("ana"))
    state.apply(AgentRegistered("probe", AgentAccess.MESSAGE, ""))
    with pytest.raises(EventRejected, match="only a line asks"):
        state.apply(
            MessageSent(
                "q",
                AgentAddress("probe"),
                OperatorAddress("ana"),
                MessageKind.QUESTION,
                "?",
            )
        )


def test_access_levels_are_ordered() -> None:
    ranks = [level.rank for level in AgentAccess]
    assert ranks == sorted(ranks) == list(range(len(AgentAccess)))


# The policy.


def test_the_policy_defaults_to_off() -> None:
    assert parse_policy({}) == AgentPolicy()
    assert not AgentPolicy().listen
    assert parse("version = 1\n").agents == AgentPolicy()


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({"listen": False}, AgentPolicy()),
        (
            {"listen": True, "max_access": "observe"},
            AgentPolicy(True, AgentAccess.NONE, AgentAccess.OBSERVE),
        ),
        (
            {"listen": True, "anonymous": "message"},
            AgentPolicy(True, AgentAccess.MESSAGE, AgentAccess.MESSAGE),
        ),
        (
            {"listen": True, "anonymous": "operator", "max_access": "operator"},
            AgentPolicy(True, AgentAccess.OPERATOR, AgentAccess.OPERATOR),
        ),
    ],
)
def test_the_policy_spans_off_to_full_trust(
    table: dict[str, object], expected: AgentPolicy
) -> None:
    assert parse_policy(table) == expected


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ([], "must be a table"),
        ({"open": True}, "agents.open is not a known key"),
        ({"listen": "yes"}, "agents.listen must be a boolean"),
        ({"anonymous": "admin"}, "agents.anonymous must be one of"),
        ({"max_access": 3}, "agents.max_access must be one of"),
        ({"anonymous": "manage"}, "anonymous manage exceeds agents.max_access message"),
    ],
)
def test_a_bad_policy_is_refused(table: object, message: str) -> None:
    with pytest.raises(AgentError, match=message):
        parse_policy(table)


def test_the_project_file_locates_a_bad_policy() -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse("version = 1\n\n[agents]\nlisten = true\nanonymous = 'root'\n")
    assert "agents: agents.anonymous must be one of" in str(raised.value)
    assert raised.value.line == 3


# Authentication.


def test_without_anonymous_access_an_unknown_agent_is_refused(
    tmp_path: Path, short_runtime: Path
) -> None:
    policy = AgentPolicy(True, AgentAccess.NONE, AgentAccess.MESSAGE)
    with daemon_for(tmp_path, short_runtime, policy) as daemon:
        response = agent_call(daemon, "stranger", "dromon_status")
        assert (response.error_code, response.error_message) == (
            ErrorCode.REJECTED,
            "agent stranger has no access",
        )
        assert "stranger" not in daemon.log.state.agents


def test_an_anonymous_observer_sees_status_but_cannot_send(
    tmp_path: Path, short_runtime: Path
) -> None:
    policy = AgentPolicy(True, AgentAccess.OBSERVE, AgentAccess.MESSAGE)
    with daemon_for(tmp_path, short_runtime, policy) as daemon:
        text, is_error = text_of(agent_call(daemon, "watcher", "dromon_status"))
        assert not is_error and json.loads(text)["lines"][0]["line_id"] == "alpha"
        text, is_error = text_of(
            agent_call(daemon, "watcher", "send_message", to="alpha", body="hi")
        )
        assert is_error and "needs message access" in text
        assert "watcher" not in daemon.log.state.agents


def test_an_anonymous_messenger_becomes_a_party_once(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        text, is_error = text_of(
            agent_call(daemon, "guest", "send_message", to="alpha", body="hello")
        )
        assert not is_error and text.startswith("sent as m-")
        agent_call(daemon, "guest", "list_mail")
        registrations = [
            entry for entry in daemon.log.state.agents.items() if entry[0] == "guest"
        ]
        assert registrations == [("guest", daemon.log.state.agents["guest"])]
        assert daemon.log.state.agents["guest"].token_sha256 == ""
        sent = list(daemon.log.state.messages.values())[-1]
        assert (sent.sender, sent.recipient) == (
            AgentAddress("guest"),
            LineAddress("alpha"),
        )


def test_a_registered_agent_needs_its_own_token(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "probe", AgentAccess.MANAGE)
        assert "create_line" in tool_names(daemon, "probe", token)
        for wrong in (None, "wrong"):
            response = agent_call(daemon, "probe", "dromon_status", token=wrong)
            assert response.error_message == "agent probe needs its token"
        response = agent_call(daemon, "unknown", "dromon_status", token=token)
        assert response.error_message == "agent unknown is not registered with a token"
        rotated = register(daemon, "probe", AgentAccess.MANAGE)
        assert not agent_call(daemon, "probe", "dromon_status", token=token).ok
        assert agent_call(daemon, "probe", "dromon_status", token=rotated).ok


def test_a_revoked_agent_is_refused(tmp_path: Path, short_runtime: Path) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "probe", AgentAccess.MESSAGE)
        revoked = daemon.execute(
            Request("v", Operation.AGENT_REVOKE, {"agent": "probe"})
        )
        assert revoked.ok
        response = agent_call(daemon, "probe", "dromon_status", token=token)
        assert response.error_message == "agent probe is revoked"


@pytest.mark.parametrize(
    ("access", "extra"),
    [
        (AgentAccess.OBSERVE, set[str]()),
        (AgentAccess.MESSAGE, set(MAIL_TOOLS)),
        (AgentAccess.MANAGE, set(MAIL_TOOLS)),
        (AgentAccess.OPERATOR, set(MAIL_TOOLS)),
    ],
)
def test_each_level_lists_exactly_its_tools(
    tmp_path: Path, short_runtime: Path, access: AgentAccess, extra: set[str]
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "probe", access)
        expected = extra | {
            spec.name for spec in DAEMON_TOOLS if spec.minimum.rank <= access.rank
        }
        assert tool_names(daemon, "probe", token) == expected


def test_the_ceiling_caps_a_registered_agent(
    tmp_path: Path, short_runtime: Path
) -> None:
    policy = AgentPolicy(True, AgentAccess.NONE, AgentAccess.OBSERVE)
    with daemon_for(tmp_path, short_runtime, policy) as daemon:
        added = daemon.execute(
            Request("a", Operation.AGENT_ADD, {"agent": "probe", "access": "operator"})
        )
        assert added.ok and added.result is not None
        assert added.result["effective_access"] == "observe"
        token = str(added.result["token"])
        assert tool_names(daemon, "probe", token) == {"dromon_status", "view_line"}
        text, is_error = text_of(
            agent_call(daemon, "probe", "stop_daemon", token=token)
        )
        assert is_error and "needs operator access, and this agent has observe" in text
        assert not daemon.stop_requested.is_set()


# Calls at each level.


def test_an_agent_works_its_own_mailbox(tmp_path: Path, short_runtime: Path) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "probe", AgentAccess.MESSAGE)
        sent = agent_call(
            daemon, "probe", "send_message", token, to="alpha", body="ping"
        )
        first_id = text_of(sent)[0].removeprefix("sent as ")
        reply = daemon.scheduler.send(
            LineAddress("alpha"), AgentAddress("probe"), "pong", reply_to=first_id
        )
        listing, _ = text_of(agent_call(daemon, "probe", "list_mail", token))
        assert reply in listing and "from alpha" in listing
        assert daemon.log.state.messages[reply].delivered
        body, _ = text_of(
            agent_call(daemon, "probe", "read_mail", token, message_id=reply)
        )
        assert body.endswith("pong")
        assert daemon.log.state.messages[reply].read
        agent_call(daemon, "probe", "acknowledge_mail", token, message_id=reply)
        assert daemon.log.state.messages[reply].acknowledged
        fates, _ = text_of(agent_call(daemon, "probe", "list_sent", token))
        assert first_id in fates
        text, is_error = text_of(
            agent_call(daemon, "probe", "read_mail", token, message_id=first_id)
        )
        assert is_error and "no message" in text


def test_a_manager_creates_configures_and_releases_lines(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "boss", AgentAccess.MANAGE)
        _, is_error = text_of(
            agent_call(
                daemon,
                "boss",
                "create_line",
                token,
                line_id="beta",
                model="claude-haiku-5-5",
            )
        )
        assert not is_error and "beta" in daemon.log.state.lines
        _, is_error = text_of(
            agent_call(
                daemon,
                "boss",
                "configure_line",
                token,
                line_id="beta",
                config={"builtin_tools": ["Read"]},
            )
        )
        assert not is_error and daemon.log.state.lines["beta"].config is not None
        text, is_error = text_of(
            agent_call(daemon, "boss", "release_line", token, line_id="beta")
        )
        assert is_error and "not held" in text
        text, is_error = text_of(agent_call(daemon, "boss", "list_questions", token))
        assert is_error and "needs operator access" in text


def test_an_operator_agent_answers_as_the_operator_and_stops_the_daemon(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        daemon.log.append(
            MessageSent(
                "q1",
                LineAddress("alpha"),
                OperatorAddress("ana"),
                MessageKind.QUESTION,
                "?",
            )
        )
        token = register(daemon, "root", AgentAccess.OPERATOR)
        listed, _ = text_of(agent_call(daemon, "root", "list_questions", token))
        assert "q1" in listed
        _, is_error = text_of(
            agent_call(
                daemon, "root", "answer_question", token, question_id="q1", body="yes"
            )
        )
        assert not is_error
        answer = list(daemon.log.state.messages.values())[-1]
        assert (answer.sender, answer.reply_to) == (OperatorAddress("ana"), "q1")
        assert "agent &root answers q1 as @ana" in capsys.readouterr().err
        text, is_error = text_of(
            agent_call(
                daemon, "root", "answer_question", token, question_id="zz", body="x"
            )
        )
        assert is_error and "no question zz" in text
        agent_call(daemon, "root", "stop_daemon", token)
        assert daemon.stop_requested.is_set()


def test_bad_calls_are_tool_errors(tmp_path: Path, short_runtime: Path) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        assert text_of(agent_call(daemon, "guest", "no_such_tool"))[1]
        bad = asyncio.run(
            daemon.respond_agent(
                agent_line(
                    "agent_call",
                    {"agent": "guest", "tool": "view_line", "arguments": []},
                )
            )
        )
        assert text_of(bad) == ("arguments must be an object", True)
        missing, is_error = text_of(
            agent_call(daemon, "guest", "view_line", line_id="zz")
        )
        assert is_error and "no line zz" in missing


# Separation of the sockets.


def test_the_operator_socket_refuses_agent_operations(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        response = daemon.respond(agent_line("agent_call", {"agent": "x"}))
        assert response.error_code is ErrorCode.UNKNOWN_OPERATION


def test_the_agent_socket_refuses_operator_operations(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        for op in ("status", "stop", "agent_add", "send"):
            response = asyncio.run(daemon.respond_agent(agent_line(op, {})))
            assert response.error_code is ErrorCode.UNKNOWN_OPERATION
        assert asyncio.run(daemon.respond_agent(agent_line("ping", {}))).ok
        bad = asyncio.run(daemon.respond_agent(b"not json\n"))
        assert bad.error_code is ErrorCode.BAD_REQUEST
        assert not daemon.stop_requested.is_set()


def test_large_results_are_cut_to_fit_one_line(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        token = register(daemon, "probe", AgentAccess.MESSAGE)
        big = daemon.scheduler.send(
            LineAddress("alpha"), AgentAddress("probe"), "x" * 200_000
        )
        body, _ = text_of(
            agent_call(daemon, "probe", "read_mail", token, message_id=big)
        )
        assert len(body) <= MAX_AGENT_TEXT + 100
        assert body.endswith("[truncated by Dromon]")


def test_agent_list_reports_the_policy_and_agents(
    tmp_path: Path, short_runtime: Path
) -> None:
    with daemon_for(tmp_path, short_runtime) as daemon:
        register(daemon, "probe", AgentAccess.OBSERVE)
        agent_call(daemon, "guest", "list_mail")
        listed = daemon.execute(Request("l", Operation.AGENT_LIST, {}))
        assert listed.ok and listed.result is not None
        assert listed.result["policy"] == {
            "listen": True,
            "anonymous": "message",
            "max_access": "operator",
        }
        assert listed.result["agents"] == [
            {"agent": "&guest", "access": "message", "token": False, "revoked": False},
            {"agent": "&probe", "access": "observe", "token": True, "revoked": False},
        ]
        bad = daemon.execute(
            Request("a", Operation.AGENT_ADD, {"agent": "x", "access": "root"})
        )
        assert bad.error_code is ErrorCode.REJECTED
        status = daemon.status()
        assert cast(dict[str, object], status["agents"])["registered"] == 2


# The MCP bridge, without a daemon.


def bridge(runtime: Path) -> Bridge:
    return Bridge(runtime / "absent.sock", "probe", None)


def test_the_bridge_negotiates_a_version_and_names_the_agent(
    short_runtime: Path,
) -> None:
    tmp_path = short_runtime
    answer = bridge(tmp_path).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert answer is not None
    result = cast(dict[str, Any], answer["result"])
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "&probe" in result["instructions"]
    newest = bridge(tmp_path).handle(
        {"id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
    )
    assert newest is not None
    assert (
        cast(dict[str, Any], newest["result"])["protocolVersion"]
        == PROTOCOL_VERSIONS[0]
    )


def test_the_bridge_reports_a_missing_daemon(short_runtime: Path) -> None:
    target = bridge(short_runtime)
    listed = target.handle({"id": 1, "method": "tools/list"})
    assert listed is not None
    assert "not running, or does not let agents connect" in str(listed["error"])
    called = target.handle(
        {
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_mail", "arguments": {}},
        }
    )
    assert called is not None
    assert cast(dict[str, Any], called["result"])["isError"] is True


def test_the_bridge_handles_protocol_edges(short_runtime: Path) -> None:
    target = bridge(short_runtime)
    assert target.handle({"method": "notifications/initialized"}) is None
    assert target.handle({"id": 1, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    unknown = target.handle({"id": 2, "method": "resources/list"})
    assert (
        unknown is not None and cast(dict[str, Any], unknown["error"])["code"] == -32601
    )
    bad = target.handle({"id": 3, "method": "tools/call", "params": {"name": 3}})
    assert bad is not None and cast(dict[str, Any], bad["error"])["code"] == -32602
    output = io.StringIO()
    status = serve(
        target,
        io.StringIO('not json\n\n[1, 2]\n{"id": 4, "method": "ping"}\n'),
        output,
    )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert status == 0
    assert [r.get("error", {}).get("code") for r in replies] == [-32700, -32600, None]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 4, "result": {}}


# The client.


def test_dromon_agent_needs_a_name_for_add_and_revoke(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    assert cli.main([*argv, "agent", "add"]) == exits.USAGE
    assert "name the agent" in capsys.readouterr().err
    assert cli.main([*argv, "agent", "list"]) == exits.NOT_RUNNING


def through_agent_socket(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Path, Operation, dict[str, object]]]:
    """Answer the client's calls with ``daemon``'s agent socket handling, without a
    socket, and record each call."""
    calls: list[tuple[Path, Operation, dict[str, object]]] = []

    def answer(
        socket_path: Path,
        operation: Operation,
        timeout_seconds: float = 0.0,
        args: dict[str, object] | None = None,
    ) -> Response:
        given = dict(args or {})
        calls.append((socket_path, operation, given))
        return asyncio.run(daemon.respond_agent(agent_line(operation.value, given)))

    monkeypatch.setattr(cli, "call", answer)
    return calls


def test_dromon_agent_send_sends_one_note_as_the_agent(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    with daemon_for(
        tmp_path,
        short_runtime,
        AgentPolicy(True, AgentAccess.NONE, AgentAccess.MESSAGE),
    ) as daemon:
        token = register(daemon, "table", AgentAccess.MESSAGE)
        calls = through_agent_socket(daemon, monkeypatch)
        monkeypatch.setenv(TOKEN_ENV, token)
        send = ["agent", "send", "--agent", "table", "--to", "alpha", "your move"]
        assert cli.main([*argv, *send]) == exits.OK
        sent = daemon.log.state.messages
        assert [(m.sender, m.recipient, m.kind, m.body) for m in sent.values()] == [
            (AgentAddress("table"), LineAddress("alpha"), MessageKind.NOTE, "your move")
        ]
        assert capsys.readouterr().out == f"sent as {next(iter(sent))}\n"
        # The command goes to the agent socket, and the token never to an argument.
        [(socket_path, operation, args)] = calls
        assert socket_path == daemon.paths.agent_socket
        assert operation is Operation.AGENT_CALL
        assert token not in send and args["token"] == token


def test_dromon_agent_send_reports_a_refusal_and_needs_its_arguments(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    for partial in (
        ["--to", "alpha", "body"],
        ["--agent", "table", "body"],
        ["--agent", "table", "--to", "alpha"],
    ):
        assert cli.main([*argv, "agent", "send", *partial]) == exits.USAGE
        assert "give --agent NAME, --to PARTY and the body" in capsys.readouterr().err
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    send = ["agent", "send", "--agent", "table", "--to", "alpha", "hello"]
    assert cli.main([*argv, *send]) == exits.NOT_RUNNING
    assert "lets agents connect" in capsys.readouterr().err
    with daemon_for(
        tmp_path,
        short_runtime,
        AgentPolicy(True, AgentAccess.NONE, AgentAccess.MESSAGE),
    ) as daemon:
        token = register(daemon, "table", AgentAccess.MESSAGE)
        through_agent_socket(daemon, monkeypatch)
        # Without its token a registered agent is refused by the daemon.
        assert cli.main([*argv, *send]) == exits.FAILURE
        assert "needs its token" in capsys.readouterr().err
        # A refusal by the tool is a failure, and sends nothing.
        monkeypatch.setenv(TOKEN_ENV, token)
        nowhere = ["agent", "send", "--agent", "table", "--to", "omega", "hello"]
        assert cli.main([*argv, *nowhere]) == exits.FAILURE
        assert "omega" in capsys.readouterr().err
        assert daemon.log.state.messages == {}


# Through the real daemon and its sockets.


def start_daemon(
    tmp_path: Path, short_runtime: Path, table: str, *, hold: bool = True
) -> tuple[ProjectPaths, int]:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    (paths.state_dir / "dromon.toml").write_text(f"version = 1\n{table}")
    return paths, client.start(paths, ["--hold-wakes"] if hold else [])


def stop_daemon(paths: ProjectPaths, pid: int) -> None:
    if client.is_running(paths):
        client.stop(paths)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished:
            return
        time.sleep(0.05)


@pytest.mark.socket
def test_no_agent_socket_exists_unless_the_project_listens(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths, pid = start_daemon(tmp_path, short_runtime, "")
    try:
        assert paths.socket.exists()
        assert not paths.agent_socket.exists()
        # The line socket is always open, private, and serves only issued line tokens.
        assert stat.S_IMODE(paths.line_socket.stat().st_mode) == 0o600
    finally:
        stop_daemon(paths, pid)
    assert not paths.line_socket.exists()


@pytest.mark.socket
def test_an_agent_reaches_the_daemon_through_its_private_socket(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths, pid = start_daemon(
        tmp_path,
        short_runtime,
        '[agents]\nlisten = true\nanonymous = "observe"\n'
        '[lines.alpha]\nmodel = "claude-haiku-5-5"\n',
    )
    try:
        assert stat.S_IMODE(paths.agent_socket.stat().st_mode) == 0o600
        output = io.StringIO()
        serve(
            Bridge(paths.agent_socket, "watcher", None),
            io.StringIO(
                '{"id": 1, "method": "tools/list"}\n'
                '{"id": 2, "method": "tools/call",'
                ' "params": {"name": "dromon_status"}}\n'
            ),
            output,
        )
        listed, status = (json.loads(line) for line in output.getvalue().splitlines())
        assert {t["name"] for t in listed["result"]["tools"]} == {
            "dromon_status",
            "view_line",
        }
        text = status["result"]["content"][0]["text"]
        assert json.loads(text)["lines"][0]["line_id"] == "alpha"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.connect(str(paths.agent_socket))
            raw.sendall(agent_line("stop", {}))
            assert b"unknown_operation" in raw.recv(4096)
        assert client.is_running(paths)
    finally:
        stop_daemon(paths, pid)
    assert not paths.agent_socket.exists()


@pytest.mark.socket
def test_dromon_agent_send_reaches_a_line_through_the_agent_socket(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, pid = start_daemon(
        tmp_path,
        short_runtime,
        '[agents]\nlisten = true\nanonymous = "message"\n'
        '[lines.alpha]\nmodel = "claude-haiku-5-5"\n',
    )
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    try:
        send = ["agent", "send", "--agent", "table", "--to", "alpha", "your move"]
        assert cli.main([*argv, *send]) == exits.OK
        assert capsys.readouterr().out.startswith("sent as m-")
    finally:
        stop_daemon(paths, pid)


@pytest.mark.live
@pytest.mark.socket
def test_a_live_claude_code_session_messages_a_line_through_dromon_mcp(
    tmp_path: Path, short_runtime: Path
) -> None:
    """An external Claude Code session, on the operator's subscription, reaches a line
    through ``dromon mcp`` and receives its reply in its own mailbox."""
    import sys

    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        ToolPermissionContext,
    )

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    paths, pid = start_daemon(
        tmp_path.resolve(),
        short_runtime,
        '[agents]\nlisten = true\nmax_access = "message"\n'
        '[lines.beta]\nmodel = "claude-haiku-5-5"\n',
        hold=False,
    )
    try:
        added = client.call(
            paths.socket,
            Operation.AGENT_ADD,
            args={"agent": "probe", "access": "message"},
        )
        assert added.ok and added.result is not None
        token = str(added.result["token"])
        dromon = str(Path(sys.executable).parent / "dromon")
        server = {
            "type": "stdio",
            "command": dromon,
            "args": [
                "--project",
                str(paths.root),
                "--runtime-dir",
                str(paths.runtime_dir),
                "mcp",
                "--agent",
                "probe",
            ],
            "env": {"DROMON_AGENT_TOKEN": token},
        }

        async def gate(
            name: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            del tool_input, context
            if name.startswith("mcp__dromon__"):
                return PermissionResultAllow()
            return PermissionResultDeny(message="not in this test")

        async def external_agent() -> None:
            options = ClaudeAgentOptions(
                model="claude-haiku-5-5",
                tools=[],
                mcp_servers=cast(Any, {"dromon": server}),
                strict_mcp_config=True,
                setting_sources=[],
                can_use_tool=gate,
                max_turns=4,
                cwd=str(paths.root),
            )
            async with ClaudeSDKClient(options=options) as session:
                await session.query(
                    "Use the send_message tool of the dromon MCP server exactly once,"
                    " to send the line beta this text: Please reply to me with"
                    " send_message containing only the word pong. Then stop."
                )
                async for message in session.receive_response():
                    if isinstance(message, ResultMessage):
                        break

        asyncio.run(external_agent())
        bridge = Bridge(paths.agent_socket, "probe", token)

        def tool(name: str, **arguments: object) -> str:
            answer = bridge.handle(
                {
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            assert answer is not None
            return str(answer["result"])

        def agent_mail_read() -> bool:
            view = client.call(
                paths.socket, Operation.LINE_VIEW, args={"line_id": "beta"}
            )
            assert view.ok and view.result is not None
            messages = cast(list[dict[str, object]], view.result["messages"])
            return any(m["sender"] == "&probe" and m["read"] for m in messages)

        # The agent's message reaches the line, which reads it. Whether the line acts
        # on it is the line's choice, since an agent has no operator authority.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline and not agent_mail_read():
            time.sleep(3)
        assert agent_mail_read(), "beta did not read the agent's message"
        # With operator authority, the operator directs the line to answer the agent.
        sent = client.call(
            paths.socket,
            Operation.SEND,
            args={
                "recipient": "beta",
                "operator": "ana",
                "body": "Use send_message once to send the word pong to &probe.",
            },
        )
        assert sent.ok, sent.error_message
        reply = ""
        while time.monotonic() < deadline and "from beta" not in reply:
            time.sleep(3)
            reply = tool("list_mail")
        assert "from beta" in reply, "beta did not message the agent"
        found = re.search(r"m-[0-9a-f]{12}", reply)
        assert found is not None, reply
        assert "pong" in tool("read_mail", message_id=found.group()).lower()
    finally:
        stop_daemon(paths, pid)
