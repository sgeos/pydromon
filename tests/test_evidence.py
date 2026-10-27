"""Row 7 of the T3 Code review. Probe evidence: recording, replay, and minimum
versions. Every engine here is a scripted fake or a recording, so no subscription is
spent."""

import asyncio
import json
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._internal.transport import Transport

from dromon import cli, evidence, exits
from dromon.capabilities import (
    CAPABILITY_NAMES,
    CLAUDE_CODE,
    TABLES,
    Known,
    below,
    isolation_of,
    kept_session_refusal,
    report_lines,
    table_of,
    version_numbers,
    version_refusal,
)
from dromon.cursor import CursorLine
from dromon.cursor import unsupported as unsupported_cursor
from dromon.pi import unsupported as unsupported_pi
from dromon.driver import Credential, Driver, Strategy
from dromon.engine import (
    ClaudeCodeLine,
    EngineRefused,
    RecordedTransport,
    TurnReport,
    replay_session,
    transport_session,
)
from dromon.evidence import (
    IN,
    OUT,
    Recording,
    RecordingError,
    Replay,
    ReplayDiverged,
    ReplayStart,
    read_recording,
    replay_factory,
)
from dromon.jsonlines import json_lines_factory
from dromon.lineconfig import LineConfig

HAIKU = "claude-haiku-5-5"
CURSOR = "cursor-agent:gpt-5.6-luna-medium"
DISPLAY = "GPT-5.6 Luna 1M"


def recorded(tmp_path: Path, frames: list[tuple[str, dict[str, Any]]]) -> Path:
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, "test-engine", "test-model")
    for direction, frame in frames:
        recording.frame(direction, frame)
    recording.close()
    return path


# The file.


def test_a_recording_is_private_and_reads_back(tmp_path: Path) -> None:
    path = recorded(tmp_path, [(OUT, {"id": 1}), (IN, {"id": 1, "ok": True})])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    found = read_recording(path)
    assert found.header["engine"] == "test-engine"
    assert found.header["model"] == "test-model"
    assert found.frames == [(OUT, {"id": 1}), (IN, {"id": 1, "ok": True})]
    assert not found.truncated and found.stderr is None
    with pytest.raises(FileExistsError):
        Recording(path, "again", "again")


def test_standard_error_is_bounded_and_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_STDERR_BYTES", 64)
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, "e", "m", stderr=True)
    recording.stderr(b"Authorization: Bearer sk-secret-value\n")
    recording.stderr(b"x" * 100)
    recording.close()
    found = read_recording(path)
    assert found.stderr is not None
    assert "sk-secret-value" not in found.stderr
    assert "Authorization: ***" in found.stderr
    assert len(found.stderr.encode()) <= 64
    assert json.loads(path.read_text().splitlines()[-1])["cut"] is True


def test_standard_error_is_not_kept_unless_asked(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, "e", "m")
    recording.stderr(b"anything")
    recording.close()
    assert read_recording(path).stderr is None


def test_a_recording_past_its_bound_is_marked_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_FRAMES", 2)
    path = recorded(tmp_path, [(OUT, {"n": n}) for n in range(5)])
    found = read_recording(path)
    assert len(found.frames) == 2 and found.truncated


def test_a_frame_goes_only_out_or_in(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "p.jsonl", "e", "m")
    with pytest.raises(ValueError, match="out or in"):
        recording.frame("sideways", {})
    recording.close()


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "is empty"),
        (["not json"], ":1: not JSON"),
        (["[1]"], ":1: not an object"),
        (['{"kind": "header", "v": 2}'], "not a version 1 header"),
        (['{"kind": "frame", "dir": "in", "frame": {}}'], "not a version 1 header"),
        ([None, '{"kind": "frame", "dir": "up", "frame": {}}'], ":2: a malformed"),
        ([None, '{"kind": "frame", "dir": "in", "frame": []}'], ":2: a malformed"),
        ([None, '{"kind": "odd"}'], ":2: an unknown record"),
        (
            [
                None,
                '{"kind": "truncated", "frames": 0}',
                '{"kind": "frame", "dir": "in", "frame": {}}',
            ],
            ":3: a frame after truncation",
        ),
        (
            [
                None,
                '{"kind": "stderr", "text": "", "cut": false}',
                '{"kind": "truncated", "frames": 0}',
            ],
            ":3: a record after standard error",
        ),
    ],
)
def test_a_malformed_recording_is_refused_where_it_fails(
    tmp_path: Path, lines: list[str | None], message: str
) -> None:
    header = json.dumps({"v": 1, "kind": "header", "engine": "e", "model": "m"})
    path = tmp_path / "bad.jsonl"
    path.write_text("".join(f"{header if line is None else line}\n" for line in lines))
    with pytest.raises(RecordingError, match=message):
        read_recording(path)


def test_a_missing_recording_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RecordingError, match="cannot read"):
        read_recording(tmp_path / "absent.jsonl")


def test_the_probe_path_is_under_tmp_probes(tmp_path: Path) -> None:
    path = evidence.probe_path(tmp_path, "pi")
    assert path.parent == tmp_path / "tmp" / "probes"
    assert path.name.startswith("pi-") and path.suffix == ".jsonl"


# Replay keeps the order of the exchange.


def test_a_replay_gives_each_frame_once_the_adapter_has_earned_it(
    tmp_path: Path,
) -> None:
    path = recorded(
        tmp_path,
        [(IN, {"hello": 1}), (OUT, {"ask": 1}), (IN, {"answer": 1}), (OUT, {"bye": 1})],
    )
    replay = Replay(read_recording(path), wait_seconds=1.0)

    async def go() -> list[dict[str, Any] | None]:
        got = [await replay.receive()]
        replay.send({"ask": 1})
        got.append(await replay.receive())
        replay.send({"bye": 1})
        got.append(await replay.receive())
        return got

    assert asyncio.run(go()) == [{"hello": 1}, {"answer": 1}, None]
    assert replay.finished()
    assert replay.sent == [{"ask": 1}, {"bye": 1}]


def test_an_adapter_that_waits_for_an_unearned_frame_diverges(tmp_path: Path) -> None:
    path = recorded(tmp_path, [(OUT, {"ask": 1}), (IN, {"answer": 1})])
    replay = Replay(read_recording(path), wait_seconds=0.05)
    with pytest.raises(ReplayDiverged, match="has sent 0"):
        asyncio.run(replay.receive())
    assert not replay.finished()


def test_an_adapter_that_sends_too_much_diverges(tmp_path: Path) -> None:
    replay = Replay(read_recording(recorded(tmp_path, [(OUT, {"a": 1})])))
    replay.send({"a": 1})
    with pytest.raises(ReplayDiverged, match="holds 1"):
        replay.send({"b": 2})


# Recording through the engine's own process.

HARNESS = (
    "import json, sys\n"
    "sys.stderr.write('token=abc123 starting\\n')\n"
    "sys.stderr.flush()\n"
    "request = json.loads(sys.stdin.readline())\n"
    "sys.stdout.write(json.dumps({'answer': request['ask']}) + '\\n')\n"
    "sys.stdout.flush()\n"
)


def test_a_json_lines_engine_is_recorded_with_its_standard_error(
    tmp_path: Path,
) -> None:
    """The Pi, Cursor and Antigravity adapters start their engine this way, so a
    recording made here is what their probes record."""
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "harness"
    script.write_text(f"#!{sys.executable}\n{HARNESS}")
    script.chmod(0o755)
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, "harness", "m", stderr=True)

    async def go() -> dict[str, Any] | None:
        factory = json_lines_factory("harness", keep_input=True)
        async with factory([], tmp_path, {"PATH": str(directory)}) as harness:
            await harness.send({"ask": 42})
            return await harness.receive()

    token = evidence.RECORDING.set(recording)
    try:
        assert asyncio.run(go()) == {"answer": 42}
    finally:
        evidence.RECORDING.reset(token)
        recording.close()
    found = read_recording(path)
    assert found.frames == [(OUT, {"ask": 42}), (IN, {"answer": 42})]
    assert found.stderr is not None
    assert "token=***" in found.stderr and "abc123" not in found.stderr


def test_nothing_is_recorded_without_a_recording(tmp_path: Path) -> None:
    assert evidence.RECORDING.get() is None
    evidence.record(OUT, {"a": 1})
    assert list(tmp_path.iterdir()) == []


# Replay through the real Cursor adapter.


def cursor_events() -> list[dict[str, Any]]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "login",
            "cwd": "/w",
            "session_id": "chat-1",
            "model": DISPLAY,
            "permissionMode": "default",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ready"}],
            },
            "session_id": "chat-1",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ready",
            "session_id": "chat-1",
        },
    ]


def cursor_line(tmp_path: Path, factory: Any) -> CursorLine:  # noqa: ANN401
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return CursorLine(
        Driver.for_model(CURSOR, Strategy.SUBSCRIPTION),
        work,
        config=LineConfig(),
        mcp_tools=[],
        session_id=None,
        bridge=[],
        line_token=None,
        environment={"PATH": "/usr/bin"},
        home=home,
        connection_factory=factory,
        partial_isolation=True,
    )


def test_a_cursor_recording_replays_through_the_real_adapter(tmp_path: Path) -> None:
    path = recorded(tmp_path, [(IN, event) for event in cursor_events()])
    replay = Replay(read_recording(path), wait_seconds=1.0)
    starts: list[ReplayStart] = []
    target = cursor_line(tmp_path, replay_factory(replay, starts))
    report = asyncio.run(target.run_turn("Reply with the single word: ready"))
    assert (report.subtype, report.text) == ("success", "ready")
    assert report.credential is Credential.SUBSCRIPTION
    assert replay.finished()
    assert starts[0].arguments[-1] == "Reply with the single word: ready"


def test_a_cursor_recording_that_ends_early_fails_the_turn(tmp_path: Path) -> None:
    """A recording cut before its result is replayed as a stream that ended, and the
    adapter judges it as it would a live one."""
    path = recorded(tmp_path, [(IN, event) for event in cursor_events()[:2]])
    target = cursor_line(tmp_path, replay_factory(Replay(read_recording(path))))
    with pytest.raises(EngineRefused, match="ended without a result"):
        asyncio.run(target.run_turn("hello"))


# Recording and replay through the real Claude Code adapter and Agent SDK client.


class ScriptedCli(Transport):
    """Claude Code's side of the control protocol, scripted: it answers each control
    request with success, and a user message with an init, a reply and a result."""

    def __init__(self, version: str = "2.1.310", *, tools: bool = True) -> None:
        self.version = version
        self.tools = tools
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.ready = False

    async def connect(self) -> None:
        self.ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            frame: dict[str, Any] = json.loads(line)
            if frame.get("type") == "control_request":
                self.queue.put_nowait(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": frame["request_id"],
                            "response": {},
                        },
                    }
                )
            elif frame.get("type") == "user":
                for message in self.turn():
                    self.queue.put_nowait(message)

    def turn(self) -> list[dict[str, Any]]:
        init: dict[str, Any] = {
            "type": "system",
            "subtype": "init",
            "model": HAIKU,
            "apiKeySource": "none",
            "claude_code_version": self.version,
            "permissionMode": "default",
            "session_id": "session-1",
        }
        if self.tools:
            init["tools"] = []
        return [
            init,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": HAIKU,
                    "content": [{"type": "text", "text": "ready"}],
                },
                "parent_tool_use_id": None,
                "session_id": "session-1",
            },
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": "session-1",
                "total_cost_usd": 0.001,
            },
        ]

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self.queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self.ready = False
        self.queue.put_nowait(None)

    def is_ready(self) -> bool:
        return self.ready

    async def end_input(self) -> None:
        return


def claude_line(factory: Any) -> ClaudeCodeLine:  # noqa: ANN401
    return ClaudeCodeLine(
        Driver.for_model(HAIKU, Strategy.SUBSCRIPTION),
        Path("/work"),
        environment={},
        session_factory=factory,
    )


def test_a_claude_code_turn_records_and_replays_through_the_real_adapter(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claude.jsonl"
    recording = Recording(path, "claude-code", HAIKU)
    live = claude_line(
        transport_session(lambda: RecordedTransport(ScriptedCli(), recording))
    )
    first = asyncio.run(live.run_turn("Reply with the single word: ready"))
    recording.close()
    found = read_recording(path)
    directions = [direction for direction, _ in found.frames]
    assert OUT in directions and IN in directions
    assert any(frame.get("type") == "user" for _, frame in found.frames)
    replay = Replay(found, wait_seconds=2.0)
    again = asyncio.run(
        claude_line(replay_session(replay)).run_turn(
            "Reply with the single word: ready"
        )
    )
    assert same_turn(first, again)
    assert replay.finished()


def same_turn(one: TurnReport, other: TurnReport) -> bool:
    return (one.subtype, one.text, one.credential, one.session_id) == (
        other.subtype,
        other.text,
        other.credential,
        other.session_id,
    ) and one.subtype == "success"


def test_a_claude_code_engine_older_than_tested_is_refused() -> None:
    target = claude_line(transport_session(lambda: ScriptedCli(version="2.1.200")))
    with pytest.raises(EngineRefused, match="version 2.1.200, below 2.1.310"):
        asyncio.run(target.run_turn("hello"))


# Minimum versions and the capability tables.


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("2.1.310", (2, 1, 310)),
        ("v2.1", (2, 1)),
        ("2026.10.01-e373342", (2026, 10, 1)),
        ("Claude Code 2.1.310 (build)", (2, 1, 310)),
        ("2.1.310-beta.2", (2, 1, 310)),
        ("2.1 (2.1)", (2, 1)),
        ("node 20, agy 1.0.0", None),
        ("2.1.310 or 2.1.311", None),
        ("", None),
        ("unknown", None),
        (None, None),
    ],
)
def test_a_version_is_read_only_when_the_text_holds_one(
    text: str | None, numbers: tuple[int, ...] | None
) -> None:
    assert version_numbers(text) == numbers


def test_versions_compare_with_missing_parts_as_zero() -> None:
    assert below((2, 1, 200), (2, 1, 310))
    assert not below((2, 1, 310), (2, 1, 310))
    assert not below((2, 1), (2, 1, 0))
    assert below((2, 1), (2, 1, 1))
    assert not below((3,), (2, 1, 310))


def test_a_version_below_the_minimum_or_unreadable_is_refused() -> None:
    assert version_refusal(CLAUDE_CODE, "2.1.310") is None
    assert version_refusal(CLAUDE_CODE, "2.2.0") is None
    older = version_refusal(CLAUDE_CODE, "2.1.309")
    assert older is not None and "below 2.1.310" in older
    unread = version_refusal(CLAUDE_CODE, None)
    assert unread is not None and "no version" in unread
    assert version_refusal(table_of("pi"), None) is None


def test_every_engine_has_a_complete_table() -> None:
    from dromon import antigravity, codex, cursor, grok, pi  # noqa: PLC0415
    from dromon.engine import ENGINE_NAME  # noqa: PLC0415

    names = {
        ENGINE_NAME,
        pi.ENGINE_NAME,
        cursor.ENGINE_NAME,
        grok.ENGINE_NAME,
        codex.ENGINE_NAME,
        antigravity.ENGINE_NAME,
    }
    assert set(TABLES) == names
    for table in TABLES.values():
        assert list(table.capabilities()) == list(CAPABILITY_NAMES)
        assert all(Known(value) for value in table.capabilities().values())
        assert (table.minimum_version is None) == (table.tested_on is None)
    for engine in ("grok-build", "codex", "antigravity"):
        table = table_of(engine)
        assert table.pending is not None
        assert set(table.capabilities().values()) == {"unknown"}
    with pytest.raises(KeyError):
        table_of("no-such-engine")


# The probe's options.


def test_the_probe_records_only_where_it_can(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    assert cli.main([*base, "--record-stderr"]) == exits.FAILURE
    assert "only together with --record" in capsys.readouterr().err
    codex = [*base, "--model", "gpt-6-luna", "--record"]
    assert cli.main(codex) == exits.FAILURE
    assert "not yet supported for codex" in capsys.readouterr().err
    assert not (tmp_path / "tmp" / "probes").exists()


def test_an_init_without_its_tools_is_unknown_and_refused() -> None:
    """Row 10. An absent list of tools is unknown, not empty, so the check that the
    engine offers no tool the line was not given cannot pass on it."""
    target = claude_line(transport_session(lambda: ScriptedCli(tools=False)))
    with pytest.raises(EngineRefused, match="did not report its tools"):
        asyncio.run(target.run_turn("hello"))


def test_refusals_and_isolation_are_derived_from_the_tables() -> None:
    assert kept_session_refusal(CLAUDE_CODE) is None
    assert kept_session_refusal(table_of("pi")) == (
        "Pi lines do not keep their engine between turns"
    )
    assert unsupported_cursor(LineConfig(keep_session=True)) == kept_session_refusal(
        table_of("cursor-agent")
    )
    assert unsupported_pi(LineConfig(keep_session=True)) == kept_session_refusal(
        table_of("pi")
    )
    assert isolation_of(CLAUDE_CODE) == "full"
    assert isolation_of(table_of("cursor-agent")) == "partial"
    assert isolation_of(table_of("codex")) == "unknown"
    # Review of 2026-10-27, C1. Pi was never run live.
    assert isolation_of(table_of("pi")) == "unknown"
    assert {"cursor-agent", "antigravity"} == cli.PARTIAL_ISOLATION_ENGINES


def test_the_probe_reports_each_table_as_it_stands() -> None:
    claude = report_lines(CLAUDE_CODE)
    assert claude[0] == "table claude-code, tested from 2.1.310 on 2026-10-26"
    assert "kept_session yes" in claude[1] and "sign_in_check unknown" in claude[1]
    assert len(claude) == 2
    for engine in ("pi", "cursor-agent"):
        assert report_lines(table_of(engine))[0] == (
            f"table {engine}, never run live, so no minimum version is enforced and"
            " any version the engine reports runs"
        )
    pi = report_lines(table_of("pi"))
    assert "own_tools unknown, deny_before_run unknown" in pi[1]
    assert "sign_in_check unknown" in pi[1]
    grok = report_lines(table_of("grok-build"))
    assert "to be filled" in grok[2]


# Scrubbing. Every value below is invented, in the shapes of the first live Claude Code
# recording, so no real account, path or identifier is in this file.

HOME = "/home/example-person"
EMAIL = "someone@example.org"
ORGANIZATION = "Example Organization Name"
SESSION = "9f8e7d6c-1111-4222-8333-444455556666"
MESSAGE = "0a1b2c3d-aaaa-4bbb-8ccc-ddddeeeeffff"
FIXTURE = (
    Path(__file__).parent / "fixtures" / "probes" / "claude-code-haiku-2026-10-27.jsonl"
)


def identifying(tmp_path: Path, extra: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "raw.jsonl"
    recording = Recording(path, "claude-code", HAIKU, stderr=True)
    recording.frame(OUT, {"type": "control_request", "request_id": "req_1"})
    recording.frame(
        IN,
        {
            "type": "control_response",
            "response": {
                "request_id": "req_1",
                "response": {
                    "account": {
                        "email": EMAIL,
                        "organization": ORGANIZATION,
                        "subscriptionType": "max",
                        "type": "chatgpt",
                    },
                    "user_output_styles_dir": f"{HOME}/.claude/output-styles",
                },
            },
        },
    )
    recording.frame(OUT, {"type": "user", "session_id": "default"})
    recording.frame(
        IN,
        {
            "type": "system",
            "subtype": "init",
            "cwd": f"{HOME}/projects/thing",
            "session_id": SESSION,
            "uuid": MESSAGE,
            "memory_paths": {
                "auto": f"{HOME}/.claude/projects/{HOME.replace('/', '-')}-projects/m/"
            },
            "note": f"made for {ORGANIZATION}",
            **(extra or {}),
        },
    )
    recording.frame(
        IN,
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning",
                "resetsAt": 1798000123,
                "rateLimitType": "seven_day",
                "utilization": 0.87,
                "overageStatus": "rejected",
                "overageDisabledReason": "out_of_credits",
                "isUsingOverage": True,
                "surpassedThreshold": 0.75,
                "remainingRequests": 4321,
                "windowLabel": "a label no reader lists",
                "overageResetsAt": None,
                "unifiedWindows": {
                    "five_hour": {
                        "utilization": 0.31,
                        "resetsAt": 1798000456,
                        "status": "allowed",
                        "rateLimitType": "a window no one named",
                        "usedRequests": [57, 58],
                    },
                },
            },
            "session_id": SESSION,
        },
    )
    recording.frame(IN, {"type": "result", "session_id": SESSION.upper()})
    recording.stderr(f"reading {HOME}/.claude/settings.json {MESSAGE}\n".encode())
    recording.close()
    return path


def test_a_scrubbed_recording_keeps_its_shape_with_stable_placeholders(
    tmp_path: Path,
) -> None:
    raw = read_recording(identifying(tmp_path))
    written = evidence.scrub_file(
        identifying_path := tmp_path / "raw.jsonl", tmp_path / "clean.jsonl", HOME
    )
    assert identifying_path.exists()
    text = written.read_text()
    for value in (HOME, HOME.replace("/", "-"), EMAIL, ORGANIZATION, SESSION, MESSAGE):
        assert value not in text
    assert "example-person" not in text
    clean = read_recording(written)
    assert [d for d, _ in clean.frames] == [d for d, _ in raw.frames]
    account = clean.frames[1][1]["response"]["response"]["account"]
    assert account["type"] == "chatgpt"
    assert account["email"] == "scrubbed-account-email"
    assert account["subscriptionType"] == "scrubbed-account-subscriptionType"
    init = clean.frames[3][1]
    assert init["cwd"] == f"{evidence.SCRUBBED_HOME}/projects/thing"
    assert "-home-operator-projects" in init["memory_paths"]["auto"]
    assert init["note"] == "made for scrubbed-account-organization"
    limits = clean.frames[4][1]["rate_limit_info"]
    assert limits == {
        "status": evidence.SCRUBBED_STATUS,
        "resetsAt": evidence.SCRUBBED_EPOCH,
        "rateLimitType": "seven_day",
        "utilization": evidence.SCRUBBED_FRACTION,
        "overageStatus": evidence.SCRUBBED_STATUS,
        "overageDisabledReason": evidence.SCRUBBED_REASON,
        "isUsingOverage": False,
        "surpassedThreshold": evidence.SCRUBBED_NUMBER,
        "remainingRequests": evidence.SCRUBBED_NUMBER,
        "windowLabel": evidence.SCRUBBED_REASON,
        "overageResetsAt": None,
        "unifiedWindows": {
            "five_hour": {
                "utilization": evidence.SCRUBBED_FRACTION,
                "resetsAt": evidence.SCRUBBED_EPOCH,
                "status": evidence.SCRUBBED_STATUS,
                "rateLimitType": evidence.SCRUBBED_REASON,
                "usedRequests": [evidence.SCRUBBED_NUMBER, evidence.SCRUBBED_NUMBER],
            },
        },
    }
    for usage in (
        "0.87",
        "0.31",
        "0.75",
        "1798000123",
        "out_of_credits",
        "4321",
        "no reader lists",
        "no one named",
        "57",
    ):
        assert usage not in text
    assert clean.frames[5][1]["session_id"] == init["session_id"] != init["uuid"]
    assert clean.frames[2][1]["session_id"] == "session-1"
    assert clean.stderr is not None and init["uuid"] in clean.stderr
    assert evidence.SCRUBBED_HOME in clean.stderr


def test_a_scrubbed_recording_replays_as_its_original_would(tmp_path: Path) -> None:
    path = tmp_path / "claude.jsonl"
    recording = Recording(path, "claude-code", HAIKU)
    live = claude_line(
        transport_session(lambda: RecordedTransport(ScriptedCli(), recording))
    )
    first = asyncio.run(live.run_turn("Reply with the single word: ready"))
    recording.close()
    clean = evidence.scrub_file(path, tmp_path / "clean.jsonl", HOME)
    replay = Replay(read_recording(clean), wait_seconds=2.0)
    again = asyncio.run(
        claude_line(replay_session(replay)).run_turn(
            "Reply with the single word: ready"
        )
    )
    assert (again.subtype, again.text, again.credential) == (
        first.subtype,
        first.text,
        first.credential,
    )
    assert replay.finished()


@pytest.mark.parametrize(
    ("extra", "kind"),
    [
        ({"contact": "other@example.net"}, "an email address"),
        ({"who": "example-person"}, "the user name"),
        ({"shout": "EXAMPLE-PERSON wrote this"}, "the user name"),
        ({"wide": "other\uff20example.net"}, "an email address"),
        ({"glued": f"x{ORGANIZATION}x"}, "an account value"),
    ],
)
def test_a_scrub_refuses_what_it_cannot_place_and_writes_nothing(
    tmp_path: Path, extra: dict[str, Any], kind: str
) -> None:
    source = identifying(tmp_path, extra)
    target = tmp_path / "clean.jsonl"
    with pytest.raises(RecordingError, match=kind) as refused:
        evidence.scrub_file(source, target, HOME)
    assert not target.exists()
    assert "example" not in str(refused.value)


def test_a_scrub_never_overwrites_and_refuses_a_bad_source(tmp_path: Path) -> None:
    source = identifying(tmp_path)
    target = tmp_path / "clean.jsonl"
    target.write_text("kept")
    with pytest.raises(FileExistsError):
        evidence.scrub_file(source, target, HOME)
    assert target.read_text() == "kept"
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n")
    with pytest.raises(RecordingError, match="not a version 1 header|not JSON"):
        evidence.scrub_file(broken, tmp_path / "other.jsonl", HOME)


def test_a_scrub_needs_an_absolute_home_and_bounded_nesting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an absolute path"):
        evidence.scrub_file(identifying(tmp_path), tmp_path / "c.jsonl", "relative")
    deep: Any = "end"
    for _ in range(evidence.MAX_SCRUB_DEPTH + 2):
        deep = [deep]
    path = recorded(tmp_path, [(IN, {"deep": deep})])
    with pytest.raises(RecordingError, match="nests deeper"):
        evidence.scrub_file(path, tmp_path / "d.jsonl", HOME)


def test_leaks_names_kinds_never_values() -> None:
    found = evidence.leaks(
        f"{HOME}/x {EMAIL} {ORGANIZATION}", HOME, frozenset({ORGANIZATION})
    )
    assert found == [
        "the home directory",
        "the user name",
        "an email address",
        "an account value",
    ]
    assert evidence.leaks("clean text", HOME) == []
    assert evidence.leaks("/home/ab is short", "/home/ab") == [
        "the home directory",
        "the user name",
    ]
    assert evidence.leaks("AB wrote, abc did not", "/home/ab") == ["the user name"]
    assert evidence.leaks("abc and xab", "/home/ab") == []
    assert evidence.leaks("\uff21\uff22 wrote", "/home/ab") == ["the user name"]
    assert evidence.leaks("on the max plan", HOME, frozenset({"max"})) == [
        "an account value"
    ]
    assert evidence.leaks("maximum", HOME, frozenset({"max"})) == []


def test_every_account_value_is_replaced_as_a_word_whatever_its_length(
    tmp_path: Path,
) -> None:
    """Review of 2026-10-27, E2. The plan name ``max`` is three characters, once too
    short to be replaced outside the account block."""
    source = identifying(tmp_path, {"plan": "on the max plan, at maximum"})
    clean = read_recording(evidence.scrub_file(source, tmp_path / "c.jsonl", HOME))
    assert clean.frames[3][1]["plan"] == (
        "on the scrubbed-account-subscriptionType plan, at maximum"
    )


def test_probe_scrub_writes_a_copy_without_running_a_turn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", HOME)
    source = identifying(tmp_path)
    assert cli.main(["probe", "--scrub", str(source)]) == exits.OK
    target = tmp_path / "raw.scrubbed.jsonl"
    assert str(target) in capsys.readouterr().out
    assert EMAIL not in target.read_text()
    assert cli.main(["probe", "--scrub", str(source)]) == exits.FAILURE
    assert "cannot write" in capsys.readouterr().err
    chosen = tmp_path / "chosen.jsonl"
    assert (
        cli.main(["probe", "--scrub", str(source), "--scrub-to", str(chosen)])
        == exits.OK
    )
    assert chosen.exists()
    assert cli.main(["probe", "--scrub-to", str(chosen)]) == exits.FAILURE
    assert "only with --scrub" in capsys.readouterr().err
    leaky = identifying(tmp_path / "leaky", {"contact": "other@example.net"})
    assert cli.main(["probe", "--scrub", str(leaky)]) == exits.FAILURE
    assert "an email address" in capsys.readouterr().err


def test_the_live_fixture_is_scrubbed_and_replays_through_the_real_adapter() -> None:
    """The first live Claude Code probe, on 2026-10-27, scrubbed by ``scrub_file``."""
    text = FIXTURE.read_text()
    assert evidence.leaks(text, str(Path.home())) == []
    assert "scrubbed-account-email" in text and evidence.SCRUBBED_HOME in text
    limits = [
        frame["rate_limit_info"]
        for _, frame in read_recording(FIXTURE).frames
        if frame.get("type") == "rate_limit_event"
    ]
    assert limits and all(
        info["utilization"] == evidence.SCRUBBED_FRACTION
        and info["resetsAt"] == evidence.SCRUBBED_EPOCH
        and info["surpassedThreshold"] == evidence.SCRUBBED_NUMBER
        for info in limits
    )
    found = read_recording(FIXTURE)
    assert found.header["engine"] == "claude-code" and not found.truncated
    replay = Replay(found, wait_seconds=2.0)
    report = asyncio.run(claude_line(replay_session(replay)).run_turn(cli.PROBE_PROMPT))
    assert report.subtype == "success"
    assert report.credential is Credential.SUBSCRIPTION
    assert report.session_id == "00000000-0000-4000-8000-000000000002"
    assert replay.finished()
    assert [frame.get("type") for frame in replay.sent] == [
        frame.get("type") for frame in replay.recorded_out
    ]
