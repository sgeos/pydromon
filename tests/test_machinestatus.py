"""Round 16, item 3. Dromon reads the shared machine's status before it starts a run or
a turn. ``dromon.machinestatus`` reads and decides, and the scheduler defers or warns.
``docs/architecture/RUNS.md`` and ``docs/protocol/LINE_CONFIG.md`` are normative. The
status file is the monitor's own and Dromon never runs the monitor or touches the
network, so every test here writes the file itself."""

import asyncio
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import machinestatus, runs
from dromon.daemon import machine_status_path
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.machinestatus import (
    NO_DECISION,
    STALE_SECONDS,
    Decision,
    Kind,
    MachineStatus,
    decide,
    kind_of,
    parse,
    read,
)
from dromon.model import HARNESS, LineAddress, OperatorAddress, RunStarted
from dromon.paths import ProjectPaths
from dromon.projectconfig import ConfigFileError, ProjectConfig, parse as parse_project
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
START = datetime(2026, 11, 11, 9, 0, 0, tzinfo=UTC)
NOW = START.timestamp()


def header(level: str, reasons: str, age: int = 10, epoch: int | None = None) -> str:
    """A status file as the monitor writes it, with its tables after the header."""
    stamp = int(NOW) - age if epoch is None else epoch
    return (
        f"time=2026-11-11T01:00:00-0800\nepoch={stamp}\nlevel={level}\n"
        f"reasons={reasons}\nplaybook=/somewhere/PLAYBOOK.md\npressure_level=1\n"
        "swap_used_mb=5088\nload1=6.34\ndata_free_gb=45\n\n"
        "# Largest sessions by the memory of their processes, MB\n   1052  x (2)\n"
    )


# The reader.


def test_a_fresh_file_in_the_monitors_own_format_is_read() -> None:
    status = parse(header("ok", "none"), NOW)
    assert status == MachineStatus("ok", (), 10)
    assert status.kinds() == frozenset()
    assert decide(status) == NO_DECISION


def test_the_reasons_are_split_bounded_and_made_printable() -> None:
    many = "; ".join(f"swap use is {n} MB" for n in range(20))
    status = parse(header("elevated", many), NOW)
    assert len(status.reasons) == machinestatus.MAX_REASONS
    long = parse(header("elevated", "x" * 500 + "\x1b[31m; ok\x07"), NOW)
    assert len(long.reasons[0]) == machinestatus.MAX_REASON_CHARS
    assert all(c.isprintable() for reason in long.reasons for c in reason)


def test_only_the_header_is_read_and_the_first_key_counts() -> None:
    text = header("ok", "none") + "level=critical\nepoch=1\n"
    assert parse(text, NOW).level == "ok"
    twice = "level=ok\nlevel=critical\nepoch=" + str(int(NOW)) + "\n"
    assert parse(twice, NOW).level == "ok"
    assert parse("garbage\nlevel=ok\nepoch=" + str(int(NOW)) + "\n", NOW).level == "ok"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "epoch=1\n",
        f"level=fine\nepoch={int(NOW)}\n",
        "level=ok\nepoch=soon\n",
        "level=ok\n",
        f"level=ok\nepoch={int(NOW) + 5 * STALE_SECONDS}\n",
    ],
)
def test_a_file_that_cannot_be_relied_on_is_unreadable(text: str) -> None:
    status = parse(text, NOW)
    assert status.level == "unreadable" and status.problem
    assert status.effective == "elevated"
    assert decide(status) == Decision(defer_runs=True, defer_turns=False, warn=True)


def test_a_stale_sample_is_stale_and_a_small_skew_is_not() -> None:
    stale = parse(header("ok", "none", age=STALE_SECONDS + 1), NOW)
    assert stale.level == "stale" and stale.age_seconds == STALE_SECONDS + 1
    assert stale.effective == "elevated"
    assert parse(header("ok", "none", age=STALE_SECONDS), NOW).level == "ok"
    skewed = parse(header("ok", "none", age=-30), NOW)
    assert skewed.level == "ok" and skewed.age_seconds == 0


def test_a_missing_file_a_directory_and_a_pipe_are_read_without_raising(
    tmp_path: Path,
) -> None:
    missing = read(tmp_path / "absent", NOW)
    assert missing.level == "missing" and missing.effective == "elevated"
    directory = read(tmp_path, NOW)
    assert directory.level == "unreadable" and "regular" in str(directory.problem)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    started = time.monotonic()
    assert read(pipe, NOW).level == "unreadable"
    assert time.monotonic() - started < 5, "a named pipe held the reader"
    assert stat.S_ISFIFO(pipe.stat().st_mode)


def test_a_file_over_the_bound_is_unreadable(tmp_path: Path) -> None:
    big = tmp_path / "status"
    big.write_text(header("ok", "none") + "x" * machinestatus.MAX_BYTES)
    status = read(big, NOW)
    assert status.level == "unreadable" and "larger" in str(status.problem)


def test_a_file_that_cannot_be_opened_is_unreadable(tmp_path: Path) -> None:
    locked = tmp_path / "status"
    locked.write_text(header("ok", "none"))
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("this user reads a file with no permission, as root does")
        assert read(locked, NOW).level == "unreadable"
    finally:
        locked.chmod(0o600)


def test_a_file_read_gives_the_same_status_as_its_text(tmp_path: Path) -> None:
    path = tmp_path / "status"
    text = header("elevated", "swap use is 17000 MB")
    path.write_text(text)
    assert read(path, NOW) == parse(text, NOW)


# What a reason is about.


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("kernel memory pressure is at warning", Kind.MEMORY),
        ("kernel memory pressure is critical", Kind.MEMORY),
        ("swap is growing by 300 MB a minute", Kind.SWAP),
        ("swap use is 17000 MB", Kind.SWAP),
        ("at the current swap growth the data volume fills in 30 minutes", Kind.SWAP),
        ("the processor load is high", Kind.LOAD),
        ("data volume has 45 GB free", Kind.DISK),
        ("could not read: pressure swap", Kind.INFO),
        ("something nobody has met", Kind.UNKNOWN),
    ],
)
def test_each_reason_has_a_kind(reason: str, kind: Kind) -> None:
    assert kind_of(reason) is kind


@pytest.mark.parametrize(
    ("level", "reasons", "runs_deferred", "turns_deferred", "warned"),
    [
        ("ok", "none", False, False, False),
        ("ok", "could not read: swap", False, False, False),
        ("elevated", "kernel memory pressure is at warning", True, False, True),
        ("elevated", "swap use is 17000 MB", True, False, True),
        ("elevated", "the processor load is high", True, False, True),
        ("elevated", "data volume has 45 GB free", False, False, True),
        (
            "elevated",
            "data volume has 45 GB free; swap use is 17000 MB",
            True,
            False,
            True,
        ),
        (
            "elevated",
            "data volume has 45 GB free; could not read: swap",
            False,
            False,
            True,
        ),
        ("elevated", "an unknown reason", True, False, True),
        ("elevated", "none", True, False, True),
        ("critical", "kernel memory pressure is critical", True, True, False),
        ("critical", "data volume has 12 GB free", False, True, False),
        (
            "critical",
            "data volume has 12 GB free; swap use is 17000 MB",
            True,
            True,
            False,
        ),
    ],
)
def test_the_decision_for_each_level_and_reason(
    level: str, reasons: str, runs_deferred: bool, turns_deferred: bool, warned: bool
) -> None:
    status = parse(header(level, reasons), NOW)
    assert decide(status) == Decision(runs_deferred, turns_deferred, warned)


def test_a_sentence_names_the_status_and_never_a_path() -> None:
    elevated = parse(header("elevated", "swap use is 17000 MB"), NOW).describe()
    assert "elevated" in elevated and "swap use is 17000 MB" in elevated
    assert "10 seconds ago" in elevated and "/" not in elevated
    assert "stale" in parse(header("ok", "none", age=500), NOW).describe()
    assert "missing" in MachineStatus("missing", problem="no file is there").describe()
    assert "cannot be relied on" in MachineStatus("unreadable", problem="x").describe()
    assert "is ok" in parse(header("ok", "none"), NOW).describe()


# The project file and the daemon's path.


def project_file(tmp_path: Path, body: str) -> ProjectConfig:
    return parse_project(f"version = 1\n[daemon]\n{body}\n", tmp_path / "p.toml")


@pytest.mark.parametrize("body", ["machine_status = 3", 'machine_status = ""'])
def test_the_project_file_refuses_a_bad_machine_status(
    tmp_path: Path, body: str
) -> None:
    with pytest.raises(ConfigFileError, match="daemon.machine_status"):
        project_file(tmp_path, body)


def test_the_project_file_names_a_machine_status_or_none(tmp_path: Path) -> None:
    assert project_file(tmp_path, "").machine_status is None
    config = project_file(tmp_path, 'machine_status = "~/.local/state/x/status"')
    assert config.machine_status == "~/.local/state/x/status"


def test_the_daemon_resolves_the_path_by_its_form(tmp_path: Path) -> None:
    paths = ProjectPaths(tmp_path, tmp_path / "state", tmp_path / "run")

    def resolved(named: str | None) -> Path | None:
        return machine_status_path(ProjectConfig(machine_status=named), paths)

    assert resolved(None) is None
    assert resolved("/var/status") == Path("/var/status")
    assert resolved("rel/status") == tmp_path / "rel" / "status"
    assert resolved("~/x") == Path("~/x").expanduser()


# The scheduler.


class Clock:
    """The wall clock, which a test moves."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Ticks:
    """The monotonic clock the read's cache expires by, which a test moves."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class Recorder:
    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.session_id = self.session_id or "session-1"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, 0.1
        )


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "reports").mkdir()
        self.status = root / "machine-status"
        self.clock = Clock()
        self.ticks = Ticks()
        self.log = EventLog(root / "events.sqlite")
        self.engines: dict[str, Recorder] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=self.clock,
            clock=self.ticks,
            default_operator="ana",
            root=root,
            report_dir=root / "reports",
            run_start_of=lambda pid: "2026-11-11T09:00:00Z" if pid else None,
            run_occupied=lambda session: False,
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.set_runs(
            {"gate": (sys.executable, "-c", "print('gate passed')")}
        )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        engine = self.engines.get(spec.line_id) or Recorder(spec)
        self.engines[spec.line_id] = engine
        return engine

    def write(self, level: str, reasons: str, age: int = 10) -> None:
        """Replace the status file with a sample taken ``age`` seconds before now."""
        stamp = int(self.clock.now.timestamp()) - age
        self.status.write_text(header(level, reasons, epoch=stamp))

    def forget(self) -> None:
        """Let the cached read expire, as time passing does."""
        self.ticks.value += machinestatus_ttl() + 1

    def prompts(self) -> list[str]:
        engine = self.engines.get("alpha")
        return [] if engine is None else engine.prompts

    def drain(self) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def mail(self, body: str = "please work") -> None:
        self.scheduler.send(ANA, LineAddress("alpha"), body)

    def harness_notes(self, recipient: str) -> list[str]:
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == HARNESS and str(message.recipient) == recipient
        ]

    def start_run(self) -> RunStarted:
        latest = self.log.state.latest_runs.get(("line", "alpha"), "none")
        return asyncio.run(self.scheduler.start_run("alpha", "gate", latest))

    def runs_started(self) -> int:
        return len(self.log.state.runs)


def machinestatus_ttl() -> float:
    from dromon.scheduler import MACHINE_READ_SECONDS

    return MACHINE_READ_SECONDS


@pytest.fixture
def rig(tmp_path: Path) -> Generator[Rig]:
    target = Rig(tmp_path.resolve())
    yield target
    target.log.close()


def configured(rig: Rig) -> Rig:
    rig.scheduler.set_machine_status(rig.status)
    return rig


def finish_run(rig: Rig, started: RunStarted) -> None:
    children = rig.scheduler._run_children  # pyright: ignore[reportPrivateUsage]
    child = children.get(started.run_id)
    if child is not None:
        child.wait(timeout=20)
    asyncio.run(rig.scheduler.reconcile_runs())


# Runs.


def test_with_no_status_file_configured_nothing_is_read_or_deferred(rig: Rig) -> None:
    assert rig.scheduler.machine_status() is None
    assert rig.scheduler.machine_decision() == NO_DECISION
    assert rig.scheduler.machine_status_view() == {"configured": False}
    finish_run(rig, rig.start_run())
    assert rig.harness_notes("@ana") == []


@pytest.mark.parametrize(
    "reasons",
    [
        "kernel memory pressure is at warning",
        "swap use is 17000 MB",
        "the processor load is high",
    ],
)
def test_a_run_is_deferred_at_elevated_for_memory_swap_or_load(
    rig: Rig, reasons: str
) -> None:
    configured(rig)
    rig.write("elevated", reasons)
    with pytest.raises(runs.RunRefused, match="deferred") as caught:
        rig.start_run()
    assert reasons in str(caught.value) and "wake_after" in str(caught.value)
    assert "Nothing was started" in str(caught.value)
    assert rig.runs_started() == 0


def test_a_deferred_run_tells_the_operator_once_for_the_deferral(rig: Rig) -> None:
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    for _ in range(3):
        with pytest.raises(runs.RunRefused, match="deferred"):
            rig.start_run()
    (note,) = rig.harness_notes("@ana")
    assert "deferred a run of alpha" in note and "critical" in note
    # Once the status is ok the deferral ends and a later one tells again.
    rig.write("ok", "none")
    rig.forget()
    finish_run(rig, rig.start_run())
    rig.write("elevated", "swap use is 17000 MB")
    rig.forget()
    with pytest.raises(runs.RunRefused, match="deferred"):
        rig.start_run()
    assert len(rig.harness_notes("@ana")) == 2


def test_a_run_is_not_deferred_at_ok_or_when_only_disk_raised_the_level(
    rig: Rig,
) -> None:
    configured(rig)
    rig.write("ok", "none")
    finish_run(rig, rig.start_run())
    rig.write("elevated", "data volume has 45 GB free")
    rig.forget()
    finish_run(rig, rig.start_run())
    rig.write("critical", "data volume has 12 GB free")
    rig.forget()
    finish_run(rig, rig.start_run())
    assert rig.runs_started() == 3
    assert rig.harness_notes("@ana") == []


@pytest.mark.parametrize("state", ["stale", "missing", "garbled"])
def test_a_run_is_deferred_when_the_status_is_stale_missing_or_unreadable(
    rig: Rig, state: str
) -> None:
    configured(rig)
    if state == "stale":
        rig.write("ok", "none", age=STALE_SECONDS + 60)
    elif state == "garbled":
        rig.status.write_text("not a status file\n")
    with pytest.raises(runs.RunRefused, match="deferred") as caught:
        rig.start_run()
    assert "elevated" in str(caught.value)
    assert rig.runs_started() == 0


def test_a_commit_run_is_deferred_as_a_run_is(rig: Rig) -> None:
    configured(rig)
    rig.scheduler.set_runs({}, {"review": (sys.executable, "-c", "pass")}, [])
    rig.write("critical", "kernel memory pressure is critical")
    with pytest.raises(runs.RunRefused, match="deferred"):
        asyncio.run(rig.scheduler.start_run("alpha", "review", "none", commit="a" * 40))
    assert rig.runs_started() == 0


def test_a_malformed_request_is_refused_for_itself_and_not_deferred(rig: Rig) -> None:
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    with pytest.raises(runs.RunRefused) as caught:
        asyncio.run(rig.scheduler.start_run("alpha", "nope", "none"))
    assert "deferred" not in str(caught.value)
    assert rig.harness_notes("@ana") == []


def test_a_reason_holding_a_listed_term_is_not_shown(rig: Rig, tmp_path: Path) -> None:
    from dromon.disclosure import load_terms

    (tmp_path / "terms").write_text("secretword\n")
    rig.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    configured(rig)
    rig.write("elevated", "swap use has a secretword in it")
    with pytest.raises(runs.RunRefused) as caught:
        rig.start_run()
    assert "secretword" not in str(caught.value).casefold()
    assert all("secretword" not in note for note in rig.harness_notes("@ana"))


# The reads.


def test_the_file_is_read_again_only_after_the_cache_expires(rig: Rig) -> None:
    configured(rig)
    rig.write("ok", "none")
    assert rig.scheduler.machine_status() == parse(header("ok", "none"), NOW)
    rig.write("critical", "kernel memory pressure is critical")
    rig.ticks.value += machinestatus_ttl() - 1
    assert rig.scheduler.machine_status() is not None
    assert rig.scheduler.machine_status().level == "ok"  # type: ignore[union-attr]
    rig.ticks.value += 2
    assert rig.scheduler.machine_status().level == "critical"  # type: ignore[union-attr]


class HungReader:
    """A reader that holds its worker until released, as a file on a hung mount does,
    and counts the workers started."""

    def __init__(self, real: Callable[[Path, float], MachineStatus]) -> None:
        self.real = real
        self.release = threading.Event()
        self.hang = True
        self.started = 0

    def __call__(self, path: Path, now_epoch: float) -> MachineStatus:
        self.started += 1
        if self.hang:
            self.release.wait(30)
        return self.real(path, now_epoch)


@pytest.fixture
def hung(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Generator[HungReader]:
    monkeypatch.setattr("dromon.scheduler.MACHINE_WAIT_SECONDS", 0.05)
    reader = HungReader(machinestatus.read)
    rig.scheduler._machine_reader = reader  # pyright: ignore[reportPrivateUsage]
    configured(rig)
    yield reader
    reader.release.set()


def test_a_read_that_hangs_does_not_hold_the_caller(rig: Rig, hung: HungReader) -> None:
    rig.write("ok", "none")
    began = time.monotonic()
    status = rig.scheduler.machine_status()
    assert time.monotonic() - began < 5
    assert status is not None and status.level == "unreadable"
    assert "too long" in (status.problem or "")
    # An unreadable status is acted on as elevated, so a run is deferred and said so.
    assert rig.scheduler.machine_decision().defer_runs
    with pytest.raises(runs.RunRefused) as caught:
        rig.start_run()
    assert "deferred" in str(caught.value)


def test_a_read_that_hangs_starts_one_worker_and_no_more(
    rig: Rig, hung: HungReader
) -> None:
    rig.write("ok", "none")
    for _ in range(5):
        rig.scheduler.machine_status()
        rig.ticks.value += machinestatus_ttl() + 1
    assert hung.started == 1


def test_a_read_that_returns_late_is_taken_at_the_next_call(
    rig: Rig, hung: HungReader
) -> None:
    rig.write("critical", "kernel memory pressure is critical")
    first = rig.scheduler.machine_status()
    assert first is not None and first.level == "unreadable"
    hung.release.set()
    deadline = time.monotonic() + 5
    second = rig.scheduler.machine_status()
    while second is not None and second.level == "unreadable":
        assert time.monotonic() < deadline
        time.sleep(0.01)
        second = rig.scheduler.machine_status()
    assert second is not None and second.level == "critical"
    assert rig.scheduler.machine_decision().defer_turns


def test_a_reader_that_raises_is_a_status_that_cannot_be_relied_on(
    rig: Rig,
) -> None:
    def broken(path: Path, now_epoch: float) -> MachineStatus:
        raise RuntimeError("no")

    rig.scheduler._machine_reader = broken  # pyright: ignore[reportPrivateUsage]
    configured(rig)
    status = rig.scheduler.machine_status()
    assert status is not None and status.level == "unreadable"
    assert status.problem == "the read failed"
    assert rig.scheduler.machine_decision().defer_runs


def test_a_new_path_forgets_a_read_that_hangs(rig: Rig, hung: HungReader) -> None:
    rig.write("ok", "none")
    rig.scheduler.machine_status()
    hung.hang = False
    rig.scheduler.set_machine_status(rig.status)
    status = rig.scheduler.machine_status()
    assert status is not None and status.level == "ok"
    assert hung.started == 2


def test_a_new_path_forgets_what_was_read(rig: Rig) -> None:
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    assert rig.scheduler.machine_decision().defer_turns
    rig.scheduler.set_machine_status(None)
    assert rig.scheduler.machine_decision() == NO_DECISION
    rig.scheduler.set_machine_status(rig.status)
    assert rig.scheduler.machine_decision().defer_turns


def test_line_status_in_detail_gives_the_status_without_a_path(rig: Rig) -> None:
    configured(rig)
    rig.write("elevated", "swap use is 17000 MB")
    view = rig.scheduler.line_status("alpha", detail=True, lists=False)
    shown = view["machine_status"]
    assert shown == {
        "configured": True,
        "level": "elevated",
        "acted_on_as": "elevated",
        "age_seconds": 10,
        "reasons": ["swap use is 17000 MB"],
        "problem": None,
        "monitor": None,
        "defers_runs": True,
        "defers_turns": False,
    }
    assert str(rig.status) not in str(view)
    missing = configured(rig)
    rig.status.unlink()
    rig.forget()
    gone = missing.scheduler.machine_status_view()
    assert gone["level"] == "missing" and gone["acted_on_as"] == "elevated"


# Turns.


def test_a_turn_is_deferred_at_critical_and_starts_when_the_status_recovers(
    rig: Rig,
) -> None:
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    rig.mail()
    rig.drain()
    assert rig.prompts() == []
    (to_line,) = rig.harness_notes("alpha")
    assert "deferred your turn" in to_line and "critical" in to_line
    (to_operator,) = rig.harness_notes("@ana")
    assert "deferred the turns" in to_operator and "alpha" in to_operator
    # More ticks while it lasts tell nobody more.
    rig.drain()
    assert rig.harness_notes("alpha") == [to_line]
    assert rig.harness_notes("@ana") == [to_operator]
    # The status recovers, a minute later, and the turn starts with what it waited.
    rig.clock.now += timedelta(seconds=90)
    rig.write("ok", "none")
    rig.forget()
    rig.drain()
    (prompt, *rest) = rig.prompts()
    assert not rest
    assert "Your turn was deferred for 90 seconds by the status." in prompt
    assert "please work" not in prompt, "bodies stay in the mailbox"


def test_a_turn_proceeds_at_elevated_with_a_warning(rig: Rig) -> None:
    configured(rig)
    rig.write("elevated", "swap use is 17000 MB")
    rig.mail()
    rig.drain()
    (prompt,) = rig.prompts()
    assert prompt.startswith("Dromon note: The machine status is elevated")
    assert "swap use is 17000 MB" in prompt and "start nothing heavy" in prompt
    assert "deferred" not in prompt
    assert rig.harness_notes("@ana") == []


@pytest.mark.parametrize("state", ["stale", "missing"])
def test_a_turn_proceeds_with_a_warning_when_the_status_is_stale_or_missing(
    rig: Rig, state: str
) -> None:
    configured(rig)
    if state == "stale":
        rig.write("ok", "none", age=STALE_SECONDS + 60)
    rig.mail()
    rig.drain()
    (prompt,) = rig.prompts()
    assert prompt.startswith("Dromon note: ")
    assert "treated as elevated" in prompt


def test_a_turn_proceeds_unremarked_at_ok_and_with_no_file_configured(rig: Rig) -> None:
    rig.mail()
    rig.drain()
    assert "machine status" not in rig.prompts()[0]
    configured(rig)
    rig.write("ok", "none")
    rig.mail("again")
    rig.forget()
    rig.drain()
    assert "machine status" not in rig.prompts()[1]


def test_a_critical_status_that_raised_only_disk_still_defers_a_turn(rig: Rig) -> None:
    """The operator's wording leaves this open, and the stricter reading is taken."""
    configured(rig)
    rig.write("critical", "data volume has 12 GB free")
    rig.mail()
    rig.drain()
    assert rig.prompts() == []
    assert rig.harness_notes("alpha")


def test_a_deferral_does_not_keep_the_loop_ticking(rig: Rig) -> None:
    """Once the line and the operator are told, the loop rests, and reads the status
    again at its next rest, so a deferral costs no more than a rest each minute."""
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    rig.mail()
    rig.drain()
    assert rig.scheduler._work_waiting() is False  # pyright: ignore[reportPrivateUsage]
    rig.write("ok", "none")
    rig.forget()
    assert rig.scheduler._work_waiting() is True  # pyright: ignore[reportPrivateUsage]


def test_mail_already_answered_is_delivered_during_a_deferral_without_a_turn(
    rig: Rig,
) -> None:
    configured(rig)
    rig.write("critical", "kernel memory pressure is critical")
    sent = rig.scheduler.send(ANA, LineAddress("alpha"), "task")
    rig.scheduler.send(LineAddress("alpha"), ANA, "done", reply_to=sent)
    rig.drain()
    assert rig.prompts() == []
    assert rig.log.state.messages[sent].delivered
    assert rig.harness_notes("@ana") == []


def test_the_machine_notice_is_not_given_to_a_line_never_deferred(rig: Rig) -> None:
    configured(rig)
    rig.write("ok", "none")
    assert rig.scheduler._machine_notice("alpha") is None  # pyright: ignore[reportPrivateUsage]


def test_a_status_that_turns_critical_defers_only_the_next_start(rig: Rig) -> None:
    """A turn that already ran is left as it is, and the next mail waits."""
    configured(rig)
    rig.write("ok", "none")
    rig.mail()
    rig.drain()
    assert len(rig.prompts()) == 1
    rig.write("critical", "kernel memory pressure is critical")
    rig.forget()
    rig.mail("a second")
    rig.drain()
    assert len(rig.prompts()) == 1
    assert len(rig.harness_notes("alpha")) == 1


# The daemon applies the key at reload and shows it.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def test_a_reload_applies_the_key_and_the_settings_show_it(
    tmp_path: Path, short_runtime: Path
) -> None:
    import json

    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.paths import project_paths
    from dromon.projectconfig import config_path

    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    file = config_path(paths.state_dir)
    status_file = tmp_path / "machine-status"
    stamp = int(START.timestamp())
    status_file.write_text(
        header("critical", "kernel memory pressure is critical", epoch=stamp)
    )
    file.write_text(f'version = 1\n[daemon]\nmachine_status = "{status_file}"\n')
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True, wall_clock=Clock())
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        target = Daemon(
            paths,
            log,
            "2026-11-11T00:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=True,
        )

        def ask(op: str) -> dict[str, object]:
            line: dict[str, object] = {"id": "r", "op": op, "v": 1, "args": {}}
            response = target.respond((json.dumps(line) + "\n").encode())
            assert response.ok and response.result is not None, response.error_message
            return response.result

        assert scheduler.machine_status() is None
        ask("reload_project")
        assert scheduler.machine_decision().defer_turns
        entries = cast(list[dict[str, object]], ask("settings")["project"])
        shown = next(e for e in entries if e["key"] == "daemon.machine_status")
        assert shown["value"] == str(status_file)
        file.write_text("version = 1\n")
        ask("reload_project")
        assert scheduler.machine_status() is None
        assert scheduler.machine_decision() == NO_DECISION


# Round 16 review, point 1. A monitor that stopped is named where the deferral is.

RESTORE = "restarting the monitor"
SETTING = "removing daemon.machine_status"


def test_a_stale_status_names_the_stopped_monitor_and_its_last_sample(rig: Rig) -> None:
    configured(rig)
    rig.write("ok", "none", age=STALE_SECONDS + 60)
    sampled = START - timedelta(seconds=STALE_SECONDS + 60)
    with pytest.raises(runs.RunRefused, match="deferred") as caught:
        rig.start_run()
    refusal = str(caught.value)
    assert "monitor looks stopped" in refusal and "is stale" in refusal
    assert f"last sample at {sampled:%Y-%m-%dT%H:%M:%SZ}" in refusal
    assert RESTORE in refusal and SETTING in refusal
    (to_operator,) = rig.harness_notes("@ana")
    assert "monitor looks stopped" in to_operator and RESTORE in to_operator
    assert SETTING in to_operator


def test_a_missing_status_is_dated_by_the_daemons_first_sight(rig: Rig) -> None:
    configured(rig)
    with pytest.raises(runs.RunRefused):
        rig.start_run()
    rig.clock.now += timedelta(seconds=600)
    rig.forget()
    with pytest.raises(runs.RunRefused) as caught:
        rig.start_run()
    first = f"first saw it so at {START:%Y-%m-%dT%H:%M:%SZ}"
    assert "its file is missing" in str(caught.value) and first in str(caught.value)
    assert rig.scheduler.machine_status_view()["monitor"] == (
        rig.scheduler._monitor_trouble(  # pyright: ignore[reportPrivateUsage]
            MachineStatus("missing", problem="no file is there")
        )
    )


def test_an_unreadable_status_is_named_the_same_way(rig: Rig) -> None:
    configured(rig)
    rig.status.write_text("not a status file\n")
    with pytest.raises(runs.RunRefused) as caught:
        rig.start_run()
    assert "its file is unreadable" in str(caught.value) and RESTORE in str(
        caught.value
    )


@pytest.mark.parametrize(
    ("level", "reasons"),
    [("ok", "none"), ("elevated", "swap use is 17000 MB"), ("critical", "swap use")],
)
def test_a_fresh_status_names_no_stopped_monitor(
    rig: Rig, level: str, reasons: str
) -> None:
    configured(rig)
    rig.write(level, reasons)
    rig.mail()
    rig.drain()
    everything = [
        *rig.prompts(),
        *rig.harness_notes("@ana"),
        *rig.harness_notes("alpha"),
    ]
    assert not any("monitor looks stopped" in text for text in everything)
    assert rig.scheduler.machine_status_view()["monitor"] is None


def test_a_turn_that_proceeds_on_a_stale_status_names_the_monitor(
    rig: Rig,
) -> None:
    """A stale file only warns a turn, so a deferred turn needs a critical status,
    and the cause is then absent. The warning a turn proceeds with names it."""
    configured(rig)
    rig.write("ok", "none", age=STALE_SECONDS + 60)
    rig.mail()
    rig.drain()
    (prompt,) = rig.prompts()
    assert "monitor looks stopped" in prompt and RESTORE in prompt


def test_the_cause_goes_when_the_monitor_writes_again(rig: Rig) -> None:
    configured(rig)
    assert rig.scheduler.machine_status_view()["monitor"] is not None
    rig.write("ok", "none")
    rig.forget()
    assert rig.scheduler.machine_status_view()["monitor"] is None
    rig.status.unlink()
    rig.clock.now += timedelta(seconds=30)
    rig.forget()
    again = rig.scheduler.machine_status_view()["monitor"]
    assert isinstance(again, str)
    assert f"first saw it so at {rig.clock.now:%Y-%m-%dT%H:%M:%SZ}" in again


# Round 16 review, point 2. The monitor's text is screened where a line reads it.


def test_the_reasons_a_line_reads_in_line_status_are_screened(
    rig: Rig, tmp_path: Path
) -> None:
    from dromon.disclosure import load_terms
    from dromon.scheduler import MACHINE_WITHHELD

    (tmp_path / "terms").write_text("secretword\n")
    rig.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    configured(rig)
    rig.write("elevated", "swap use is 17000 MB; a SecretWord in this one")
    view = rig.scheduler.line_status("alpha", detail=True, lists=False)
    shown = cast(dict[str, Any], view["machine_status"])
    assert shown["reasons"] == ["swap use is 17000 MB", MACHINE_WITHHELD]
    assert "secretword" not in str(view).casefold()


def test_a_problem_holding_a_listed_term_is_withheld_in_line_status(
    rig: Rig, tmp_path: Path
) -> None:
    from dromon.disclosure import load_terms
    from dromon.scheduler import MACHINE_WITHHELD

    (tmp_path / "terms").write_text("secretword\n")
    rig.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    configured(rig)
    rig.scheduler._machine_reader = (  # pyright: ignore[reportPrivateUsage]
        lambda _path, _now: MachineStatus("unreadable", problem="a secretword failed")
    )
    shown = rig.scheduler.machine_status_view()
    assert shown["problem"] == MACHINE_WITHHELD
    assert "secretword" not in str(shown).casefold()
