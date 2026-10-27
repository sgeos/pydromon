"""Row 7 of the T3 Code review. Probe evidence: recording, replay, and minimum
versions. Every engine here is a scripted fake or a recording, so no subscription is
spent."""

import asyncio
import dataclasses
import json
import stat
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._internal.transport import Transport

from dromon import cli, codex, evidence, exits, grok
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
from dromon.antigravity import unsupported as unsupported_antigravity
from dromon.codex import unsupported as unsupported_codex
from dromon.cursor import CursorLine
from dromon.cursor import unsupported as unsupported_cursor
from dromon.grok import unsupported as unsupported_grok
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
from dromon.paths import PathError, project_paths

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


def test_the_probe_path_is_under_the_state_directory(tmp_path: Path) -> None:
    """Review of 2026-10-27, R1. A raw recording holds the account, so it is written
    where no line can read it, and never under the shared ``tmp``."""
    path = evidence.probe_path(tmp_path / ".dromon", "pi")
    assert path.parent == tmp_path / ".dromon" / "probes"
    assert path.name.startswith("pi-") and path.suffix == ".jsonl"
    assert Path("tmp") / "probes" == evidence.FIXTURES_DIRECTORY


def test_a_probe_recording_is_private_in_a_private_directory(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    recording = cli._recording(paths, "pi", "m", False)  # pyright: ignore[reportPrivateUsage]
    recording.close()
    probes = paths.state_dir / evidence.PROBES_DIRECTORY
    assert recording.path.parent == probes
    assert stat.S_IMODE(probes.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(recording.path.stat().st_mode) == 0o600
    assert not (tmp_path / "tmp").exists()


def test_a_probe_refuses_a_state_directory_others_can_read(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o755)
    paths.state_dir.chmod(0o755)
    with pytest.raises(PathError, match="group or others"):
        cli._recording(paths, "pi", "m", False)  # pyright: ignore[reportPrivateUsage]
    assert not (paths.state_dir / evidence.PROBES_DIRECTORY).exists()


class FailingFile:
    """A file whose every write fails, as on a full disk."""

    closed = False

    def write(self, text: str) -> int:
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        raise OSError(28, "No space left on device")

    def close(self) -> None:
        self.closed = True


def test_a_failed_write_is_noted_and_never_raised(tmp_path: Path) -> None:
    """Review of 2026-10-27, R3. The turn goes on when the recording cannot be
    written, and the probe says the file is incomplete."""
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, "e", "m", stderr=True)
    recording.frame(OUT, {"kept": 1})
    recording._file.close()  # pyright: ignore[reportPrivateUsage]
    recording._file = FailingFile()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    recording.frame(IN, {"lost": 1})
    recording.frame(OUT, {"lost": 2})
    recording.stderr(b"anything")
    recording.close()
    assert recording.failure is not None and "No space" in recording.failure
    lines = path.read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["header", "frame"]
    assert read_recording(path).frames == [(OUT, {"kept": 1})]
    with pytest.raises(ValueError, match="out or in"):
        recording.frame("sideways", {})


def test_a_failed_recording_is_reported_by_the_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recording = Recording(tmp_path / "probe.jsonl", "e", "m")
    recording._file.close()  # pyright: ignore[reportPrivateUsage]
    recording._file = FailingFile()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    recording.frame(OUT, {"lost": 1})
    recording.close()
    cli._report_recording(recording)  # pyright: ignore[reportPrivateUsage]
    captured = capsys.readouterr()
    assert "dromon probe --scrub probe.jsonl" in captured.out
    assert "incomplete" in captured.err and "No space" in captured.err


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


@pytest.mark.parametrize(
    ("command", "start"),
    [
        ("codex", codex._app_server),  # pyright: ignore[reportPrivateUsage]
        ("grok", grok._grok_agent),  # pyright: ignore[reportPrivateUsage]
    ],
)
def test_codex_and_grok_are_recorded_with_their_standard_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    start: codex.ConnectionFactory,
) -> None:
    """Codex and Grok frame their own traffic, so their own process wrappers record,
    as ``dromon.jsonlines`` does for the others."""
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / command
    script.write_text(f"#!{sys.executable}\n{HARNESS}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, command, "m", stderr=True)

    async def go() -> dict[str, Any] | None:
        async with start([], tmp_path, {"PATH": str(directory)}) as engine:
            await engine.send({"ask": 7})
            return await engine.receive()

    token = evidence.RECORDING.set(recording)
    try:
        assert asyncio.run(go()) == {"answer": 7}
    finally:
        evidence.RECORDING.reset(token)
        recording.close()
    found = read_recording(path)
    assert found.frames == [(OUT, {"ask": 7}), (IN, {"answer": 7})]
    assert found.stderr is not None
    assert "token=***" in found.stderr and "abc123" not in found.stderr


LOUD_HARNESS = (
    "import json, sys\n"
    "sys.stderr.write('x' * 262144)\n"
    "sys.stderr.flush()\n"
    "request = json.loads(sys.stdin.readline())\n"
    "sys.stdout.write(json.dumps({'answer': request['ask']}) + '\\n')\n"
    "sys.stdout.flush()\n"
)
"""An engine that writes more standard error than a pipe holds before it answers,
so it answers only if its standard error is read."""


@pytest.mark.parametrize(
    "start",
    [
        codex._app_server,  # pyright: ignore[reportPrivateUsage]
        grok._grok_agent,  # pyright: ignore[reportPrivateUsage]
        json_lines_factory("harness", keep_input=True),
    ],
)
def test_standard_error_is_drained_alike_without_a_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: codex.ConnectionFactory
) -> None:
    """Review of 2026-10-27, R2. Standard error is a pipe read to its end whether or
    not a probe records, so a probe's engine and a daemon line's run alike. The
    engine here blocks unless it is read, and nothing is written to disk."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for command in ("codex", "grok", "harness"):
        script = directory / command
        script.write_text(f"#!{sys.executable}\n{LOUD_HARNESS}")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))

    async def go() -> dict[str, Any] | None:
        async with start([], tmp_path, {"PATH": str(directory)}) as engine:
            await engine.send({"ask": 8})
            return await engine.receive()

    assert evidence.RECORDING.get() is None
    assert evidence.STDERR_TARGET == asyncio.subprocess.PIPE
    assert asyncio.run(asyncio.wait_for(go(), 20)) == {"answer": 8}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin"]


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
        assert table.minimum_version is None or table.tested_on is not None
    with pytest.raises(KeyError):
        table_of("no-such-engine")


def test_a_minimum_version_needs_the_date_of_its_run() -> None:
    with pytest.raises(ValueError, match="without its run"):
        dataclasses.replace(CLAUDE_CODE, tested_on=None)


@pytest.mark.parametrize(
    ("engine", "refusal"),
    [
        ("codex", unsupported_codex),
        ("grok-build", unsupported_grok),
        ("antigravity", unsupported_antigravity),
    ],
)
def test_the_filled_tables_match_what_their_adapters_refuse(
    engine: str, refusal: Callable[[LineConfig], str | None]
) -> None:
    """The Grok, Codex and Antigravity tables were filled after their adapters
    changed on another branch. A kept session is refused by the adapter exactly where
    the table says it is not kept."""
    refused = refusal(LineConfig(keep_session=True)) is not None
    assert refused == (table_of(engine).kept_session is not Known.YES)


def test_the_filled_tables_say_what_was_never_observed() -> None:
    """Review of 2026-10-27, C1, applied to the filled tables. Only Codex was run
    live for a turn, so only its own tools and isolation are known."""
    codex_table, grok_table, antigravity_table = (
        table_of("codex"),
        table_of("grok-build"),
        table_of("antigravity"),
    )
    assert (codex_table.own_tools, codex_table.deny_before_run) == (Known.YES,) * 2
    assert codex_table.tested_on is not None and codex_table.minimum_version is None
    for table in (grok_table, antigravity_table):
        assert table.own_tools is Known.UNKNOWN
        assert table.tested_on is None
    assert grok_table.deny_before_run is Known.UNKNOWN
    assert antigravity_table.deny_before_run is Known.NO
    for table in (codex_table, grok_table, antigravity_table):
        assert table.sign_in_check is Known.UNKNOWN


# The probe's options.


def test_every_engine_with_a_table_records() -> None:
    assert set(TABLES) == cli.RECORDING_ENGINES


def test_the_probe_records_only_where_it_can(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every engine records, so an engine whose adapter does not is made here, by
    taking Codex out of the engines that record."""
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    assert cli.main([*base, "--record-stderr"]) == exits.FAILURE
    assert "only together with --record" in capsys.readouterr().err
    monkeypatch.setattr(cli, "RECORDING_ENGINES", cli.RECORDING_ENGINES - {"codex"})
    codex = [*base, "--model", "gpt-6-luna", "--record"]
    assert cli.main(codex) == exits.FAILURE
    assert "not yet supported for codex" in capsys.readouterr().err
    assert not (tmp_path / "tmp" / "probes").exists()
    assert not (tmp_path / ".dromon" / "probes").exists()


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
    assert isolation_of(table_of("codex")) == "full"
    assert isolation_of(table_of("antigravity")) == "partial"
    # Review of 2026-10-27, C1. Pi and Grok were never run live for a turn.
    assert isolation_of(table_of("pi")) == "unknown"
    assert isolation_of(table_of("grok-build")) == "unknown"
    # Antigravity is partial by its table now, not by name.
    assert {"cursor-agent", "antigravity"} == cli.PARTIAL_ISOLATION_ENGINES


def test_the_probe_reports_each_table_as_it_stands() -> None:
    claude = report_lines(CLAUDE_CODE)
    assert claude[0] == "table claude-code, tested from 2.1.310 on 2026-10-26"
    assert "kept_session yes" in claude[1] and "sign_in_check unknown" in claude[1]
    assert len(claude) == 2
    for engine in ("pi", "cursor-agent", "grok-build", "antigravity"):
        assert report_lines(table_of(engine))[0] == (
            f"table {engine}, never run live, so no minimum version is enforced and"
            " any version the engine reports runs"
        )
    pi = report_lines(table_of("pi"))
    assert "own_tools unknown, deny_before_run unknown" in pi[1]
    assert "sign_in_check unknown" in pi[1]
    codex_report = report_lines(table_of("codex"))
    assert codex_report[0] == (
        "table codex, run live on 2026-10-25, but no version is read, so no minimum"
        " version is enforced and any version the engine reports runs"
    )
    assert "deny_before_run yes" in codex_report[1]
    assert all(len(report_lines(table)) == 2 for table in TABLES.values())


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
                "utilization": 0.871931,
                "overageStatus": "rejected",
                "overageDisabledReason": "out_of_credits",
                "isUsingOverage": True,
                "surpassedThreshold": 0.758263,
                "remainingRequests": 864213,
                "windowLabel": "a label no reader lists",
                "overageResetsAt": None,
                "unifiedWindows": {
                    "five_hour": {
                        "utilization": 0.314927,
                        "resetsAt": 1798000456,
                        "status": "allowed",
                        "rateLimitType": "a window no one named",
                        "usedRequests": [735791, 735792],
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
    # The invented values are long and unusual, so no timestamp, identifier or
    # placeholder in the scrubbed text can hold one by chance. A value of 57 once
    # failed this check when the recording's own clock read 01:57.
    for usage in (
        "0.871931",
        "0.314927",
        "0.758263",
        "1798000123",
        "out_of_credits",
        "864213",
        "no reader lists",
        "no one named",
        "735791",
        "735792",
    ):
        assert usage not in text
    assert clean.frames[5][1]["session_id"] == init["session_id"] != init["uuid"]
    assert clean.frames[2][1]["session_id"] == "session-1"
    assert clean.stderr is not None and init["uuid"] in clean.stderr
    assert evidence.SCRUBBED_HOME in clean.stderr


def test_a_codex_recording_loses_its_account_and_plan_usage(tmp_path: Path) -> None:
    """Codex reports its account in ``account/read`` and its plan's use in
    ``rateLimits``, in the shapes of its app-server protocol, which no recording has
    shown yet. Every value here is invented."""
    path = tmp_path / "codex.jsonl"
    recording = Recording(path, "codex", "gpt-6-luna")
    recording.frame(OUT, {"id": 2, "method": "account/read", "params": {}})
    recording.frame(
        IN,
        {
            "id": 2,
            "result": {
                "account": {"type": "chatgpt", "email": EMAIL, "planType": "pro"},
                "requiresOpenaiAuth": True,
            },
        },
    )
    recording.frame(
        IN,
        {
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 37.518,
                        "windowDurationMins": 29983,
                        "resetsAt": 1798000789,
                    },
                    "secondary": None,
                    "planType": "pro",
                }
            },
        },
    )
    recording.close()
    written = evidence.scrub_file(path, tmp_path / "clean.jsonl", HOME)
    text = written.read_text()
    for value in (EMAIL, "37.518", "29983", "1798000789", '"pro"'):
        assert value not in text
    clean = read_recording(written)
    assert clean.frames[1][1]["result"]["account"]["type"] == "chatgpt"
    assert clean.frames[2][1]["params"]["rateLimits"] == {
        "primary": {
            "usedPercent": evidence.SCRUBBED_NUMBER,
            "windowDurationMins": evidence.SCRUBBED_NUMBER,
            "resetsAt": evidence.SCRUBBED_EPOCH,
        },
        "secondary": None,
        "planType": evidence.SCRUBBED_REASON,
    }


def test_any_key_naming_an_account_organization_email_or_plan_is_scrubbed(
    tmp_path: Path,
) -> None:
    """Review of 2026-10-27, S2. A key is judged by the words of its name, wherever it
    stands, and each value is replaced by one of its own type. A list or object under
    an email or plan key is scrubbed as any other value, so Codex's plan steps keep
    their status words, which other frames share."""
    source = recorded(
        tmp_path,
        [
            (
                IN,
                {
                    "method": "account/updated",
                    "params": {
                        "userEmail": EMAIL,
                        "orgId": "org-8c1f4e2a",
                        "organization": {
                            "name": ORGANIZATION,
                            "type": "team",
                            "seats": 12,
                            "active": True,
                            "parent": None,
                        },
                        "chatgptPlanType": "prolific",
                        "explanation": "the prolific plan",
                    },
                },
            ),
            (
                IN,
                {
                    "method": "turn/plan/updated",
                    "params": {
                        "plan": [{"step": "read files", "status": "completed"}],
                    },
                },
            ),
            (IN, {"method": "item/completed", "params": {"status": "completed"}}),
        ],
    )
    clean = read_recording(evidence.scrub_file(source, tmp_path / "c.jsonl", HOME))
    params = clean.frames[0][1]["params"]
    assert params == {
        "userEmail": "scrubbed-account-userEmail",
        "orgId": "scrubbed-account-orgId",
        "organization": {
            "name": "scrubbed-account-name",
            "type": "team",
            "seats": evidence.SCRUBBED_NUMBER,
            "active": False,
            "parent": None,
        },
        "chatgptPlanType": "scrubbed-account-chatgptPlanType",
        "explanation": "the scrubbed-account-chatgptPlanType plan",
    }
    assert clean.frames[1][1] == read_recording(source).frames[1][1]
    assert clean.frames[2][1] == read_recording(source).frames[2][1]


def test_key_words_split_every_naming_style() -> None:
    words = evidence._key_words  # pyright: ignore[reportPrivateUsage]
    assert words("planType") == {"plan", "type"}
    assert words("chatgpt_plan_type") == {"chatgpt", "plan", "type"}
    assert words("OrgID") == {"org", "id"}
    assert words("organizationUUID") == {"organization", "uuid"}
    assert words("explanation") == {"explanation"}
    assert words("user-email") == {"user", "email"}


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
    source = identifying(tmp_path, {"remark": "on the max plan, at maximum"})
    clean = read_recording(evidence.scrub_file(source, tmp_path / "c.jsonl", HOME))
    assert clean.frames[3][1]["remark"] == (
        "on the scrubbed-account-subscriptionType plan, at maximum"
    )


def test_probe_scrub_writes_a_copy_without_running_a_turn(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of 2026-10-27, R1. A relative name is read from the state directory's
    probes, where ``--record`` writes, and the copy goes to the project's
    ``tmp/probes``, where lines can read it."""
    monkeypatch.setenv("HOME", HOME)
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    probes = tmp_path / ".dromon" / "probes"
    probes.mkdir(parents=True)
    source = identifying(probes)
    assert cli.main([*base, "--scrub", source.name]) == exits.OK
    target = tmp_path / "tmp" / "probes" / "raw.scrubbed.jsonl"
    assert str(target) in capsys.readouterr().out
    assert EMAIL not in target.read_text()
    assert cli.main([*base, "--scrub", source.name]) == exits.FAILURE
    assert "cannot write" in capsys.readouterr().err
    chosen = tmp_path / "chosen.jsonl"
    assert (
        cli.main([*base, "--scrub", str(source), "--scrub-to", str(chosen)]) == exits.OK
    )
    assert chosen.exists()
    assert cli.main([*base, "--scrub-to", str(chosen)]) == exits.FAILURE
    assert "only with --scrub" in capsys.readouterr().err
    leaky = identifying(tmp_path / "leaky", {"contact": "other@example.net"})
    assert cli.main([*base, "--scrub", str(leaky)]) == exits.FAILURE
    assert "an email address" in capsys.readouterr().err
    assert cli.main([*base, "--scrub", "absent.jsonl"]) == exits.FAILURE
    assert "cannot read" in capsys.readouterr().err


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
