"""The Codex engine adapter, driven by a scripted app-server instead of Codex.

The tests marked ``live`` reach the real engine on the operator's ChatGPT plan, so
they need both the ``live`` marker selected and ``DROMON_LIVE_TESTS=1``.
"""

import asyncio
import json
import os
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import codex as codex_module
from dromon import evidence
from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.codex import (
    DISABLED_FEATURES,
    CodexLine,
    Connection,
    TOOLS_CHANGED_NOTE,
    default_cache_home,
    split_session,
    engine_environment,
    server_arguments,
    tool_output,
    turn_usage,
    unsupported,
)
from dromon.daemon import check_engines
from dromon.driver import Credential, Driver, Strategy
from dromon.priority import SteeredDriver
from dromon.engine import EnginePlanLimit, EngineRefused, TurnReport
from dromon.evidence import IN, OUT, Recording, Replay, read_recording, replay_factory
from dromon.launcher import (
    LAUNCH_MARK_VARIABLE,
    LAUNCH_VARIABLES,
    STOP_POLICY_VARIABLE,
)
from dromon.lineconfig import LineConfig, config_from_document
from dromon.gitssh import COMMAND_VARIABLE, GitSshAccess, ssh_command
from dromon.model import format_due
from dromon.projectconfig import parse
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineSpec, engine_factory
from dromon.settings import parse_settings, reserved_variable

LUNA = "gpt-6-luna"


def test_first_usage_notice_on_resumed_thread_excludes_inherited_totals() -> None:
    turn = codex_module._Turn()  # pyright: ignore[reportPrivateUsage]
    engine = CodexLine(luna(), CWD)
    engine._usage_seen(  # pyright: ignore[reportPrivateUsage]
        {
            "tokenUsage": {
                "last": {
                    "totalTokens": 100,
                    "inputTokens": 80,
                    "outputTokens": 20,
                },
                "total": {
                    "totalTokens": 1300,
                    "inputTokens": 1080,
                    "outputTokens": 220,
                },
            }
        },
        turn,
    )
    engine._usage_seen(  # pyright: ignore[reportPrivateUsage]
        {
            "tokenUsage": {
                "last": {
                    "totalTokens": 100,
                    "inputTokens": 70,
                    "outputTokens": 30,
                },
                "total": {
                    "totalTokens": 1400,
                    "inputTokens": 1150,
                    "outputTokens": 250,
                },
            }
        },
        turn,
    )
    assert turn_usage(turn) == {
        "input_tokens": 150,
        "output_tokens": 50,
        "total_tokens": 200,
    }


CWD = Path("/work")


def luna(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(LUNA, strategy)


calls: list[dict[str, Any]] = []


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    calls.append(args)
    return {"content": [{"type": "text", "text": f"recorded {args.get('word')}"}]}


RECORD: SdkMcpTool[Any] = SdkMcpTool(
    name="record",
    description="Record a word.",
    input_schema={"type": "object", "properties": {"word": {"type": "string"}}},
    handler=_record,
)


def notice(method: str, **params: object) -> dict[str, Any]:
    return {"method": method, "params": params}


def item(kind: str, **fields: object) -> dict[str, Any]:
    return notice("item/started", item={"type": kind, "id": "i", **fields})


def message(text: str) -> dict[str, Any]:
    return notice(
        "item/completed", item={"type": "agentMessage", "id": "m", "text": text}
    )


def completed(status: str = "completed") -> dict[str, Any]:
    return notice("turn/completed", threadId="th", turn={"id": "t1", "status": status})


class FakeServer:
    """A scripted app-server: answers requests by method and plays the turn's
    messages after ``turn/start``."""

    def __init__(
        self,
        turn: Sequence[dict[str, Any]] = (message("done"), completed()),
        *,
        account: object = {"type": "chatgpt", "planType": "pro", "email": None},
        model: str = LUNA,
        provider: str = "openai",
        servers: Sequence[tuple[str, str, int]] = (("gmail", "disabled", 0),),
        apps: Sequence[object] = (),
        failing: str | None = None,
        failing_error: dict[str, object] | None = None,
    ) -> None:
        self.turn = list(turn)
        self.account = account
        self.model = model
        self.provider = provider
        self.profile: object = {"id": "dromon-line", "extends": ":workspace"}
        self.servers = servers
        self.apps = list(apps)
        self.failing = failing
        self.failing_error = failing_error or {"code": -1, "message": "no"}
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    def params(self, method: str) -> dict[str, Any]:
        return next(m["params"] for m in self.sent if m.get("method") == method)

    def answers(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if "method" not in m]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if not isinstance(method, str) or "id" not in sent:
            return
        if method == self.failing:
            self.queue.append({"id": sent["id"], "error": self.failing_error})
            return
        result = self.result(method)
        self.queue.append({"id": sent["id"], "result": result})
        if method == "turn/start":
            self.queue.append(notice("turn/started", turn={"id": "t1"}))
            self.queue.extend(self.turn)
        if method == "turn/interrupt":
            self.queue.append(completed("interrupted"))

    def result(self, method: str) -> dict[str, Any]:
        if method == "account/read":
            return {"account": self.account, "requiresOpenaiAuth": True}
        if method in ("thread/start", "thread/resume"):
            return {
                "thread": {"id": "th"},
                "model": self.model,
                "modelProvider": self.provider,
                "activePermissionProfile": self.profile,
            }
        if method == "mcpServerStatus/list":
            return {
                "data": [
                    {
                        "name": n,
                        "runtimeStatus": s,
                        "tools": {str(i): {} for i in range(t)},
                    }
                    for n, s, t in self.servers
                ]
            }
        if method == "app/list":
            return {"data": self.apps}
        if method == "turn/start":
            return {"turn": {"id": "t1", "status": "inProgress"}}
        return {}

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return self.queue.popleft() if self.queue else None


def line(
    server: FakeServer,
    *,
    driver: Driver | None = None,
    config: LineConfig | None = None,
    environment: Mapping[str, str] | None = None,
    session_id: str | None = None,
    observe: Observer = ignore,
    credits: list[str] | None = None,
) -> CodexLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del cwd
        server.arguments = arguments
        server.environment = dict(env)
        yield server

    return CodexLine(
        driver or luna(),
        CWD,
        config=config or LineConfig(),
        mcp_tools=[RECORD],
        session_id=session_id,
        environment={} if environment is None else environment,
        connection_factory=connect,
        server_lister=lambda env: ["gmail", "slack"],
        observe=observe,
        credits_spent=(credits if credits is not None else []).append,
    )


def run(target: CodexLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


def test_plan_limit_is_classified_with_reset_and_stops_before_turn() -> None:
    server = FakeServer(
        failing="thread/start",
        failing_error={
            "code": "usage_limit_reached",
            "message": "retry later",
            "data": {"resetsAt": "2026-10-27T08:00:00Z"},
        },
    )
    with pytest.raises(EnginePlanLimit, match="2026-10-27T08:00:00Z"):
        run(line(server))
    assert "turn/start" not in server.methods()


def test_an_unrelated_quota_error_does_not_hold_the_line() -> None:
    server = FakeServer(
        failing="thread/start",
        failing_error={
            "code": -32000,
            "message": "The required parameter quota is missing",
        },
    )
    with pytest.raises(EngineRefused) as raised:
        run(line(server))
    assert not isinstance(raised.value, EnginePlanLimit)


def test_plan_limit_fallback_uses_specific_wording() -> None:
    server = FakeServer(
        failing="thread/start",
        failing_error={"code": -32000, "message": "You have reached your limit"},
    )
    with pytest.raises(EnginePlanLimit) as raised:
        run(line(server))
    # Inferred wording holds the line and never moves a role.
    assert not raised.value.may_move_role


# The shapes the operator observed on 2026-10-27, from a Codex app-server probe with no
# credit left, in the order they arrived.

OBSERVED_MESSAGE = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to"
    " purchase more credits or try again at Nov 1st, 2026 10:08 PM."
)
OBSERVED_ERROR: dict[str, object] = {
    "message": OBSERVED_MESSAGE,
    "codexErrorInfo": "usageLimitExceeded",
}
NO_CREDIT: dict[str, object] = {
    "limitId": "premium",
    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    "primary": None,
    "secondary": None,
    "planType": None,
    "rateLimitReachedType": None,
}


def observed_failure(error: dict[str, object] = OBSERVED_ERROR) -> list[dict[str, Any]]:
    return [
        notice("account/rateLimits/updated", rateLimits=NO_CREDIT),
        notice("thread/status/changed", threadId="th", status={"type": "systemError"}),
        notice("error", threadId="th", turnId="t1", error=error, willRetry=False),
        notice(
            "turn/completed",
            threadId="th",
            turn={"id": "t1", "status": "failed", "error": error},
        ),
    ]


def test_the_observed_usage_limit_ends_the_turn_as_a_plan_limit() -> None:
    credits: list[str] = []
    with pytest.raises(EnginePlanLimit) as raised:
        run(line(FakeServer(observed_failure()), credits=credits))
    # The reset is read in the host's local zone, by the assumption reset_from_message
    # states, so the expected instant is derived the same way.
    local = datetime(2026, 11, 1, 22, 8).astimezone()
    assert raised.value.resets_at == format_due(local)
    assert raised.value.may_move_role
    assert str(raised.value).startswith("Codex usage limit exceeded, which resets")
    # The notice before the failure reported the spent credit at once.
    assert credits == [
        "Codex reported no credit left for its premium limit, with hasCredits and"
        " unlimited false"
    ]


def test_either_report_of_the_usage_limit_suffices() -> None:
    error_only = observed_failure()
    error_only[-1] = completed("failed")
    turn_only = observed_failure()
    del turn_only[2]
    for script in (error_only, turn_only):
        with pytest.raises(EnginePlanLimit):
            run(line(FakeServer(script)))


def test_another_failure_still_fails_the_turn_and_holds_the_line() -> None:
    other: dict[str, object] = {"message": "server error", "codexErrorInfo": "other"}
    report = run(line(FakeServer(observed_failure(other))))
    assert report.subtype == "failed"
    # An error notice for another thread is not the line's.
    foreign = observed_failure()
    foreign[2] = notice("error", threadId="elsewhere", error=OBSERVED_ERROR)
    foreign[-1] = completed("failed")
    assert run(line(FakeServer(foreign))).subtype == "failed"


def test_a_turn_that_completes_after_a_limit_notice_is_not_a_limit() -> None:
    script = [
        notice("error", threadId="th", error=OBSERVED_ERROR, willRetry=True),
        message("done"),
        completed(),
    ]
    assert run(line(FakeServer(script))).text == "done"


def test_only_the_observed_camel_case_code_is_a_usage_limit() -> None:
    assert codex_module.usage_limit(OBSERVED_ERROR) is not None
    others: tuple[object, ...] = (
        {"codexErrorInfo": "usage_limit_exceeded", "message": OBSERVED_MESSAGE},
        {"codexErrorInfo": {"usageLimitExceeded": {}}, "message": OBSERVED_MESSAGE},
        {"message": OBSERVED_MESSAGE},
        "usageLimitExceeded",
        None,
    )
    for error in others:
        assert codex_module.usage_limit(error) is None
    unworded = codex_module.usage_limit({"codexErrorInfo": "usageLimitExceeded"})
    assert unworded is not None and unworded.resets_at is None
    assert str(unworded) == "Codex usage limit exceeded"


def test_the_reset_is_read_from_the_observed_wording_in_a_given_zone() -> None:
    read = codex_module.reset_from_message
    assert read(OBSERVED_MESSAGE, UTC) == "2026-11-01T22:08:00Z"
    pacific = timezone(timedelta(hours=-7))
    assert read(OBSERVED_MESSAGE, pacific) == "2026-11-02T05:08:00Z"
    assert read("try again at Jan 2nd, 2027 12:05 AM.", UTC) == "2027-01-02T00:05:00Z"
    assert read("try again at Mar 3rd, 2027 12:30 PM.", UTC) == "2027-03-03T12:30:00Z"
    assert read("try again at Apr 21st, 2027 1:00 PM.", UTC) == "2027-04-21T13:00:00Z"
    for unread in (
        "try again later.",
        "try again at Feb 30th, 2027 10:08 PM.",
        "try again at Foo 1st, 2027 10:08 PM.",
        "try again at Nov 1st, 2026 13:08 PM.",
        "try again at Nov 1st, 2026 0:08 AM.",
        "try again at Nov 1st, 2026 10:08.",
    ):
        assert read(unread, UTC) is None


def test_only_no_credit_and_no_unlimited_is_a_spent_credit() -> None:
    spent = codex_module.credits_spent
    assert spent({"rateLimits": NO_CREDIT}) is not None
    for credits in (
        {"hasCredits": True, "unlimited": False},
        {"hasCredits": False, "unlimited": True},
        {"hasCredits": False},
        {"hasCredits": "false", "unlimited": False},
        None,
    ):
        assert spent({"rateLimits": {**NO_CREDIT, "credits": credits}}) is None
    assert spent({}) is None


def test_no_credit_with_a_reported_window_is_not_a_spent_credit() -> None:
    """The operator's narrowing of 2026-10-27. Only the observed shape, both windows
    present and null, counts, since an account with a plan window may still have
    allowance left while it has no purchased credit."""
    spent = codex_module.credits_spent
    window: dict[str, object] = {"usedPercent": 40, "windowDurationMins": 300}
    for name in ("primary", "secondary"):
        assert spent({"rateLimits": {**NO_CREDIT, name: window}}) is None
        missing = {key: value for key, value in NO_CREDIT.items() if key != name}
        assert spent({"rateLimits": missing}) is None
    spent_window: dict[str, object] = {"usedPercent": 100, "windowDurationMins": 300}
    assert spent({"rateLimits": {**NO_CREDIT, "primary": spent_window}}) is None


def test_the_unnamed_credit_note() -> None:
    spent = codex_module.credits_spent
    unnamed = spent({"rateLimits": {**NO_CREDIT, "limitId": None}})
    assert unnamed == (
        "Codex reported no credit left, with hasCredits and unlimited false"
    )


def test_app_server_output_close_is_bounded_even_while_process_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_module, "OUTPUT_CLOSE_SECONDS", 0.01)

    class Stdout:
        async def readline(self) -> bytes:
            return b""

    class LiveProcess:
        stdout = Stdout()
        returncode = None

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    async def go() -> None:
        from dromon.codex import _Process  # pyright: ignore[reportPrivateUsage]

        assert await _Process(LiveProcess()).receive() is None  # pyright: ignore[reportArgumentType]

    asyncio.run(go())


# A plain turn.


def test_a_plain_turn_reports_text_credential_and_thread() -> None:
    server = FakeServer()
    target = line(server)
    report = run(target)
    session = f"th:{target.tools_fingerprint()}"
    assert (report.subtype, report.text, report.session_id) == (
        "success",
        "done",
        session,
    )
    assert report.credential is Credential.SUBSCRIPTION
    assert report.cost_estimate_usd is None
    assert target.session_id == session
    assert server.methods()[:3] == ["initialize", "initialized", "account/read"]
    assert server.params("initialize")["capabilities"] == {"experimentalApi": True}


def test_the_thread_is_isolated_and_offers_only_dromons_tools() -> None:
    server = FakeServer()
    run(line(server))
    start = server.params("thread/start")
    assert start["model"] == LUNA
    assert "sandbox" not in start
    assert (
        start["permissions"],
        start["approvalPolicy"],
        start["approvalsReviewer"],
    ) == ("dromon-line", "never", "user")
    assert [t["name"] for t in start["dynamicTools"]] == ["record"]
    assert start["dynamicTools"][0]["inputSchema"] == RECORD.input_schema
    turn = server.params("turn/start")
    assert turn["effort"] == luna().settings.effort
    assert turn["input"] == [{"type": "text", "text": "hello"}]


def test_the_arguments_disable_every_server_feature_and_the_shell() -> None:
    arguments = server_arguments(["slack", "gmail", "gmail"], LineConfig())
    assert arguments[0] == "app-server"
    assert arguments.count("mcp_servers.gmail.enabled=false") == 1
    assert "mcp_servers.slack.enabled=false" in arguments
    with pytest.raises(EngineRefused, match="cannot be disabled"):
        server_arguments(["mail.example"], LineConfig())
    for feature in (*DISABLED_FEATURES, "shell_tool", "unified_exec"):
        assert arguments[arguments.index(feature) - 1] == "--disable"
    assert 'web_search="disabled"' in arguments
    shell = server_arguments([], LineConfig(builtin_tools=("Bash",)))
    assert shell[shell.index("shell_tool") - 1] == "--enable"
    assert shell[shell.index("unified_exec") - 1] == "--enable"


def test_a_line_effort_replaces_the_drivers() -> None:
    server = FakeServer()
    run(line(server, config=LineConfig(effort="max")))
    assert server.params("turn/start")["effort"] == "max"


def test_the_next_turn_resumes_the_thread() -> None:
    server = FakeServer()
    fingerprint = line(FakeServer()).tools_fingerprint()
    run(line(server, session_id=f"th:{fingerprint}"))
    resume = server.params("thread/resume")
    assert resume["threadId"] == "th"
    assert "dynamicTools" not in resume
    assert "thread/start" not in server.methods()
    assert TOOLS_CHANGED_NOTE not in str(server.params("turn/start")["input"])


@pytest.mark.parametrize("stored", ["th", "th:0000000000000000"])
def test_a_thread_started_with_other_tools_is_replaced_and_the_line_told(
    stored: str,
) -> None:
    server = FakeServer()
    target = line(server, session_id=stored)
    report = run(target)
    assert "thread/resume" not in server.methods()
    start = server.params("thread/start")
    assert [t["name"] for t in start["dynamicTools"]] == ["record"]
    text = server.params("turn/start")["input"][0]["text"]
    assert text.startswith(TOOLS_CHANGED_NOTE)
    assert report.session_id == f"th:{target.tools_fingerprint()}"


def test_the_fingerprint_follows_the_tool_names_only() -> None:
    assert split_session(None) == (None, None)
    assert split_session("th") == ("th", None)
    assert split_session("th:abc") == ("th", "abc")
    first = line(FakeServer()).tools_fingerprint()
    assert first == line(FakeServer()).tools_fingerprint()
    assert len(first) == 16


# Requests from the engine.


def test_dromons_tools_are_served_and_counted() -> None:
    calls.clear()
    server = FakeServer(
        [
            item("dynamicToolCall", tool="record"),
            {
                "id": "s1",
                "method": "item/tool/call",
                "params": {"tool": "record", "arguments": {"word": "heron"}},
            },
            {
                "id": "s2",
                "method": "item/tool/call",
                "params": {"tool": "missing", "arguments": {}},
            },
            message("ok"),
            completed(),
        ]
    )
    report = run(line(server))
    assert calls == [{"word": "heron"}]
    assert report.tool_uses == ("record",)
    first, second = server.answers()[:2]
    assert first == {
        "id": "s1",
        "result": {
            "success": True,
            "contentItems": [{"type": "inputText", "text": "recorded heron"}],
        },
    }
    assert second["result"]["success"] is False


def test_the_turn_is_reported_to_the_observer_as_it_runs() -> None:
    server = FakeServer(
        [
            item("reasoning"),
            item("dynamicToolCall", tool="record"),
            message("first"),
            message("second"),
            completed(),
        ]
    )
    log = ActivityLog()
    run(line(server, observe=log.observer("alpha")))
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TOOL, "record"),
        (ActivityKind.TEXT, "first"),
        (ActivityKind.TEXT, "second"),
    ]


def test_approvals_are_declined_and_other_requests_refused() -> None:
    server = FakeServer(
        [
            {
                "id": "a1",
                "method": "item/commandExecution/requestApproval",
                "params": {},
            },
            {"id": "a2", "method": "item/fileChange/requestApproval", "params": {}},
            {"id": "a3", "method": "item/tool/requestUserInput", "params": {}},
            completed(),
        ]
    )
    run(line(server))
    answers = {a["id"]: a for a in server.answers()}
    assert answers["a1"]["result"] == {"decision": "decline"}
    assert answers["a2"]["result"] == {"decision": "decline"}
    assert answers["a3"]["error"]["code"] == -32601


def test_tool_output_maps_text_and_errors() -> None:
    assert tool_output(
        {"content": [{"type": "text", "text": "x"}, {"type": "image"}]}
    ) == {
        "success": True,
        "contentItems": [{"type": "inputText", "text": "x"}],
    }
    assert tool_output({"content": [], "is_error": True})["success"] is False


# A recording replayed through the real adapter.


class Tap:
    """The scripted app-server, recorded as the app-server's own process records."""

    def __init__(self, server: FakeServer, recording: Recording) -> None:
        self.server = server
        self.recording = recording

    async def send(self, message: Mapping[str, object]) -> None:
        await self.server.send(message)
        self.recording.frame(OUT, message)

    async def receive(self) -> dict[str, Any] | None:
        found = await self.server.receive()
        if found is not None:
            self.recording.frame(IN, found)
        return found


def test_a_recorded_turn_replays_through_the_real_adapter(tmp_path: Path) -> None:
    """A Codex probe records this exchange, and the recording then stands in for
    Codex, so the adapter gives the same report without the engine."""
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, codex_module.ENGINE_NAME, LUNA)
    server = FakeServer(
        (item("dynamicToolCall", tool="record"), message("done"), completed())
    )

    @asynccontextmanager
    async def tapped(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield Tap(server, recording)

    def target(factory: Any) -> CodexLine:  # noqa: ANN401
        return CodexLine(
            luna(),
            CWD,
            config=LineConfig(),
            mcp_tools=[RECORD],
            environment={},
            connection_factory=factory,
            server_lister=lambda env: ["gmail"],
        )

    live = run(target(tapped))
    recording.close()
    assert (live.text, live.tool_uses) == ("done", ("record",))
    replay = Replay(read_recording(path), wait_seconds=1.0)
    replayed = run(target(replay_factory(replay)))
    assert replay.finished()
    assert replay.sent == replay.recorded_out
    assert (replayed.subtype, replayed.text, replayed.tool_uses) == (
        live.subtype,
        live.text,
        live.tool_uses,
    )
    assert (replayed.credential, replayed.session_id) == (
        live.credential,
        live.session_id,
    )


STAND_IN = """
import json, sys
sys.stderr.write("token=abc123 replaying\\n")
sys.stderr.flush()
with open(sys.argv[0] + ".jsonl", encoding="utf-8") as handle:
    documents = [json.loads(line) for line in handle][1:]
for document in documents:
    if document.get("kind") != "frame":
        continue
    if document["dir"] == "out":
        line = sys.stdin.readline()
        if not line or json.loads(line) != document["frame"]:
            sys.exit(3)
    else:
        sys.stdout.write(json.dumps(document["frame"]) + "\\n")
        sys.stdout.flush()
"""
"""A stand-in for ``codex app-server`` that plays the recording beside it, checks each
frame the adapter sends against the recorded one, and stops at the first that
differs, so the turn then fails."""


def test_a_recorded_turn_replays_through_the_process_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-27, P1. The other replay replaces the connection, so the
    process wrapper, its standard error drain and the launcher never run. Here the
    recording stands in for the engine's process, and the wrapper records the
    replayed exchange as it would a live one."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    directory = tmp_path / "bin"
    directory.mkdir()
    stand_in = directory / "codex"
    server = FakeServer(
        (item("dynamicToolCall", tool="record"), message("done"), completed())
    )
    first = Recording(stand_in.with_name("codex.jsonl"), codex_module.ENGINE_NAME, LUNA)

    @asynccontextmanager
    async def tapped(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield Tap(server, first)

    def target(factory: codex_module.ConnectionFactory) -> CodexLine:
        return CodexLine(
            luna(),
            workspace,
            config=LineConfig(),
            mcp_tools=[RECORD],
            environment={},
            connection_factory=factory,
            server_lister=lambda env: ["gmail"],
        )

    live = run(target(tapped))
    first.close()
    stand_in.write_text(f"#!{sys.executable}\n{STAND_IN}")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    second = Recording(
        tmp_path / "again.jsonl", codex_module.ENGINE_NAME, LUNA, stderr=True
    )
    token = evidence.RECORDING.set(second)
    try:
        replayed = run(target(codex_module._app_server))  # pyright: ignore[reportPrivateUsage]
    finally:
        evidence.RECORDING.reset(token)
        second.close()
    assert (replayed.subtype, replayed.text, replayed.tool_uses) == (
        live.subtype,
        live.text,
        live.tool_uses,
    )
    assert (replayed.credential, replayed.session_id) == (
        live.credential,
        live.session_id,
    )
    again = read_recording(second.path)
    assert again.frames == read_recording(stand_in.with_name("codex.jsonl")).frames
    assert again.stderr is not None and "token=***" in again.stderr
    assert "abc123" not in again.stderr


# Credentials, models and isolation.


def test_a_subscription_driver_refuses_an_api_key_before_any_thread() -> None:
    server = FakeServer(account={"type": "apiKey"})
    with pytest.raises(EngineRefused, match="subscription driver refuses the api"):
        run(line(server))
    assert "thread/start" not in server.methods()


def test_an_api_driver_accepts_an_api_key() -> None:
    report = run(
        line(FakeServer(account={"type": "apiKey"}), driver=luna(Strategy.API))
    )
    assert report.credential is Credential.API


@pytest.mark.parametrize("account", [None, {"type": "amazonBedrock"}, "chatgpt"])
def test_an_unrecognized_account_is_refused(account: object) -> None:
    with pytest.raises(EngineRefused, match="unrecognized account"):
        run(line(FakeServer(account=account)))


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (FakeServer(model="gpt-6-astra"), "refuses the engine model 'gpt-6-astra'"),
        (FakeServer(provider="azure"), "model provider 'azure'"),
        (
            FakeServer(servers=[("gmail", "connected", 23)]),
            "MCP servers the line was not given: gmail",
        ),
        (
            FakeServer(servers=[("gmail", "disabled", 1)]),
            "MCP servers the line was not given",
        ),
        (
            FakeServer(servers=[("gmail", "notStarted", 0)]),
            "MCP servers the line was not given",
        ),
        (FakeServer(apps=[{"id": "drive"}]), "offers apps"),
    ],
)
def test_an_engine_that_breaks_isolation_is_refused_before_the_turn(
    server: FakeServer, reason: str
) -> None:
    with pytest.raises(EngineRefused, match=reason):
        run(line(server))
    assert "turn/start" not in server.methods()


def test_a_tool_the_line_was_not_given_interrupts_and_refuses_the_turn() -> None:
    server = FakeServer([item("mcpToolCall", server="gmail", tool="send")])
    with pytest.raises(EngineRefused, match="not given: mcpToolCall"):
        run(line(server))
    assert server.params("turn/interrupt") == {"threadId": "th", "turnId": "t1"}


def test_the_shell_counts_as_bash_only_for_a_line_given_bash() -> None:
    with pytest.raises(EngineRefused, match="not given: commandExecution"):
        run(line(FakeServer([item("commandExecution", command="ls")])))
    server = FakeServer([item("commandExecution", command="ls"), completed()])
    report = run(
        line(
            server,
            config=LineConfig(builtin_tools=("Bash",)),
            environment={"SHELL": "/bin/bash"},
        )
    )
    assert report.tool_uses == ("Bash",)


def test_a_reroute_to_another_model_interrupts_and_refuses_the_turn() -> None:
    server = FakeServer(
        [notice("model/rerouted", fromModel=LUNA, toModel="gpt-6-mini")]
    )
    with pytest.raises(
        EngineRefused, match="rerouted the turn to the model 'gpt-6-mini'"
    ):
        run(line(server))
    assert "turn/interrupt" in server.methods()


def test_the_driver_budget_interrupts_a_turn_once() -> None:
    budget = luna().settings.tool_budget
    server = FakeServer([item("dynamicToolCall", tool="record")] * (budget + 3))
    report = run(line(server))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "interrupted"
    assert server.methods().count("turn/interrupt") == 1


def test_a_closed_connection_or_failed_request_refuses_the_turn() -> None:
    with pytest.raises(EngineRefused, match="closed its connection"):
        run(line(FakeServer([message("partial")])))
    with pytest.raises(EngineRefused, match="refused a request: thread/start"):
        run(line(FakeServer(failing="thread/start")))


# Environment and configuration.


KEYS = {"OPENAI_API_KEY": "k", "CODEX_API_KEY": "k", "CODEX_ACCESS_TOKEN": "k"}


def test_a_subscription_lines_codex_never_sees_an_api_key() -> None:
    environment = {
        **KEYS,
        "PATH": "/bin",
        "DAEMON_ONLY_SENTINEL": "must-not-reach-engine",
    }
    cache = {"XDG_CACHE_HOME": default_cache_home()}
    expected = {"PATH": "/bin", **cache}
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == expected
    for metered in (Strategy.API, Strategy.FLEXIBLE):
        assert engine_environment(environment, metered) == {
            **KEYS,
            "PATH": "/bin",
            **cache,
        }
    server = FakeServer()
    run(line(server, environment=environment))
    assert "DAEMON_ONLY_SENTINEL" not in server.environment
    # The launcher's own variables, which it removes before Codex starts.
    assert server.environment[STOP_POLICY_VARIABLE] == "kill_all"
    launched = {
        k: v for k, v in server.environment.items() if k not in LAUNCH_VARIABLES
    }
    assert launched == expected


def test_codex_shell_is_explicit_for_lines_with_bash() -> None:
    environment = {"PATH": "/bin", "SHELL": "/bin/bash"}
    selected = engine_environment(
        environment, Strategy.SUBSCRIPTION, require_shell=True
    )
    assert selected["SHELL"] == "/bin/bash"
    server = FakeServer()
    run(
        line(
            server,
            config=LineConfig(builtin_tools=("Bash",)),
            environment=environment,
        )
    )
    assert 'shell_environment_policy.set.SHELL="/bin/bash"' in server.arguments


def test_codex_bash_refuses_missing_shell() -> None:
    server = FakeServer()
    with pytest.raises(
        EngineRefused, match="Codex shell unavailable: SHELL is not set"
    ):
        run(line(server, config=LineConfig(builtin_tools=("Bash",))))
    assert server.sent == []


def test_a_redirected_provider_is_always_refused() -> None:
    for strategy in Strategy:
        with pytest.raises(EngineRefused, match="OPENAI_BASE_URL"):
            engine_environment({"OPENAI_BASE_URL": "https://x"}, strategy)
    server = FakeServer()
    with pytest.raises(EngineRefused, match="OPENAI_BASE_URL"):
        run(line(server, environment={"OPENAI_BASE_URL": "https://x"}))
    assert server.sent == []


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (
            LineConfig(builtin_tools=("Read", "Bash")),
            "only the built-in tools Bash, Edit and Write, not Read",
        ),
        (LineConfig(inherit_account_mcp=True), "do not yet support MCP servers"),
        (
            LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True)),
            "Dromon's Codex permission profile",
        ),
        (
            LineConfig(settings=parse_settings({"env": {"A": "1"}})),
            "Dromon's Codex permission profile",
        ),
    ],
)
def test_a_configuration_codex_cannot_run_is_refused(
    config: LineConfig, reason: str
) -> None:
    assert reason in (unsupported(config) or "")
    with pytest.raises(EngineRefused, match=reason):
        run(line(FakeServer(), config=config))
    assert unsupported(LineConfig(builtin_tools=("Bash",))) is None


def test_openai_codex_and_xai_variables_are_reserved() -> None:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "CODEX_HOME",
        "XAI_API_KEY",
    ):
        assert reserved_variable(name)


def test_the_codex_adapter_refuses_a_driver_for_another_engine() -> None:
    claude = Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the claude-code engine"):
        CodexLine(claude, CWD)


def test_the_factory_builds_each_line_on_its_drivers_engine() -> None:
    build = engine_factory(str(CWD))
    engine = build(LineSpec("alpha", LUNA, Strategy.SUBSCRIPTION), [RECORD])
    assert isinstance(engine, CodexLine)
    assert list(engine.mcp_tools) == ["record"]
    sandboxed = engine_factory(
        str(CWD), project_sandbox=parse_sandbox({"enabled": True}, operator=False)
    )
    with pytest.raises(EngineRefused, match="project file sets one"):
        sandboxed(LineSpec("alpha", LUNA, Strategy.SUBSCRIPTION), [])


def test_the_daemon_refuses_project_tables_that_would_reach_a_codex_line() -> None:
    check_engines(parse("version = 1\n[lines.a]\nmodel = 'gpt-6-luna'\n"))
    check_engines(
        parse(
            "version = 1\n[sandbox]\nenabled = true\n"
            "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
        )
    )
    with pytest.raises(ValueError, match="which run on engines other than Claude Code"):
        check_engines(
            parse(
                "version = 1\n[settings.env]\nA = '1'\n"
                "[lines.a]\nmodel = 'gpt-6-luna'\n"
            )
        )


def test_a_codex_line_gets_its_instructions_as_developer_instructions(
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "CLAUDE.md").write_text("Follow the project rules.\n")
    server = FakeServer()
    target = line(server, config=LineConfig(instructions=("CLAUDE.md",)))
    target.cwd = root
    asyncio.run(target.run_turn("hello"))
    assert server.params("thread/start")["developerInstructions"] == (
        "Contents of CLAUDE.md:\n\nFollow the project rules."
    )
    plain = FakeServer()
    asyncio.run(line(plain).run_turn("hello"))
    assert "developerInstructions" not in plain.params("thread/start")
    reason = unsupported(LineConfig(claude_code_prompt=True))
    assert reason is not None and "Codex's own instructions" in reason


# The permission profile.


def profile_of(arguments: list[str]) -> str:
    entry = next(a for a in arguments if a.startswith("permissions.dromon-line="))
    return entry.removeprefix("permissions.dromon-line=")


def test_the_profile_writes_only_with_a_write_tool_and_never_git(
    tmp_path: Path,
) -> None:
    import tomllib

    def parsed(config: LineConfig) -> dict[str, Any]:
        text = profile_of(server_arguments([], config, (tmp_path,)))
        return tomllib.loads(f"profile = {text}")["profile"]

    reader = parsed(LineConfig(builtin_tools=("Bash",)))
    assert reader["extends"] == ":workspace"
    assert reader["filesystem"][":workspace_roots"] == {".": "read", ".git": "read"}
    for tools in (("Edit",), ("Write",), ("Bash", "Edit")):
        writer = parsed(LineConfig(builtin_tools=tools))
        assert writer["filesystem"][":workspace_roots"] == {
            ".": "write",
            ".git": "read",
        }


def test_a_read_only_workdir_profile_keeps_tmp_writable() -> None:
    import tomllib

    config = config_from_document(
        {"builtin_tools": ["Bash", "Edit", "Write"], "workdir_access": "read"}
    )
    text = profile_of(server_arguments([], config, ()))
    profile = tomllib.loads(f"profile = {text}")["profile"]
    assert profile["filesystem"][":workspace_roots"] == {
        ".": "read",
        ".git": "read",
        "tmp": "write",
    }


def test_the_profile_denies_each_protected_directory_that_exists(
    tmp_path: Path,
) -> None:
    import os
    import tomllib

    state = tmp_path / "project" / ".dromon"
    state.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(state)
    missing = tmp_path / "missing"
    text = profile_of(server_arguments([], LineConfig(), (link, missing)))
    filesystem = tomllib.loads(f"profile = {text}")["profile"]["filesystem"]
    assert filesystem[os.path.realpath(state)] == "deny"
    assert str(link) not in filesystem and str(missing) not in filesystem
    odd = tmp_path / "bad\nname"
    odd.mkdir()
    with pytest.raises(EngineRefused, match="control character"):
        server_arguments([], LineConfig(), (odd,))


def test_a_thread_without_dromons_profile_is_refused() -> None:
    for profile in (None, {"id": ":workspace"}, "dromon-line"):
        server = FakeServer()
        server.profile = profile
        with pytest.raises(EngineRefused, match="permission profile"):
            run(line(server))
        assert "turn/start" not in server.methods()


def test_a_file_change_is_tool_use_only_for_a_line_given_a_write_tool() -> None:
    with pytest.raises(EngineRefused, match="not given: fileChange"):
        run(line(FakeServer([item("fileChange")])))
    server = FakeServer([item("fileChange"), completed()])
    report = run(line(server, config=LineConfig(builtin_tools=("Edit",))))
    assert report.tool_uses == ("Edit",)


# Real turns, on the operator's ChatGPT plan.


def _live() -> None:
    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")


@pytest.mark.live
def test_a_live_luna_line_keeps_its_thread_and_tools_across_turns(
    tmp_path: Path,
) -> None:
    _live()
    calls.clear()
    target = CodexLine(luna(), tmp_path.resolve(), mcp_tools=[RECORD])
    first = asyncio.run(
        target.run_turn("Remember the word heron. Use no tool. Reply with the word ok.")
    )
    assert first.credential is Credential.SUBSCRIPTION
    assert first.subtype == "success"
    second = asyncio.run(
        target.run_turn(
            "Call the record tool once, with the word you were asked to remember."
            " Then reply with the word done."
        )
    )
    assert second.session_id == first.session_id, "the second turn did not resume"
    assert second.tool_uses == ("record",)
    assert calls == [{"word": "heron"}]


@pytest.mark.live
def test_a_live_luna_line_writes_the_workspace_but_not_protected_paths(
    tmp_path: Path,
) -> None:
    _live()
    project = tmp_path.resolve()
    state = project / ".dromon"
    state.mkdir()
    (state / "dromon.toml").write_text("secret = true\n")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    target = CodexLine(
        luna(),
        project,
        config=LineConfig(builtin_tools=("Bash", "Edit")),
        protected=(state,),
    )
    prompt = (
        "Do these four steps in order, each once, and do not retry a step that fails."
        " 1. With your file editing tool, create notes.txt containing hello."
        " 2. With your file editing tool, create .dromon/probe.txt containing x."
        " 3. With your file editing tool, create .git/probe.txt containing x."
        " 4. With the shell, run: cat .dromon/dromon.toml"
        " Then reply with one line per step saying whether it succeeded."
    )
    report = asyncio.run(target.run_turn(prompt))
    assert report.subtype == "success"
    assert (project / "notes.txt").read_text().strip() == "hello"
    assert not (state / "probe.txt").exists(), "the line wrote Dromon's state"
    assert not (project / ".git" / "probe.txt").exists(), "the line wrote .git"
    assert "secret = true" not in report.text, "the line read Dromon's state"
    assert "Edit" in report.tool_uses


def test_the_mark_reaches_codexs_shell_commands_and_not_codex_itself() -> None:
    server = FakeServer()
    target = line(server, environment={"PATH": "/bin"})
    target.mark = "alpha.token"
    run(target)
    assert "DROMON_LINE_MARK" not in server.environment
    # Only for the launcher, which names the line by it and removes it.
    assert server.environment[LAUNCH_MARK_VARIABLE] == "alpha.token"
    assert LAUNCH_MARK_VARIABLE in LAUNCH_VARIABLES
    pairs = list(zip(server.arguments, server.arguments[1:], strict=False))
    assert (
        "-c",
        'shell_environment_policy.set.DROMON_LINE_MARK="alpha.token"',
    ) in pairs
    unmarked = FakeServer()
    run(line(unmarked, environment={"PATH": "/bin"}))
    assert not any("DROMON_LINE_MARK" in a for a in unmarked.arguments)
    assert reserved_variable("DROMON_LINE_MARK")
    assert server_arguments([], LineConfig(), (), "a.b")[1:3] == [
        "-c",
        'shell_environment_policy.set.DROMON_LINE_MARK="a.b"',
    ]


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    steered: Steered,
) -> None:
    driver, asked = steered(LUNA, Strategy.SUBSCRIPTION)
    finished = notice(
        "item/completed", item={"type": "dynamicToolCall", "id": "i", "tool": "record"}
    )
    server = FakeServer(
        [message("I will record it"), item("dynamicToolCall", tool="record"), finished]
    )
    report = run(line(server, driver=driver))
    assert asked == [1]
    assert report.interrupted_for == REASON
    assert server.methods().count("turn/interrupt") == 1


def test_a_finished_item_that_is_no_tool_is_not_a_safe_point(steered: Steered) -> None:
    driver, asked = steered(LUNA, Strategy.SUBSCRIPTION)
    server = FakeServer(
        [item("reasoning"), message("one"), message("two"), completed()]
    )
    report = run(line(server, driver=driver))
    assert asked == []
    assert report.interrupted_for is None


GIT_SSH = GitSshAccess(
    "/run/agent.sock", "/keys/known_hosts", "/keys/deploy.pub", "0" * 64, "0" * 64
)
"""A resolved ``[git_ssh]`` agent, which no test here connects to."""


def test_a_codex_line_given_git_ssh_sets_it_for_its_shell_commands_alone() -> None:
    """As the mark does, through ``shell_environment_policy.set``, so Codex's own
    policy cannot drop it and Codex's own processes do not carry it."""
    server = FakeServer()
    target = line(server, environment={"PATH": "/bin", COMMAND_VARIABLE: "x"})
    target.git_ssh = GIT_SSH
    run(target)
    pairs = list(zip(server.arguments, server.arguments[1:], strict=False))
    value = json.dumps(ssh_command(GIT_SSH))
    assert ("-c", f"shell_environment_policy.set.{COMMAND_VARIABLE}={value}") in pairs
    assert COMMAND_VARIABLE not in server.environment
    plain = FakeServer()
    run(line(plain, environment={"PATH": "/bin", COMMAND_VARIABLE: "x"}))
    assert not any(COMMAND_VARIABLE in argument for argument in plain.arguments)
    assert COMMAND_VARIABLE not in plain.environment
    with pytest.raises(EngineRefused, match="not a bare TOML key"):
        server_arguments([], LineConfig(), (), None, {"A.B": "x"})
