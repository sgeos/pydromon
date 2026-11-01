"""A line's processes: the mark its engines carry, ``list_processes`` and
``stop_process``, on fake process tables, and the platform reader behind them."""

import asyncio
import json
import signal
import subprocess
import threading
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import processes
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine
from dromon.eventlog import EventLog
from dromon.processes import (
    MARK_VARIABLE,
    MAX_COMMAND_CHARACTERS,
    ProcessInfo,
    ProcessRefused,
    ProcessWorker,
    MAX_PENDING_PROCESS_CALLS,
    SystemProcesses,
    line_processes,
    marked_processes,
    parse_ps,
    read_environment,
    stop_line_process,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory

HAIKU = "claude-haiku-5-5"
DAEMON = 1000
LAUNCHER = 1500
"""The launcher the daemon starts, whose child is the engine."""
MARK = "alpha.0123abcd"
OTHER = "beta.0123abcd"


def test_process_worker_refuses_calls_past_its_bounded_queue() -> None:
    worker = ProcessWorker()
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        entered.set()
        release.wait(5)
        return "finished"

    async def exercise() -> None:
        running = asyncio.create_task(worker.run(blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        queued = [
            asyncio.create_task(worker.run(lambda index=index: str(index)))
            for index in range(MAX_PENDING_PROCESS_CALLS)
        ]
        await asyncio.sleep(0)
        with pytest.raises(ProcessRefused, match="queue is full"):
            await worker.run(lambda: str("overflow"))
        release.set()
        assert await running == "finished"
        assert await asyncio.gather(*queued) == [
            str(index) for index in range(len(queued))
        ]

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        assert worker.close()


def test_process_worker_skips_a_queued_call_after_its_caller_cancels() -> None:
    worker = ProcessWorker()
    entered = threading.Event()
    release = threading.Event()
    ran: list[str] = []

    def blocked() -> None:
        entered.set()
        release.wait(5)

    async def exercise() -> None:
        running = asyncio.create_task(worker.run(blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        queued = asyncio.create_task(worker.run(lambda: ran.append("later")))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await running
        await asyncio.sleep(0.02)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        assert worker.close()
    assert ran == []


def test_process_worker_close_rejects_new_calls() -> None:
    worker = ProcessWorker()
    assert worker.close()

    async def exercise() -> None:
        with pytest.raises(ProcessRefused, match="worker is stopped"):
            await worker.run(lambda: None)

    asyncio.run(exercise())


def test_process_worker_reports_when_a_running_call_outlasts_close_bound() -> None:
    worker = ProcessWorker()
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        entered.set()
        release.wait(5)
        return "finished"

    async def exercise() -> None:
        running = asyncio.create_task(worker.run(blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        assert worker.close(timeout=0.01) is False
        with pytest.raises(ProcessRefused, match="worker is stopped"):
            await worker.run(lambda: "late")
        release.set()
        assert await running == "finished"
        thread = worker._thread  # pyright: ignore[reportPrivateUsage]
        assert thread is not None
        await asyncio.to_thread(thread.join, 1)
        assert not thread.is_alive()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        worker.close(timeout=1)


class FakeTable:
    """A process table the test writes, which records the signals sent."""

    def __init__(
        self,
        rows: Sequence[ProcessInfo],
        environments: dict[int, list[str] | None],
    ) -> None:
        self.rows = list(rows)
        self.envs = environments
        self.sent: list[tuple[int, int]] = []
        self.gone: set[int] = set()
        self.forbidden: set[int] = set()

    def own_processes(self) -> Sequence[ProcessInfo]:
        return self.rows

    def has_variable(self, pid: int, assignment: str) -> bool:
        entries = self.envs.get(pid)
        return entries is not None and assignment in entries

    def environments(self) -> dict[int, list[str]]:
        return {pid: env for pid, env in self.envs.items() if env is not None}

    def send(self, pid: int, signal_number: int) -> None:
        if pid in self.gone:
            raise ProcessLookupError(pid)
        if pid in self.forbidden:
            raise PermissionError(pid)
        self.sent.append((pid, signal_number))


def mark(value: str = MARK) -> list[str]:
    return ["PATH=/bin", f"{MARK_VARIABLE}={value}", "HOME=/home"]


@pytest.fixture
def table() -> FakeTable:
    return FakeTable(
        [
            ProcessInfo(DAEMON, 1, "01:00:00", "dromond"),
            ProcessInfo(LAUNCHER, DAEMON, "59:00", "python -I -m dromon.launcher"),
            ProcessInfo(1001, LAUNCHER, "59:00", "claude --engine"),
            ProcessInfo(2001, 1001, "02:00", "uv run pytest " + "x" * 300),
            ProcessInfo(2002, 1, "1-02:03:04", "nohup ./gate.sh"),
            ProcessInfo(2003, 1001, "00:05", "sleep 100"),
            ProcessInfo(2004, 1, "00:07", "unreadable"),
            ProcessInfo(2005, 1, "00:09", "prefix trick"),
        ],
        {
            DAEMON: mark(),
            LAUNCHER: mark(),
            1001: mark(),
            2001: mark(),
            2002: mark(),
            2003: mark(OTHER),
            2004: None,
            2005: [f"{MARK_VARIABLE}={MARK}extra"],
        },
    )


def test_a_line_lists_only_its_own_marked_processes(table: FakeTable) -> None:
    found = line_processes(table, MARK, DAEMON)
    assert [p["pid"] for p in found] == [2001, 2002]
    assert found[1] == {
        "pid": 2002,
        "task_id": "pid:2002",
        "elapsed": "1-02:03:04",
        "command": "nohup ./gate.sh",
    }
    assert len(cast(str, found[0]["command"])) == MAX_COMMAND_CHARACTERS
    assert [p["pid"] for p in line_processes(table, OTHER, DAEMON)] == [2003]


def test_stopping_sends_term_or_kill_to_the_lines_own_process(table: FakeTable) -> None:
    assert stop_line_process(table, MARK, DAEMON, 2001, False) == (
        "sent SIGTERM to process 2001"
    )
    assert stop_line_process(table, MARK, DAEMON, 2002, True) == (
        "sent SIGKILL to process 2002"
    )
    assert table.sent == [(2001, signal.SIGTERM), (2002, signal.SIGKILL)]


@pytest.mark.parametrize(
    ("pid", "message"),
    [
        (2003, "is not one of yours"),
        (2004, "is not one of yours"),
        (2005, "is not one of yours"),
        (4242, "is not one of yours"),
        (DAEMON, "the daemon or a line's engine"),
        (1001, "the daemon or a line's engine"),
        (1, "above 1"),
        (0, "above 1"),
        (-5, "above 1"),
    ],
)
def test_stopping_any_other_process_is_refused(
    table: FakeTable, pid: int, message: str
) -> None:
    with pytest.raises(ProcessRefused, match=message):
        stop_line_process(table, MARK, DAEMON, pid, False)
    assert table.sent == []


def test_the_mark_is_read_again_at_the_moment_of_stopping(table: FakeTable) -> None:
    assert [p["pid"] for p in line_processes(table, MARK, DAEMON)] == [2001, 2002]
    table.envs[2001] = mark(OTHER)
    with pytest.raises(ProcessRefused, match="is not one of yours"):
        stop_line_process(table, MARK, DAEMON, 2001, False)
    assert table.sent == []


def test_a_process_that_ended_or_cannot_be_signalled_is_reported(
    table: FakeTable,
) -> None:
    table.gone.add(2001)
    with pytest.raises(ProcessRefused, match="has already ended"):
        stop_line_process(table, MARK, DAEMON, 2001, False)
    table.forbidden.add(2002)
    with pytest.raises(ProcessRefused, match="cannot be signalled"):
        stop_line_process(table, MARK, DAEMON, 2002, True)


# The system table and its platform reader.


def test_ps_rows_are_parsed_and_malformed_ones_skipped() -> None:
    output = (
        "  101     1      05:00 /bin/zsh -l\n"
        "  102   101   01:02:03 python -m dromon.cli\n"
        "  103   101 00:01\n"
        "garbage line here\n"
        "\n"
    )
    assert parse_ps(output) == [
        ProcessInfo(101, 1, "05:00", "/bin/zsh -l"),
        ProcessInfo(102, 101, "01:02:03", "python -m dromon.cli"),
        ProcessInfo(103, 101, "00:01", ""),
    ]


class Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_the_system_table_lists_through_ps_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> Completed:
        calls.append(command)
        return Completed("  7  1  00:01 sleep 9\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SystemProcesses().own_processes() == [ProcessInfo(7, 1, "00:01", "sleep 9")]
    assert calls[0][-1] == "pid=,ppid=,etime=,args="

    def broken(command: list[str], **_: object) -> Completed:
        raise OSError("ps is not available")

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(ProcessRefused, match="cannot list processes"):
        SystemProcesses().own_processes()


def test_the_environment_is_read_per_platform_and_none_means_not_the_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def marked_ps(command: list[str], **_: object) -> Completed:
        return Completed(f"sleep 9 PATH=/bin {MARK_VARIABLE}={MARK}\n")

    def failed_ps(command: list[str], **_: object) -> Completed:
        return Completed("", 1)

    environ = f"PATH=/bin\x00{MARK_VARIABLE}={MARK}\x00".encode()

    def proc_environ(self: Path) -> bytes:
        return environ

    def unreadable(self: Path) -> bytes:
        raise PermissionError(str(self))

    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", marked_ps)
    assert read_environment(7) == ["sleep", "9", "PATH=/bin", f"{MARK_VARIABLE}={MARK}"]
    assert SystemProcesses().has_variable(7, f"{MARK_VARIABLE}={MARK}")
    assert not SystemProcesses().has_variable(7, f"{MARK_VARIABLE}={MARK[:-1]}")
    monkeypatch.setattr(subprocess, "run", failed_ps)
    assert read_environment(7) is None
    assert not SystemProcesses().has_variable(7, f"{MARK_VARIABLE}={MARK}")
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", proc_environ)
    assert f"{MARK_VARIABLE}={MARK}" in cast(list[str], read_environment(7))
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert read_environment(7) is None
    monkeypatch.setattr(processes.sys, "platform", "sunos5")
    assert read_environment(7) is None


# The engines and the tools.


def test_a_claude_line_starts_with_its_mark(tmp_path: Path) -> None:
    driver = Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)
    marked = ClaudeCodeLine(driver, tmp_path, mark=MARK)
    environment = marked.options().env
    assert environment[MARK_VARIABLE] == MARK
    assert MARK_VARIABLE not in ClaudeCodeLine(driver, tmp_path).options().env
    build = engine_factory(str(tmp_path))
    engine = build(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, mark=MARK), [])
    assert isinstance(engine, ClaudeCodeLine) and engine.mark == MARK


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path, table: FakeTable) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True, processes=table)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        yield found


def call(
    scheduler: Scheduler, line_id: str, name: str, args: dict[str, object]
) -> tuple[str, bool]:
    tool = next(t for t in scheduler.own_tools(line_id) if t.name == name)

    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


def test_marks_are_per_line_and_per_daemon_run(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    alpha, beta = scheduler.line_mark("alpha"), scheduler.line_mark("beta")
    assert alpha.startswith("alpha.") and beta.startswith("beta.")
    assert alpha.split(".")[1] != beta.split(".")[1]
    assert scheduler.line_mark("alpha") == alpha
    with EventLog(tmp_path / "other.sqlite") as log:
        assert Scheduler(log, no_engine).line_mark("alpha") != alpha


def test_a_line_cannot_present_another_lines_mark(
    scheduler: Scheduler, table: FakeTable
) -> None:
    """Alpha knows only its own token, so the most it can forge for beta is beta's
    name with alpha's token, which is not beta's mark."""
    alpha = scheduler.line_mark("alpha")
    forged = "beta." + alpha.split(".")[1]
    table.envs[2001] = mark(alpha)
    table.envs[2002] = mark(forged)
    beta = scheduler.line_mark("beta")
    assert forged != beta
    assert line_processes(table, beta, DAEMON) == []
    with pytest.raises(ProcessRefused, match="is not one of yours"):
        stop_line_process(table, beta, DAEMON, 2002, False)
    assert [p["pid"] for p in line_processes(table, alpha, DAEMON)] == [2001]
    assert table.sent == []


def test_the_tools_list_and_stop_only_the_calling_lines_processes(
    scheduler: Scheduler, table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = scheduler.line_mark("alpha")
    table.envs[2001] = mark(alpha)
    table.envs[2003] = mark(scheduler.line_mark("beta"))
    table.rows[0] = ProcessInfo(4000, 1, "00:00", "elsewhere")
    import os

    monkeypatch.setattr(os, "getpid", lambda: DAEMON)
    text, error = call(scheduler, "alpha", "list_processes", {})
    assert not error and [p["pid"] for p in json.loads(text)] == [2001]
    text, error = call(scheduler, "alpha", "stop_process", {"pid": 2003})
    assert error and "is not one of yours" in text
    text, error = call(scheduler, "alpha", "stop_process", {"pid": 2001, "force": True})
    assert (text, error) == ("sent SIGKILL to process 2001", False)
    assert table.sent == [(2001, signal.SIGKILL)]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"pid": "2001"}, "pid must be a whole number"),
        ({"pid": 2001.0}, "pid must be a whole number"),
        ({"pid": True}, "pid must be a whole number"),
        ({}, "pid must be a whole number"),
        ({"pid": 2001, "force": "yes"}, "force must be true or false"),
        ({"pid": 2001, "signal": 9}, "unexpected argument signal"),
    ],
)
def test_stop_process_refuses_bad_arguments(
    scheduler: Scheduler, table: FakeTable, args: dict[str, object], message: str
) -> None:
    text, error = call(scheduler, "alpha", "stop_process", args)
    assert error and message in text
    assert table.sent == []


def test_list_processes_takes_no_arguments_and_reports_a_listing_failure(
    scheduler: Scheduler, table: FakeTable
) -> None:
    text, error = call(scheduler, "alpha", "list_processes", {"all": True})
    assert error and "unexpected argument all" in text

    def broken() -> Sequence[ProcessInfo]:
        raise ProcessRefused("cannot list processes: ps is not available")

    table.own_processes = broken  # type: ignore[method-assign]
    text, error = call(scheduler, "alpha", "list_processes", {})
    assert error and "cannot list processes" in text
    assert text.startswith("list_processes is unavailable: ")


def test_a_table_whose_environments_failed_is_unavailable_and_never_an_empty_list(
    scheduler: Scheduler, table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 14, item 2. A ps that timed out gave an empty mapping of environments,
    and an empty mapping listed nothing, which a line reads as no process running."""
    import os

    monkeypatch.setattr(os, "getpid", lambda: DAEMON)

    def timed_out() -> dict[int, list[str]]:
        raise ProcessRefused("ps did not answer within 10 seconds")

    table.environments = timed_out  # type: ignore[method-assign]
    text, error = call(scheduler, "alpha", "list_processes", {})
    assert error
    assert text == (
        "list_processes is unavailable: ps did not answer within 10 seconds"
    )
    assert text != "[]"


def test_no_readable_environment_at_all_is_unavailable_and_not_an_empty_list(
    table: FakeTable,
) -> None:
    """Round 14, item 2. Processes exist and none can be attributed, so the reading
    failed, which is not the same as a line with nothing running."""
    for pid in table.envs:
        table.envs[pid] = None
    with pytest.raises(ProcessRefused, match="none of the 8 processes"):
        line_processes(table, MARK, DAEMON)
    with pytest.raises(ProcessRefused, match="none of the 8 processes"):
        marked_processes(table, DAEMON)


def test_a_working_table_with_nothing_of_the_lines_lists_an_empty_list(
    table: FakeTable,
) -> None:
    """The counterpart of the above. An empty list is still the answer for a line
    with nothing running, when the environments could be read."""
    assert line_processes(table, "alpha.nothing", DAEMON) == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_strict_environment_reading_names_why_it_failed(
    monkeypatch: pytest.MonkeyPatch, platform: str, tmp_path: Path
) -> None:
    monkeypatch.setattr(processes.sys, "platform", platform)
    monkeypatch.setattr(processes, "PROC", tmp_path / "missing")

    def timed_out(command: list[str], **_: object) -> Completed:
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(ProcessRefused):
        processes.read_environments(1, strict=True)
    assert processes.read_environments(1) == {}
    if platform == "darwin":
        with pytest.raises(ProcessRefused, match="did not answer within 10 seconds"):
            processes.read_environments(1, strict=True)

        def cannot_run(command: list[str], **_: object) -> Completed:
            raise OSError("not permitted")

        monkeypatch.setattr(subprocess, "run", cannot_run)
        with pytest.raises(ProcessRefused, match="ps cannot be run: not permitted"):
            processes.read_environments(1, strict=True)

        def denied(command: list[str], **_: object) -> Completed:
            return Completed("", 2, "denied\n")

        monkeypatch.setattr(subprocess, "run", denied)
        with pytest.raises(ProcessRefused, match="status 2 and said denied"):
            processes.read_environments(1, strict=True)


def test_strict_environment_reading_is_refused_on_an_unknown_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "plan9")
    with pytest.raises(ProcessRefused, match="does not let the daemon read"):
        processes.read_environments(1, strict=True)
    assert processes.read_environments(1) == {}


def test_the_system_table_environments_are_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "darwin")

    def timed_out(command: list[str], **_: object) -> Completed:
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(ProcessRefused):
        SystemProcesses().environments()


def test_all_environments_are_read_in_one_call_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def one_ps(command: list[str], **_: object) -> Completed:
        calls.append(command)
        return Completed(
            f"  7 sleep 9 PATH=/bin {MARK_VARIABLE}={MARK}\n"
            "  8 zsh HOME=/home\n"
            "garbage\n"
        )

    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", one_ps)
    found = processes.read_environments(501)
    assert len(calls) == 1 and "-U" in calls[0] and "501" in calls[0]
    assert found[7][-1] == f"{MARK_VARIABLE}={MARK}" and found[8] == [
        "zsh",
        "HOME=/home",
    ]
    assert set(found) == {7, 8}

    def broken(command: list[str], **_: object) -> Completed:
        raise OSError("no ps")

    monkeypatch.setattr(subprocess, "run", broken)
    assert processes.read_environments(501) == {}
    monkeypatch.setattr(processes.sys, "platform", "sunos5")
    assert processes.read_environments(501) == {}


def test_linux_environments_come_from_the_users_proc_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    for pid, environ in (("7", f"{MARK_VARIABLE}={MARK}\x00A=1"), ("8", "B=2")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "environ").write_bytes(environ.encode())
    (tmp_path / "self").mkdir()
    (tmp_path / "9").mkdir()
    monkeypatch.setattr(processes, "PROC", tmp_path)
    monkeypatch.setattr(processes.sys, "platform", "linux")
    found = processes.read_environments(os.getuid())
    assert found == {7: [f"{MARK_VARIABLE}={MARK}", "A=1"], 8: ["B=2"]}
    assert processes.read_environments(os.getuid() + 1) == {}


def test_the_process_tools_run_off_the_event_loop(
    scheduler: Scheduler, table: FakeTable
) -> None:
    import threading

    threads: list[int] = []
    rows = table.rows

    def recording() -> Sequence[ProcessInfo]:
        threads.append(threading.get_ident())
        return rows

    table.own_processes = recording  # type: ignore[method-assign]
    main = threading.get_ident()
    call(scheduler, "alpha", "list_processes", {})
    call(scheduler, "alpha", "stop_process", {"pid": 2001})
    assert len(threads) == 2 and main not in threads


# The start-time self-test, and the explanation when it fails.


class FakeChild:
    pid = 4242

    def __init__(self) -> None:
        self.killed = False
        self.environment: dict[str, str] = {}

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> FakeChild:
    child = FakeChild()

    def popen(command: list[str], **kwargs: object) -> FakeChild:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        child.environment = environment
        return child

    monkeypatch.setattr(processes.subprocess, "Popen", popen)

    def token_hex(size: int) -> str:
        return "t"

    def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(processes.secrets, "token_hex", token_hex)
    monkeypatch.setattr(processes.time, "sleep", no_pause)
    return child


@pytest.mark.parametrize(
    ("listed", "single", "works"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_the_self_test_needs_both_readers_to_see_the_child(
    probe: FakeChild,
    monkeypatch: pytest.MonkeyPatch,
    listed: bool,
    single: bool,
    works: bool,
) -> None:
    monkeypatch.setenv("DROMON_TEST_ONLY", "must-not-reach-probe")

    seen = ["DROMON_PROBE=t"]

    def environments(uid: int) -> dict[int, list[str]]:
        return {4242: seen} if listed else {4242: ["OTHER=1"]}

    def environment(pid: int) -> list[str] | None:
        return seen if single else None

    monkeypatch.setattr(processes, "read_environments", environments)
    monkeypatch.setattr(processes, "read_environment", environment)
    assert processes.environment_reading_works() is works
    assert probe.killed
    assert processes.PROBE_VARIABLE in probe.environment
    assert set(probe.environment) <= {
        *processes.BASE_ENVIRONMENT,
        processes.PROBE_VARIABLE,
    }
    assert "DROMON_TEST_ONLY" not in probe.environment


def test_a_child_that_cannot_start_fails_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(command: list[str], **_: object) -> FakeChild:
        raise OSError("no sleep")

    monkeypatch.setattr(processes.subprocess, "Popen", refused)
    assert processes.environment_reading_works() is False


def test_the_real_self_test_answers_and_leaves_no_child() -> None:
    assert isinstance(processes.environment_reading_works(), bool)


def test_where_processes_cannot_be_identified_the_tools_explain(
    tmp_path: Path, table: FakeTable
) -> None:
    calls: list[str] = []
    rows = table.rows

    def counted() -> Sequence[ProcessInfo]:
        calls.append("listed")
        return rows

    table.own_processes = counted  # type: ignore[method-assign]
    with EventLog(tmp_path / "events.sqlite") as log:
        blind = Scheduler(
            log,
            no_engine,
            hold_wakes=True,
            processes=table,
            processes_identifiable=False,
        )
        blind.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        cases: list[tuple[str, dict[str, object]]] = [
            ("list_processes", {}),
            ("stop_process", {"pid": 2001}),
        ]
        for name, args in cases:
            text, error = call(blind, "alpha", name, args)
            assert (text, error) == (
                processes.unavailable(name, processes.UNIDENTIFIABLE),
                True,
            )
        status = blind.line_status("alpha")["processes"]
        assert status == {
            "identifiable": False,
            "note": processes.UNIDENTIFIABLE,
            "stop_policy": "kill_all",
            "left": None,
            "stop_unverified": None,
            "walk_limit": None,
        }
    assert calls == [] and table.sent == []


def test_where_processes_can_be_identified_line_status_says_so(
    scheduler: Scheduler,
) -> None:
    assert scheduler.line_status("alpha")["processes"] == {
        "identifiable": True,
        "note": None,
        "stop_policy": "kill_all",
        "left": None,
        "stop_unverified": None,
        "walk_limit": None,
    }
