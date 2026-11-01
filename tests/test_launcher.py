"""The launcher each harness runs under, and the daemon's side of it.

The harness here is a small Python script, never a real harness. **Rows 1 and 2 of the
T3 Code review, and the operator's decision B.** A stop, or the daemon's death, ends
the harness and, by the line's stop policy, what it started, or leaves that running
and reports it.

Inside an agent sandbox ``ps`` cannot run, so the launcher reaches the harness's group
only. The tests that need the walk of the process table are skipped there, with a
reason, and run in continuous integration.
"""

import asyncio
import contextlib
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from dromon import launcher, processes
from dromon.launcher import (
    LAUNCH_MARK_VARIABLE,
    LAUNCH_VARIABLES,
    PARENT_VARIABLE,
    REPORT_VARIABLE,
    STOP_POLICY_VARIABLE,
    TARGET_VARIABLE,
    ExitWatch,
    LaunchRecord,
    LeftReport,
    StopPolicy,
    Swept,
    arm_death_signal,
    collect_reports,
    death_signal,
    launched_at,
    launcher_running,
    line_of,
    parse_report,
    policy_of,
    read_launches,
    relayable,
    sweep_launches,
    version_target,
    version_environment,
    walk_gap,
    walk_unavailable,
    wrap,
)
from dromon.processes import (
    MARK_VARIABLE,
    TreeReadFailure,
    TreeRow,
    descendants,
    parse_tree,
)

MARK = "alpha.0123abcd"
TEST_SOURCE = str(Path(__file__).resolve().parents[1] / "src")
LAUNCHER_RUNNER = (
    "import runpy, sys; "
    f"sys.path.insert(0, {TEST_SOURCE!r}); "
    "runpy.run_module('dromon.launcher', run_name='__main__')"
)


def launcher_command(*arguments: str) -> list[str]:
    """Run this checkout's launcher source despite Python's isolated mode."""
    return [sys.executable, "-I", "-c", LAUNCHER_RUNNER, *arguments]


HARNESS = (
    "import json, os, subprocess, sys, time\n"
    "stay = subprocess.Popen(['/bin/sleep', '60'])\n"
    "away = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\n"
    "names = sorted(k for k in os.environ if k.startswith('DROMON_'))\n"
    "sys.stdout.write(json.dumps({'stay': stay.pid, 'away': away.pid,"
    " 'unapproved': os.environ.get('DAEMON_ONLY_SENTINEL'),"
    " 'names': names, 'engine_environment': {name: os.environ.get(name) for name in ("
    "'USER', 'LOGNAME', 'ENABLE_TOOL_SEARCH', 'CLAUDE_CODE_ENTRYPOINT',"
    "'CLAUDE_AGENT_SDK_VERSION',"
    "'CLAUDE_CODE_SDK_READS_SESSION_STATE', "
    "'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING', 'CLAUDE_CONFIG_DIR')}}"
    ") + '\\n')\n"
    "sys.stdout.flush()\n"
)
"""A harness that starts one child in its own group and one that leaves it, reports
their pids and the Dromon variables it sees, and then waits or exits."""


def ps_runs() -> bool:
    try:
        found = subprocess.run(  # noqa: S603
            ["ps", "-o", "pid=", "-p", str(os.getpid())],  # noqa: S607
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False
    return found.returncode == 0


NEEDS_PS = pytest.mark.skipif(
    not ps_runs(), reason="ps cannot run here, as inside an agent sandbox"
)

NEEDS_SUBREAPER = pytest.mark.skipif(
    sys.platform != "linux",
    reason="only Linux has a child subreaper. Elsewhere the gap is recorded instead,"
    " as test_stops.py shows",
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A zombie still answers, so ask ps's view where it can be had.
        state = subprocess.run(  # noqa: S603
            ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        # A ps slowed past its bound, as under heavy load, has not shown the process
        # gone, so it counts as alive and the caller asks again within its deadline.
        return True
    return not state.startswith("Z")


GONE_SECONDS = 4 * (launcher.LAUNCHER_EXIT_SECONDS + launcher.KILLED_EXIT_SECONDS)
"""How long a process the launcher ended may take to be gone, read from the launcher's
own bounds. **Round 9, item 7.** The daemon waits ``LAUNCHER_EXIT_SECONDS`` for a
launcher and ``KILLED_EXIT_SECONDS`` more once it kills one, so a process the stop
ended is gone well within their sum, and the factor is the margin for a machine so
loaded that one run at a load average of 189 failed a wait of a fixed number of polls.
The check is unchanged, since the process must still be gone, and only how long the
test waits to see it is derived."""


def gone(pid: int, seconds: float = GONE_SECONDS) -> bool:
    """Whether ``pid`` is gone, polled until a monotonic deadline ``seconds`` away,
    whatever each poll costs."""
    deadline = time.monotonic() + seconds
    while True:
        if not alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


RECORD_SECONDS = 30
"""How long a test harness may take to write its first record, well under any sleep a
test harness's processes run."""


class Launched:
    """A launcher started as the daemon starts one, with its harness's first record."""

    def __init__(
        self,
        tmp_path: Path,
        body: str,
        policy: StopPolicy,
        *,
        parent: int | None = None,
        report: Path | None = None,
        stdin: int = subprocess.DEVNULL,
        path: str = "/bin:/usr/bin",
        extra_environment: dict[str, str] | None = None,
        filter_environment: bool = False,
    ) -> None:
        script = tmp_path / "harness"
        script.write_text(f"#!{sys.executable}\n{body}")
        script.chmod(0o755)
        self.script = script
        environment = {
            "PATH": path,
            TARGET_VARIABLE: str(script),
            PARENT_VARIABLE: str(os.getpid() if parent is None else parent),
            STOP_POLICY_VARIABLE: policy.value,
            MARK_VARIABLE: MARK,
        }
        if report is not None:
            environment[REPORT_VARIABLE] = str(report)
        if filter_environment:
            environment[launcher.FILTER_ENVIRONMENT_VARIABLE] = "1"
        environment.update(extra_environment or {})
        self.process = subprocess.Popen(  # noqa: S603
            launcher_command(),
            env=environment,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        # A harness that never reports fails here, rather than hanging the run.
        ready, _, _ = select.select([self.process.stdout], [], [], RECORD_SECONDS)
        assert ready, f"the harness wrote no record within {RECORD_SECONDS} seconds"
        self.record: dict[str, object] = json.loads(self.process.stdout.readline())

    def pid(self, name: str) -> int:
        value = self.record[name]
        assert isinstance(value, int)
        return value

    def wait(self) -> int:
        return self.process.wait(timeout=60)

    def clean(self) -> None:
        for name in ("stay", "away"):
            pid = self.pid(name)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=60)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stdin is not None:
            self.process.stdin.close()


UNVERIFIED = 1
"""The status a launcher exits with when one of its own bounds passed, read from
``launcher.main`` and ``launcher.reap``. It is returned only when a walk of the
process table did not finish within ``WALK_DEADLINE_SECONDS``, when what it killed
was not confirmed gone within ``KILL_CONFIRM_SECONDS``, or when the harness was not
reaped within ``REAP_SECONDS``."""

ATTEMPTS = 3
"""How many times a stop test runs its scenario when the launcher reports that one
of its own bounds passed and a measured delay explains it."""

PROBE_SECONDS = 0.01
"""The nominal sleep of the delay probe. An idle machine wakes it within a few
milliseconds of this."""

MAX_PROBES = 100_000
"""The most sleeps one probe makes, so its loop is bounded, about 1,000 seconds at
the nominal sleep, far past any attempt."""

UNVERIFIED_SLACK = min(
    launcher.WALK_DEADLINE_SECONDS - launcher.WALK_SECONDS,
    launcher.KILL_CONFIRM_SECONDS,
    launcher.REAP_SECONDS,
)
"""The least delay that could make the launcher exit ``UNVERIFIED``, its smallest
margin, derived from its constants. The status does not say which bound passed, so
the smallest is the delay a probe must have seen to explain it."""


@dataclass(frozen=True, slots=True)
class Inconclusive:
    """An attempt whose outcome only a delay explains, with the delay that would."""

    bound: str
    """What the launcher or the test reported passing."""
    explained_by: float
    """The seconds of delay that would explain it."""


class DelayProbe:
    """Measures the scheduling delay this process sees while an attempt runs, by a
    thread that sleeps ``PROBE_SECONDS`` again and again and records how far each
    sleep overran. **Audit of round 14, L1.** The one-minute load average it
    replaces lagged the attempt and excused a real regression on any machine whose
    load passed its processor count. This measures the attempt's own time.

    **Inference.** A loaded machine delays the launcher and this thread alike, since
    both wait on the same scheduler, so a delay this thread did not see did not
    delay the launcher either, up to the probe's granularity. **Residual.** A delay
    confined to the launcher's process, such as a page-in, is not seen, and fails
    the test, which is the failure mode chosen, since it fails safe."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock, self._sleep = clock, sleep
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.most = 0.0
        """The longest single overrun seen, in seconds."""
        self.total = 0.0
        """The sum of every overrun seen, in seconds."""

    def _run(self) -> None:
        for _ in range(MAX_PROBES):
            if self._stop.is_set():
                return
            began = self._clock()
            self._sleep(PROBE_SECONDS)
            over = max(0.0, self._clock() - began - PROBE_SECONDS)
            self.most = max(self.most, over)
            self.total += over

    def __enter__(self) -> "DelayProbe":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=RECORD_SECONDS)

    def explains(self, needed: float) -> bool:
        """Whether the delay seen could account for ``needed`` seconds: one stall of
        at least half of it, or stalls that add up to all of it."""
        return self.most >= needed / 2 or self.total >= needed

    def described(self) -> str:
        return (
            f"a longest stall of {self.most * 1000:.0f} ms and"
            f" {self.total * 1000:.0f} ms in all"
        )


def until_conclusive(
    attempt: Callable[[int], Inconclusive | None],
    what: str,
    probe: Callable[[], DelayProbe] = DelayProbe,
) -> None:
    """Run ``attempt`` until one run is conclusive, at most ``ATTEMPTS`` times.

    **Round 14, item 9.** The launcher's stop is bounded by its own constants, which
    the Agent SDK's window fixes, so a test cannot wait longer for it without changing
    what it checks. ``attempt`` asserts every outcome the bounds do not explain, and
    returns ``None`` when its run was conclusive, or what passed with the delay that
    would explain it. **Audit of round 14, L1.** Each attempt runs under a
    ``DelayProbe``, and its outcome counts as inconclusive only when the delay the
    probe measured during that attempt explains it, so the same outcome with no such
    delay fails the test, however high the load average. When no run is conclusive,
    the test is skipped with each bound and measured delay named, so the skip is
    counted and seen, as a test that needs ``ps`` is."""
    passed: list[str] = []
    for number in range(ATTEMPTS):
        with probe() as seen:
            outcome = attempt(number)
        if outcome is None:
            return
        assert seen.explains(outcome.explained_by), (
            f"{what}: {outcome.bound}, with {seen.described()}, too little to explain"
            f" the {outcome.explained_by * 1000:.0f} ms it needs"
        )
        passed.append(f"{outcome.bound}, with {seen.described()}")
    pytest.skip(
        f"{what} was inconclusive in {ATTEMPTS} runs, since the launcher's own"
        f" bounds passed under a measured delay: {'; '.join(passed)}"
    )


class Measured(DelayProbe):
    """A probe that reports a fixed delay, for the tests of ``until_conclusive``."""

    def __init__(self, most: float, total: float) -> None:
        super().__init__()
        self._fixed = (most, total)

    def __enter__(self) -> "DelayProbe":
        self.most, self.total = self._fixed
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def measured(most: float, total: float) -> Callable[[], DelayProbe]:
    return lambda: Measured(most, total)


STALLED = measured(UNVERIFIED_SLACK, UNVERIFIED_SLACK)
IDLE = measured(0.002, 0.01)
PASSED = Inconclusive("a bound passed", UNVERIFIED_SLACK)


def test_the_probe_measures_overruns_of_its_sleep() -> None:
    """A fake clock that loses 300 ms on the second sleep, and stops the probe on
    the fourth, so the loop runs without real time."""
    now = [0.0]
    sleeps: list[float] = []
    stalls = [0.0, 0.3, 0.001, 0.0]
    probe = DelayProbe(clock=lambda: now[0])

    def sleep(seconds: float) -> None:
        now[0] += seconds + stalls[len(sleeps)]
        sleeps.append(seconds)
        if len(sleeps) == len(stalls):
            probe._stop.set()  # pyright: ignore[reportPrivateUsage]

    probe._sleep = sleep  # pyright: ignore[reportPrivateUsage]
    probe._run()  # pyright: ignore[reportPrivateUsage]
    assert sleeps == [PROBE_SECONDS] * len(stalls)
    assert probe.most == pytest.approx(0.3)
    assert probe.total == pytest.approx(0.301)
    assert probe.explains(0.6) and probe.explains(0.3)
    assert not probe.explains(0.61)
    assert "a longest stall of 300 ms" in probe.described()


def test_a_real_probe_starts_and_stops() -> None:
    with DelayProbe() as probe:
        time.sleep(PROBE_SECONDS * 3)
    assert not probe._thread.is_alive()  # pyright: ignore[reportPrivateUsage]
    assert probe.most >= 0.0 and probe.total >= probe.most


def test_the_slack_is_the_launchers_smallest_margin() -> None:
    assert 0 < UNVERIFIED_SLACK <= launcher.KILL_CONFIRM_SECONDS
    assert UNVERIFIED_SLACK <= launcher.REAP_SECONDS


def test_a_conclusive_run_ends_the_attempts() -> None:
    runs: list[int] = []

    def attempt(number: int) -> Inconclusive | None:
        runs.append(number)
        return PASSED if number == 0 else None

    until_conclusive(attempt, "a stop", STALLED)
    assert runs == [0, 1]


def test_a_passed_bound_without_a_measured_delay_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit of round 14, L1. A high load average no longer excuses it."""
    monkeypatch.setattr(os, "getloadavg", lambda: (500.0, 500.0, 500.0))
    runs: list[int] = []

    def attempt(number: int) -> Inconclusive | None:
        runs.append(number)
        return PASSED

    with pytest.raises(AssertionError, match="a stop: a bound passed, with a longest"):
        until_conclusive(attempt, "a stop", IDLE)
    assert runs == [0]


def test_runs_inconclusive_under_a_measured_delay_skip_naming_each() -> None:
    runs: list[int] = []

    def attempt(number: int) -> Inconclusive | None:
        runs.append(number)
        return Inconclusive(f"bound {number}", UNVERIFIED_SLACK)

    with pytest.raises(pytest.skip.Exception) as skipped:
        until_conclusive(attempt, "a stop", STALLED)
    assert runs == list(range(ATTEMPTS))
    for number in range(ATTEMPTS):
        assert f"bound {number}, with a longest stall" in str(skipped.value)


def test_a_failed_assertion_in_a_run_is_never_retried() -> None:
    runs: list[int] = []

    def attempt(number: int) -> Inconclusive | None:
        runs.append(number)
        raise AssertionError("the child survived")

    with pytest.raises(AssertionError, match="the child survived"):
        until_conclusive(attempt, "a stop", STALLED)
    assert runs == [0]


@pytest.fixture
def started() -> Iterator[list[Launched]]:
    launches: list[Launched] = []
    yield launches
    for launched in launches:
        launched.clean()


# The daemon's side.


def test_the_policy_is_kill_all_unless_leave_background_is_named_exactly() -> None:
    assert policy_of({}) is StopPolicy.KILL_ALL
    assert policy_of({STOP_POLICY_VARIABLE: "kill_all"}) is StopPolicy.KILL_ALL
    leave = {STOP_POLICY_VARIABLE: "leave_background"}
    assert policy_of(leave) is StopPolicy.LEAVE_BACKGROUND
    for near in ("LEAVE_BACKGROUND", " leave_background", "leave", ""):
        assert policy_of({STOP_POLICY_VARIABLE: near}) is StopPolicy.KILL_ALL


def test_a_wrapped_command_starts_the_launcher_with_its_variables() -> None:
    environment = {
        "PATH": "/bin",
        STOP_POLICY_VARIABLE: "leave_background",
        TARGET_VARIABLE: "/forged",
        LAUNCH_MARK_VARIABLE: MARK,
    }
    arguments, launched, policy = wrap(["/opt/harness", "--mode", "rpc"], environment)
    assert arguments == [sys.executable, "-I", "-m", "dromon.launcher", "--mode", "rpc"]
    assert policy is StopPolicy.LEAVE_BACKGROUND
    assert launched[TARGET_VARIABLE] == "/opt/harness"
    assert launched[PARENT_VARIABLE] == str(os.getpid())
    assert launched[STOP_POLICY_VARIABLE] == "leave_background"
    assert launched[LAUNCH_MARK_VARIABLE] == MARK
    assert launched["PATH"] == "/bin"
    with pytest.raises(ValueError, match="needs a command"):
        wrap([], environment)


def test_the_report_directory_reaches_a_launch_once_configured(tmp_path: Path) -> None:
    try:
        launcher.configure(tmp_path)
        _, launched, _ = wrap(["/opt/harness"], {})
        assert launched[REPORT_VARIABLE] == str(tmp_path)
    finally:
        launcher.configure(None)
    _, launched, _ = wrap(["/opt/harness"], {})
    assert REPORT_VARIABLE not in launched


def test_the_launcher_command_is_installed_beside_the_interpreter() -> None:
    assert Path(launcher.script_path()).name == launcher.SCRIPT_NAME


def test_a_mark_names_its_line() -> None:
    assert line_of(MARK) == "alpha"
    assert line_of("a.b.0123") == "a.b"
    for bad in (None, 7, "", "nodot", ".0123"):
        assert line_of(bad) is None


def report_text(**changes: object) -> bytes:
    document: dict[str, object] = {
        "v": 1,
        "mark": MARK,
        "cause": "stopped",
        "processes": [{"pid": 4242, "command": "sleep 600"}],
        "more": 0,
    }
    document.update(changes)
    return json.dumps(document).encode()


def test_a_report_is_read_and_every_field_checked() -> None:
    assert parse_report(report_text()) == LeftReport(
        "alpha", "stopped", ((4242, "sleep 600"),), 0
    )
    long = parse_report(report_text(processes=[{"pid": 9, "command": "x" * 500}]))
    assert long is not None and len(long.processes[0][1]) == 200
    for bad in (
        report_text(mark=None),
        report_text(mark="nodot"),
        report_text(cause="vanished"),
        report_text(processes="none"),
        report_text(processes=[{"pid": 1, "command": "init"}]),
        report_text(processes=[{"pid": True, "command": "x"}]),
        report_text(processes=[{"pid": 9}]),
        report_text(processes=[7]),
        report_text(more=-1),
        b"[]",
        b"not json",
        b" " * (launcher.MAX_REPORT_BYTES + 1),
    ):
        assert parse_report(bad) is None


def test_reports_are_collected_once_and_malformed_ones_dropped(tmp_path: Path) -> None:
    (tmp_path / "1-1.json").write_bytes(report_text())
    (tmp_path / "2-1.json").write_bytes(b"not json")
    (tmp_path / "3-1.part").write_bytes(report_text())
    found = collect_reports(tmp_path)
    assert [report.line_id for report in found] == ["alpha"]
    # Review N9. A report read is kept aside, as the only copy of its commands, and
    # is not read again.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["1-1.read", "3-1.part"]
    assert collect_reports(tmp_path) == []
    assert collect_reports(tmp_path / "missing") == []
    assert collect_reports(None) == []


def test_the_walk_follows_parents_and_groups_and_ends() -> None:
    rows = parse_tree(
        "  10     1    10 launcher\n"
        "  11    10    11 harness\n"
        "  12    11    11 tool\n"
        "  13    12    13 detached\n"
        "  14     1    11 orphan in the harness group\n"
        "  15     1    15 stranger\n"
        "  16    16    16 its own parent\n"
        "bad row\n"
    )
    assert [row.pid for row in descendants(rows, 10)] == [11, 12, 13, 14]
    assert [row.pid for row in descendants(rows, 15)] == []
    looped = [TreeRow(20, 21, 20, "a"), TreeRow(21, 20, 20, "b")]
    assert [row.pid for row in descendants(looped, 20)] == [21]


def test_a_timed_out_ps_walk_retries_within_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    calls: list[float] = []

    def fake_ps(uid: int, timeout: float) -> list[TreeRow] | TreeReadFailure:
        del uid
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] += timeout
            return TreeReadFailure(
                f"ps did not answer within {timeout:g} seconds", timed_out=True
            )
        return []

    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher, "tree_or_reason", fake_ps)
    found = launcher._harness_tree(10, 11)  # pyright: ignore[reportPrivateUsage]
    assert found == []
    assert calls == [
        launcher.WALK_SECONDS,
        launcher.WALK_DEADLINE_SECONDS - launcher.WALK_SECONDS,
    ]


# The launcher itself, with a scripted harness.


def test_the_harness_sees_no_launch_variable_but_its_mark(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, HARNESS, StopPolicy.KILL_ALL)
    started.append(launched)
    assert launched.record["names"] == [MARK_VARIABLE]
    assert not set(LAUNCH_VARIABLES) & set(launched.record["names"])  # type: ignore[arg-type]


def test_the_launcher_drops_daemon_variables_before_starting_its_command(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(
        tmp_path,
        HARNESS,
        StopPolicy.KILL_ALL,
        extra_environment={
            "DAEMON_ONLY_SENTINEL": "must-not-reach-child",
            "USER": "dromon-user",
            "LOGNAME": "dromon-login",
            "CLAUDE_CONFIG_DIR": "/operator/claude-config",
            "ENABLE_TOOL_SEARCH": "true",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.174",
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
        },
        filter_environment=True,
    )
    started.append(launched)
    assert launched.record["unapproved"] is None
    assert launched.record["engine_environment"] == {
        "USER": "dromon-user",
        "LOGNAME": "dromon-login",
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.174",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
        "CLAUDE_CONFIG_DIR": None,
    }


GIT_HARNESS = HARNESS.replace(
    "'CLAUDE_CONFIG_DIR')", "'CLAUDE_CONFIG_DIR', 'GIT_SSH_COMMAND')"
)
"""The harness, reporting also the command ``dromon.gitssh`` gives a line."""


@pytest.mark.parametrize("line_value", ["ssh -F /dev/null", ""])
def test_the_filter_passes_the_lines_git_command_and_never_the_daemons(
    tmp_path: Path, started: list[Launched], line_value: str
) -> None:
    launched = Launched(
        tmp_path,
        GIT_HARNESS,
        StopPolicy.KILL_ALL,
        extra_environment={
            "GIT_SSH_COMMAND": "ssh -i /operator/key",
            launcher.relayed("GIT_SSH_COMMAND"): line_value,
        },
        filter_environment=True,
    )
    started.append(launched)
    seen = cast(dict[str, object], launched.record["engine_environment"])
    assert seen["GIT_SSH_COMMAND"] == (line_value or None)
    assert launched.record["names"] == [MARK_VARIABLE]


@NEEDS_PS
def test_a_stop_under_kill_all_ends_the_harness_and_its_group(
    tmp_path: Path, started: list[Launched]
) -> None:
    def attempt(number: int) -> Inconclusive | None:
        del number
        launched = Launched(tmp_path, HARNESS + "time.sleep(60)\n", StopPolicy.KILL_ALL)
        started.append(launched)
        stay = launched.pid("stay")
        assert alive(stay)
        launched.process.send_signal(signal.SIGTERM)
        status = launched.wait()
        # The group is signalled whether or not the walk finished, so the child in
        # it is gone either way.
        assert gone(stay)
        if status == UNVERIFIED:
            return Inconclusive(
                "the launcher could not confirm its stop within its bounds",
                UNVERIFIED_SLACK,
            )
        assert status == 128 + signal.SIGTERM
        return None

    until_conclusive(attempt, "the stop under kill_all")


@NEEDS_PS
def test_a_stop_under_kill_all_also_ends_a_child_that_left_the_group(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, HARNESS + "time.sleep(60)\n", StopPolicy.KILL_ALL)
    started.append(launched)
    away = launched.pid("away")
    launched.process.send_signal(signal.SIGTERM)
    launched.wait()
    assert gone(away)


STAND_IN = (
    "import os, subprocess, sys\n"
    "environment = dict(os.environ)\n"
    f"environment[{PARENT_VARIABLE!r}] = str(os.getpid())\n"
    f"launched = subprocess.Popen([sys.executable, '-I', '-c', {LAUNCHER_RUNNER!r}],"
    " env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,"
    " start_new_session=True)\n"
    "sys.stdout.write(launched.stdout.readline().decode())\n"
    "sys.stdout.flush()\n"
    "os._exit(0)\n"
)
"""A stand-in for the daemon, which starts a launcher, passes on its harness's first
record, and dies at once without stopping anything, as a crashed daemon does."""


def test_a_dead_daemon_is_noticed_and_everything_ended(tmp_path: Path) -> None:
    script = tmp_path / "harness"
    script.write_text(f"#!{sys.executable}\n{HARNESS}time.sleep(60)\n")
    script.chmod(0o755)
    daemon = subprocess.run(  # noqa: S603
        [sys.executable, "-c", STAND_IN],
        env={
            "PATH": "/bin:/usr/bin",
            TARGET_VARIABLE: str(script),
            STOP_POLICY_VARIABLE: StopPolicy.KILL_ALL.value,
        },
        capture_output=True,
        timeout=60,
        check=True,
    )
    record = json.loads(daemon.stdout)
    stay = int(record["stay"])
    try:
        # The launcher sees its parent change within POLL_SECONDS.
        assert gone(stay)
    finally:
        for pid in (stay, int(record["away"])):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


@NEEDS_PS
def test_a_harness_that_exits_has_its_group_ended_and_its_status_kept(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, HARNESS + "sys.exit(3)\n", StopPolicy.KILL_ALL)
    started.append(launched)
    assert launched.wait() == 3
    assert gone(launched.pid("stay"))


@NEEDS_PS
def test_a_stop_under_leave_background_ends_the_harness_alone(
    tmp_path: Path, started: list[Launched]
) -> None:
    reports = tmp_path / "left"
    reports.mkdir()
    launched = Launched(
        tmp_path,
        HARNESS + "time.sleep(60)\n",
        StopPolicy.LEAVE_BACKGROUND,
        report=reports,
    )
    started.append(launched)
    launched.process.send_signal(signal.SIGTERM)
    launched.wait()
    stay = launched.pid("stay")
    assert alive(stay)
    found = collect_reports(reports)
    (report,) = found
    assert report.line_id == "alpha" and report.cause == "stopped"
    assert {pid for pid, _ in report.processes} >= {stay, launched.pid("away")}


def test_a_launcher_without_a_target_refuses_to_start() -> None:
    finished = subprocess.run(  # noqa: S603
        launcher_command(),
        env={"PATH": "/bin"},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert finished.returncode == 127
    assert TARGET_VARIABLE.encode() in finished.stderr


def test_a_target_that_cannot_start_is_reported() -> None:
    finished = subprocess.run(  # noqa: S603
        launcher_command(),
        env={"PATH": "/bin", TARGET_VARIABLE: "/no/such/harness"},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert finished.returncode == 127
    assert b"cannot start" in finished.stderr


# Review F1 to F5, and F8.


ORPHANING = (
    "import json, os, subprocess, sys, time\n"
    "middle = subprocess.Popen([sys.executable, '-c',"
    ' \'import subprocess, sys; away = subprocess.Popen(["/bin/sleep", "60"],'
    " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
    " stderr=subprocess.DEVNULL); sys.stdout.write(str(away.pid));"
    " sys.stdout.flush()'], stdout=subprocess.PIPE)\n"
    "away = int(middle.stdout.read())\n"
    "middle.wait()\n"
    "sys.stdout.write(json.dumps({'stay': away, 'away': away}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)
"""A harness whose child starts a grandchild in a session of its own and exits, so the
grandchild has lost its parent and left the harness's group before any stop. The
grandchild must not inherit the child's output, which the harness reads to its end, or
the harness waits out the grandchild's sleep before it reports, and the grandchild is
by then a zombie of the launcher that adopted it. That was the Linux failure of
2026-10-27, on the first run of this test anywhere."""


@NEEDS_PS
@NEEDS_SUBREAPER
def test_a_stop_under_kill_all_ends_an_orphan_in_a_session_of_its_own(
    tmp_path: Path, started: list[Launched]
) -> None:
    """F1. On Linux the launcher is a subreaper, so the orphan is its own child and
    the walk reaches it."""
    launched = Launched(tmp_path, ORPHANING, StopPolicy.KILL_ALL)
    started.append(launched)
    away = launched.pid("away")
    assert alive(away)
    launched.process.send_signal(signal.SIGTERM)
    launched.wait()
    assert gone(away)


def test_the_walk_gap_is_named_where_no_subreaper_exists() -> None:
    """F1. The gap is a fact of the platform, which the daemon records once."""
    assert walk_gap("linux") is None
    for platform in ("darwin", "freebsd14"):
        assert walk_gap(platform) == launcher.WALK_GAP
    assert walk_gap() == (None if sys.platform == "linux" else launcher.WALK_GAP)


def test_a_walk_that_cannot_read_the_table_is_never_an_empty_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2. A failed ps is a reason, and the launcher's walk gives None for it."""

    def denied(uid: int, timeout: float = 0.0) -> list[TreeRow] | TreeReadFailure:
        return TreeReadFailure("ps cannot be run: denied")

    def empty(uid: int, timeout: float = 0.0) -> list[TreeRow] | TreeReadFailure:
        return []

    monkeypatch.setattr(launcher, "tree_or_reason", denied)
    assert walk_unavailable() == "ps cannot be run: denied"
    assert launcher._harness_tree(1, 2) is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(launcher, "tree_or_reason", empty)
    assert walk_unavailable() is None
    assert launcher._harness_tree(1, 2) == []  # pyright: ignore[reportPrivateUsage]


def test_a_process_table_error_is_not_retried_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def failed(uid: int, timeout: float) -> TreeReadFailure:
        nonlocal calls
        calls += 1
        return TreeReadFailure(
            "ps failed with status 1 and said ps did not answer within policy",
            timed_out=False,
        )

    monkeypatch.setattr(launcher, "tree_or_reason", failed)
    assert launcher._harness_tree(1, 2) is None  # pyright: ignore[reportPrivateUsage]
    assert calls == 1


def test_a_process_still_alive_after_sigkill_makes_the_stop_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = TreeRow(30, 10, 10, "blocked child")
    waits: list[tuple[set[int], float]] = []
    signals: list[tuple[int, int]] = []

    def wait_gone(
        _exits: ExitWatch,
        pids: set[int],
        seconds: float = launcher.EXIT_SECONDS,
    ) -> set[int]:
        waits.append((set(pids), seconds))
        return set(pids)

    def tree(_me: int, _harness: int) -> list[TreeRow]:
        return [row]

    def record_signal(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    def alive_always(_pid: int) -> bool:
        return True

    monkeypatch.setattr(launcher, "_harness_tree", tree)
    monkeypatch.setattr(launcher, "_wait_gone", wait_gone)
    monkeypatch.setattr(launcher, "signal_group", record_signal)
    monkeypatch.setattr(launcher, "_send", record_signal)
    monkeypatch.setattr(launcher, "_alive", alive_always)

    stopped = launcher.end_tree(1, 10, cast(ExitWatch, object()))

    assert not stopped
    assert waits == [
        ({30}, launcher.EXIT_SECONDS),
        ({30}, launcher.KILL_CONFIRM_SECONDS),
    ]
    assert (30, signal.SIGKILL) in signals


def test_ps_failing_is_a_reason_and_not_a_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: object, **__: object) -> object:
        raise FileNotFoundError("no ps")

    monkeypatch.setattr(processes.subprocess, "run", missing)
    assert processes.read_tree(os.getuid()) is None
    reason = processes.tree_or_reason(os.getuid())
    assert isinstance(reason, TreeReadFailure) and "no ps" in reason.reason

    def refused(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ps"], 1, "", "ps: not permitted\n")

    monkeypatch.setattr(processes.subprocess, "run", refused)
    reason = processes.tree_or_reason(os.getuid())
    assert isinstance(reason, TreeReadFailure)
    assert reason.reason == "ps failed with status 1 and said ps: not permitted"
    assert not reason.timed_out


def test_a_launcher_is_signalled_by_pid_as_well_as_by_group() -> None:
    """F3. A launcher that has not yet made its own group is still reached."""
    sleeper = subprocess.Popen(["/bin/sleep", "60"])  # noqa: S603
    try:
        # The sleeper is in the test's own group, so its pid names no group.
        launcher.signal_launch(sleeper.pid, signal.SIGTERM, StopPolicy.KILL_ALL)
        assert sleeper.wait(timeout=10) == -signal.SIGTERM
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)


class Unending:
    """A launched process, as the daemon holds one, that never exits."""

    pid = 2**22 + 12345
    returncode: int | None = None
    stdin = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


def test_ending_a_launcher_that_survives_its_kill_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3. Every wait in the daemon's end is bounded, the last one too."""
    sent: list[tuple[int, int]] = []

    def record(pid: int, number: int, policy: StopPolicy) -> None:
        sent.append((pid, number))

    monkeypatch.setattr(launcher, "signal_launch", record)
    monkeypatch.setattr(launcher, "LAUNCHER_EXIT_SECONDS", 0.05)
    monkeypatch.setattr(launcher, "KILLED_EXIT_SECONDS", 0.05)
    process = Unending()

    async def ending() -> None:
        await asyncio.wait_for(
            launcher.end(
                process,  # type: ignore[arg-type]
                StopPolicy.LEAVE_BACKGROUND,
                close_input=False,
            ),
            timeout=5,
        )

    asyncio.run(ending())
    assert sent == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


def test_an_exit_is_seen_without_reaping_the_harness() -> None:
    """F4. The harness stays unreaped, so its pid and group cannot be reused, until
    the launcher reaps it last."""
    child = subprocess.Popen(["/bin/sh", "-c", "exit 3"])  # noqa: S603
    watched = ExitWatch(child.pid)
    try:
        for _ in range(200):
            if watched.exited():
                break
            time.sleep(0.05)
        assert watched.exited()
        # Still a zombie of this process, so signalling it is allowed and harmless.
        os.kill(child.pid, 0)
        assert child.returncode is None
        assert child.wait(timeout=10) == 3
    finally:
        watched.close()


def test_an_exit_watch_on_a_reaped_child_sees_it_exited() -> None:
    child = subprocess.Popen(["/bin/sh", "-c", "exit 0"])  # noqa: S603
    child.wait(timeout=10)
    watched = ExitWatch(child.pid)
    try:
        assert watched.exited()
    finally:
        watched.close()


def test_the_stop_fits_within_the_sdk_window_with_a_margin() -> None:
    """F5. The SDK kills the launcher SDK_CLOSE_SECONDS after its SIGTERM."""
    assert launcher.STOP_BUDGET_SECONDS + 0.25 <= launcher.SDK_CLOSE_SECONDS
    assert launcher.LAUNCHER_EXIT_SECONDS > launcher.STOP_BUDGET_SECONDS


STUBBORN = (
    "import json, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "stay = subprocess.Popen([sys.executable, '-c', 'import signal, time;"
    " signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
    "sys.stdout.write(json.dumps({'stay': stay.pid, 'away': stay.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)
"""A harness that ignores SIGTERM, with a child that does too."""


@NEEDS_PS
def test_a_harness_that_ignores_sigterm_is_killed_within_the_sdk_window(
    tmp_path: Path, started: list[Launched]
) -> None:
    """F5. The launcher's own stop, under the SDK's timing. The time is the meaning
    of the test, so a stop past the window counts as inconclusive only on a loaded
    machine, where the launcher's waits, each a count of short sleeps, take longer."""

    def attempt(number: int) -> Inconclusive | None:
        del number
        launched = Launched(tmp_path, STUBBORN, StopPolicy.KILL_ALL)
        started.append(launched)
        stay = launched.pid("stay")
        began = time.monotonic()
        launched.process.send_signal(signal.SIGTERM)
        status = launched.wait()
        took = time.monotonic() - began
        assert gone(stay)
        if status == UNVERIFIED:
            return Inconclusive(
                "the launcher could not confirm its stop within its bounds",
                UNVERIFIED_SLACK,
            )
        assert status == 128 + signal.SIGKILL
        if took >= launcher.SDK_CLOSE_SECONDS:
            # The launcher plans its stop within STOP_BUDGET_SECONDS, so only a
            # delay of the time beyond that explains it.
            return Inconclusive(
                f"the stop took {took:.1f} seconds, past the Agent SDK's"
                f" {launcher.SDK_CLOSE_SECONDS:g}",
                took - launcher.STOP_BUDGET_SECONDS,
            )
        return None

    until_conclusive(attempt, "the stop of a harness that ignores SIGTERM")


VERSION_HARNESS = "import sys\nsys.stdout.write('1.0.0 (Claude Code)\\n')\n"


def test_the_sdk_version_check_reads_the_version_through_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F8. The SDK's own check, run on the launcher, reads the harness's version. A
    version below the SDK's minimum shows that it was read."""
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{VERSION_HARNESS}")
    script.chmod(0o755)
    monkeypatch.setenv(TARGET_VARIABLE, str(script))
    monkeypatch.setenv(PARENT_VARIABLE, str(os.getpid()))
    transport = SubprocessCLITransport(
        prompt="", options=ClaudeAgentOptions(cli_path=launcher.script_path())
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(transport._check_claude_version())  # pyright: ignore[reportPrivateUsage]
    assert any("1.0.0" in record.getMessage() for record in caplog.records)


def test_a_version_query_without_a_target_names_the_sdk_s_claude_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8. The SDK runs the check with its own environment, so the launcher has no
    target, and answers from the same lookup the engine starts Claude Code from."""
    monkeypatch.setattr(launcher, "claude_command", lambda: "/opt/claude")
    assert version_target(["-v"]) == "/opt/claude"
    assert version_target(["--version"]) == "/opt/claude"
    for other in ([], ["-v", "-p"], ["--print"], ["-V"]):
        assert version_target(other) is None

    def missing() -> str:
        raise FileNotFoundError("no Claude Code")

    monkeypatch.setattr(launcher, "claude_command", missing)
    assert version_target(["-v"]) is None


def test_a_version_query_receives_only_the_base_environment() -> None:
    assert version_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/line",
            "ANTHROPIC_API_KEY": "must-not-reach-cli",
            "DAEMON_ONLY_SENTINEL": "must-not-reach-cli",
        }
    ) == {
        "PATH": "/bin",
        "HOME": "/home/line",
        "ENABLE_TOOL_SEARCH": "true",
    }


# Item 7 of the live check of 2026-10-26, a harness that ends its own work.


CLOSING = (
    "import json, os, signal, subprocess, sys\n"
    "away = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\n"
    "sys.stdout.write(json.dumps({'stay': away.pid, 'away': away.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('echo ' + line)\n"
    "    sys.stdout.flush()\n"
    "os.kill(away.pid, signal.SIGKILL)\n"
    "away.wait()\n"
)
"""A harness that, as Claude Code was observed to, ends its background command, in a
session of its own, when its input closes, and echoes what it reads until then."""


@NEEDS_PS
def test_under_leave_background_a_closed_input_does_not_reach_the_harness(
    tmp_path: Path, started: list[Launched]
) -> None:
    def attempt(number: int) -> Inconclusive | None:
        reports = tmp_path / f"left-{number}"
        reports.mkdir()
        launched = Launched(
            tmp_path,
            CLOSING,
            StopPolicy.LEAVE_BACKGROUND,
            report=reports,
            stdin=subprocess.PIPE,
        )
        started.append(launched)
        stdin, stdout = launched.process.stdin, launched.process.stdout
        assert stdin is not None and stdout is not None
        # The relay carries the input across.
        stdin.write(b"ping\n")
        stdin.flush()
        assert stdout.readline() == b"echo ping\n"
        # The daemon's close, as the Agent SDK's first step, stops the harness
        # instead.
        stdin.close()
        status = launched.wait()
        away = launched.pid("away")
        # The harness never saw its input close, whatever the walk found.
        assert alive(away)
        (report,) = collect_reports(reports)
        assert report.cause == "stopped"
        if status == UNVERIFIED or not report.listed:
            return Inconclusive(
                "the launcher could not list or reap within its bounds",
                UNVERIFIED_SLACK,
            )
        assert status == 128 + signal.SIGKILL
        assert away in {pid for pid, _ in report.processes}
        return None

    until_conclusive(attempt, "the stop under leave_background")


@NEEDS_PS
def test_under_kill_all_the_harness_still_sees_its_input_close(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, CLOSING, StopPolicy.KILL_ALL, stdin=subprocess.PIPE)
    started.append(launched)
    assert launched.process.stdin is not None
    launched.process.stdin.close()
    # The harness exited by itself on the end of its input, with its own status.
    assert launched.wait() == 0
    assert gone(launched.pid("away"))


TERMINATING = (
    "import json, os, signal, subprocess, sys, time\n"
    "away = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\n"
    "def end(number, frame):\n"
    "    os.kill(away.pid, signal.SIGKILL)\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, end)\n"
    "sys.stdout.write(json.dumps({'stay': away.pid, 'away': away.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)
"""A harness that ends its background command when asked to stop, which a harness
may do on ``SIGTERM``."""


def test_under_leave_background_the_harness_is_not_asked_to_stop(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, TERMINATING, StopPolicy.LEAVE_BACKGROUND)
    started.append(launched)
    launched.process.send_signal(signal.SIGTERM)
    assert launched.wait() == 128 + signal.SIGKILL
    assert alive(launched.pid("away"))


def test_only_a_pipe_or_a_socket_is_relayed(tmp_path: Path) -> None:
    """A harness started on the null device, or a file, has no input to close."""
    reading, writing = os.pipe()
    left, right = socket.socketpair()
    null = os.open(os.devnull, os.O_RDONLY)
    plain = tmp_path / "plain"
    plain.write_bytes(b"")
    file = os.open(plain, os.O_RDONLY)
    try:
        assert relayable(reading) and relayable(writing)
        assert relayable(left.fileno())
        assert not relayable(null)
        assert not relayable(file)
    finally:
        for descriptor in (reading, writing, null, file):
            os.close(descriptor)
        left.close()
        right.close()
    assert not relayable(null)


# Re-review of the stop policy, F2 and N1 to N3.


def test_a_failed_walk_under_kill_all_is_reported_to_the_daemon(
    tmp_path: Path, started: list[Launched]
) -> None:
    """F2. With no ps on its path the launcher cannot read the table. It still ends
    the harness's group, and reports to the daemon, not only on its standard error,
    that it could not look further."""
    reports = tmp_path / "left"
    reports.mkdir()
    empty = tmp_path / "no-ps"
    empty.mkdir()
    launched = Launched(
        tmp_path,
        HARNESS + "time.sleep(60)\n",
        StopPolicy.KILL_ALL,
        report=reports,
        path=str(empty),
    )
    started.append(launched)
    launched.process.send_signal(signal.SIGTERM)
    assert launched.wait() == 1
    assert gone(launched.pid("stay"))
    found = collect_reports(reports)
    assert [(r.line_id, r.cause, r.processes, r.listed) for r in found] == [
        ("alpha", "stopped", (), False)
    ]


def test_a_timed_out_ps_at_stop_is_reported_and_exits_unsuccessfully(
    tmp_path: Path, started: list[Launched]
) -> None:
    reports = tmp_path / "left-timeout"
    reports.mkdir()
    fake_bin = tmp_path / "slow-ps"
    fake_bin.mkdir()
    fake_ps = fake_bin / "ps"
    fake_ps.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    fake_ps.chmod(0o755)
    launched = Launched(
        tmp_path,
        HARNESS + "time.sleep(60)\n",
        StopPolicy.KILL_ALL,
        report=reports,
        path=str(fake_bin),
    )
    started.append(launched)
    stay = launched.pid("stay")
    launched.process.send_signal(signal.SIGTERM)
    assert launched.wait() == 1
    assert gone(stay)
    found = collect_reports(reports)
    assert [(report.processes, report.listed) for report in found] == [((), False)]


def launch_records(directory: Path, done: bool) -> list[LaunchRecord]:
    """The launch records in ``directory`` once one is marked ``done`` as given,
    waited for within a bound, since the launcher writes it as the harness starts."""
    for _ in range(200):
        found = read_launches(directory)
        if found and all(record.done is done for record in found):
            return found
        time.sleep(0.05)
    return read_launches(directory)


def test_a_launch_record_names_the_harness_and_is_marked_done(
    tmp_path: Path, started: list[Launched]
) -> None:
    """N1. The record the daemon's sweep reads."""
    reports = tmp_path / "left"
    reports.mkdir()
    launched = Launched(
        tmp_path, HARNESS + "time.sleep(60)\n", StopPolicy.KILL_ALL, report=reports
    )
    started.append(launched)
    (record,) = launch_records(reports, done=False)
    assert record.line_id == "alpha" and record.policy is StopPolicy.KILL_ALL
    assert record.launcher == launched.process.pid
    assert record.harness == os.getpgid(launched.pid("stay"))
    assert record.command == str(launched.script)
    assert record.path.stat().st_mode & 0o777 == 0o600
    launched.process.send_signal(signal.SIGTERM)
    launched.wait()
    (finished,) = launch_records(reports, done=True)
    assert finished.done and finished.harness == record.harness


ORPHAN = (
    "import os, subprocess, sys, time\n"
    "member = subprocess.Popen(['/bin/sleep', '60'])\n"
    "sys.stdout.write(f'{os.getpid()} {member.pid}\\n')\n"
    "sys.stdout.close()\n"
    "time.sleep(60)\n"
)
"""A process that leads a group of its own with one member in it, as a harness does,
and reports both pids."""


def orphan_group() -> tuple[int, int]:
    """Start ``ORPHAN`` through a parent that exits at once, so the group is no
    child of the test's, as a harness whose launcher was killed is no child of the
    daemon's, and the system reaps what the sweep ends."""
    starter = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {ORPHAN!r}],"
        " start_new_session=True)\n"
    )
    outer = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", "-c", starter], stdout=subprocess.PIPE
    )
    assert outer.stdout is not None
    line = outer.stdout.readline()
    outer.stdout.close()
    outer.wait(timeout=60)
    leader, member = (int(field) for field in line.split())
    return leader, member


@pytest.fixture
def orphans() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def write_record(
    directory: Path,
    harness: int,
    policy: StopPolicy,
    *,
    done: bool = False,
    launcher_pid: int = 2,
    command: str = sys.executable,
    name: str = "1-1",
) -> Path:
    path = directory / f"{name}{launcher.LAUNCH_SUFFIX}"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "mark": MARK,
                "launcher": launcher_pid,
                "harness": harness,
                "command": command,
                "policy": policy.value,
                "done": done,
            }
        )
    )
    return path


def group_rows(leader: int, member: int) -> list[TreeRow]:
    """The rows ps would give for an orphaned group, its parent the first process."""
    return [
        TreeRow(leader, 1, leader, f"{sys.executable} -I -c {ORPHAN!r}"),
        TreeRow(member, leader, leader, "/bin/sleep 60"),
    ]


def test_a_sweep_under_kill_all_ends_a_group_its_launcher_left_frozen(
    tmp_path: Path, orphans: list[int]
) -> None:
    """N1. The launcher was killed between its SIGSTOP and its SIGCONT."""
    leader, member = orphan_group()
    orphans.extend([leader, member])
    os.killpg(leader, signal.SIGSTOP)
    record = write_record(tmp_path, leader, StopPolicy.KILL_ALL)
    swept = asyncio.run(sweep_launches(tmp_path, lambda: group_rows(leader, member)))
    assert swept == [Swept("alpha", StopPolicy.KILL_ALL, False, (leader, member), ())]
    assert gone(leader) and gone(member)
    assert not record.exists()


def test_a_sweep_under_leave_background_kills_the_harness_and_records_the_rest(
    tmp_path: Path, orphans: list[int]
) -> None:
    """N1. Under leave_background the harness alone is ended, as its launcher would
    have, and the member it left is continued and named for the record."""
    leader, member = orphan_group()
    orphans.extend([leader, member])
    os.kill(leader, signal.SIGSTOP)
    write_record(tmp_path, leader, StopPolicy.LEAVE_BACKGROUND)
    rows = group_rows(leader, member)
    swept = asyncio.run(sweep_launches(tmp_path, lambda: rows))
    assert swept == [
        Swept("alpha", StopPolicy.LEAVE_BACKGROUND, False, (leader,), (rows[1],))
    ]
    assert gone(leader)
    assert alive(member)


def test_a_sweep_of_a_finished_launch_records_nothing_and_ends_nothing(
    tmp_path: Path, orphans: list[int]
) -> None:
    leader, member = orphan_group()
    orphans.extend([leader, member])
    write_record(tmp_path, leader, StopPolicy.LEAVE_BACKGROUND, done=True)
    swept = asyncio.run(sweep_launches(tmp_path, lambda: group_rows(leader, member)))
    assert swept == [Swept("alpha", StopPolicy.LEAVE_BACKGROUND, True, (), ())]
    assert alive(leader) and alive(member)


def test_a_sweep_leaves_a_reused_pid_and_a_running_launcher_alone(
    tmp_path: Path, orphans: list[int]
) -> None:
    """N1. A pid that now runs another command, or that a launcher still running
    claims, is not the swept harness's, so nothing is signalled."""
    leader, member = orphan_group()
    orphans.extend([leader, member])
    os.kill(leader, signal.SIGSTOP)
    reused = write_record(tmp_path, leader, StopPolicy.KILL_ALL, command="/opt/other")
    swept = asyncio.run(sweep_launches(tmp_path, lambda: group_rows(leader, member)))
    assert isinstance(swept, list)
    assert [s.skipped for s in swept] == ["another process now holds the harness's pid"]
    assert alive(leader) and alive(member) and not reused.exists()
    live = write_record(tmp_path, leader, StopPolicy.KILL_ALL, launcher_pid=7, name="2")
    dead = write_record(tmp_path, leader, StopPolicy.KILL_ALL, name="1")
    # A running launcher is its harness's parent.
    rows = [
        TreeRow(leader, 7, leader, group_rows(leader, member)[0].command),
        group_rows(leader, member)[1],
        TreeRow(7, 1, 7, f"{sys.executable} -I -m dromon.launcher"),
    ]
    swept = asyncio.run(sweep_launches(tmp_path, lambda: rows))
    assert isinstance(swept, list)
    assert [s.skipped for s in swept] == ["another launch's harness now holds its pid"]
    assert alive(leader) and live.exists() and not dead.exists()
    os.kill(leader, signal.SIGCONT)


def test_a_sweep_that_cannot_read_the_table_touches_nothing(tmp_path: Path) -> None:
    record = write_record(tmp_path, 4242, StopPolicy.KILL_ALL)
    assert asyncio.run(sweep_launches(tmp_path, lambda: "no ps")) == "no ps"
    assert record.exists()
    assert asyncio.run(sweep_launches(None)) == []


def test_a_launcher_is_running_only_while_it_holds_its_harness() -> None:
    """Third live run of N1. A killed launcher the daemon has not reaped still
    answers a signal, so the table decides. It is gone when ``ps`` shows it
    ``<defunct>``, or when its harness has another parent. A harness that has exited,
    or whose pid another command now holds, says nothing of its launcher."""
    me = f"{sys.executable} /venv/bin/dromon-launch"
    held = [TreeRow(7, 1, 7, me), TreeRow(40, 7, 40, "/opt/claude -p")]
    assert launcher_running(held, 7, 40, "/opt/claude")
    assert launcher_running(held[:1], 7, 40, "/opt/claude")
    assert launcher_running(held, 7)
    orphaned = [held[0], TreeRow(40, 1, 40, "/opt/claude -p")]
    assert not launcher_running(orphaned, 7, 40, "/opt/claude")
    assert launcher_running(orphaned, 7, 40, "/opt/other")
    assert not launcher_running([TreeRow(7, 1, 7, launcher.DEFUNCT)], 7)
    assert not launcher_running([TreeRow(7, 1, 7, "[dromon-launch] <defunct>")], 7)
    assert not launcher_running([TreeRow(7, 1, 7, "/bin/sleep 60")], 7)
    assert not launcher_running(held, 8)


def test_a_sweep_reads_the_table_while_a_record_is_unfinished(tmp_path: Path) -> None:
    """Third live run of N1. A launcher whose pid answers a signal, the test's own
    here, is still checked in the table while its record is unfinished, and a
    finished record whose launcher answers costs no read."""
    reads: list[int] = []

    def read() -> list[TreeRow]:
        reads.append(1)
        return [TreeRow(os.getpid(), 1, os.getpid(), launcher.DEFUNCT)]

    me = os.getpid()
    write_record(tmp_path, 4242, StopPolicy.KILL_ALL, done=True, launcher_pid=me)
    assert asyncio.run(sweep_launches(tmp_path, read)) == []
    assert reads == []
    record = write_record(
        tmp_path, 4242, StopPolicy.KILL_ALL, launcher_pid=me, name=f"{me}-77"
    )
    swept = asyncio.run(sweep_launches(tmp_path, read))
    assert reads == [1]
    assert isinstance(swept, list)
    assert [(s.finished, s.ended, s.launched_ns) for s in swept] == [(False, (), 77)]
    assert not record.exists()
    assert len(read_launches(tmp_path)) == 1


def test_a_launch_record_s_time_is_read_from_its_name(tmp_path: Path) -> None:
    assert launched_at(tmp_path / f"12-345{launcher.LAUNCH_SUFFIX}") == 345
    for name in ("12", "12-", "12-3x", "12-\u0663"):
        assert launched_at(tmp_path / f"{name}{launcher.LAUNCH_SUFFIX}") is None


def test_a_malformed_launch_record_is_removed(tmp_path: Path) -> None:
    good = write_record(tmp_path, 4242, StopPolicy.KILL_ALL)
    for name, text in (
        ("a", "not json"),
        ("b", json.dumps({"launcher": 1, "harness": 5, "command": "x"})),
        ("c", json.dumps({"launcher": 5, "harness": 5, "command": "", "done": False})),
    ):
        (tmp_path / f"{name}{launcher.LAUNCH_SUFFIX}").write_text(text)
    assert [record.path for record in read_launches(tmp_path)] == [good]
    assert sorted(p.name for p in tmp_path.iterdir()) == [good.name]


def test_the_death_signal_follows_the_policy() -> None:
    """N1. On Linux the harness learns at once that its launcher died."""
    assert death_signal(StopPolicy.KILL_ALL) is signal.SIGTERM
    assert death_signal(StopPolicy.LEAVE_BACKGROUND) is signal.SIGKILL
    armed = arm_death_signal(StopPolicy.KILL_ALL)
    assert (armed is None) == (sys.platform != "linux")


@pytest.mark.skipif(sys.platform != "linux", reason="only Linux has PR_SET_PDEATHSIG")
def test_a_harness_ends_with_its_killed_launcher_on_linux(
    tmp_path: Path, started: list[Launched]
) -> None:
    launched = Launched(tmp_path, HARNESS + "time.sleep(60)\n", StopPolicy.KILL_ALL)
    started.append(launched)
    harness = os.getpgid(launched.pid("stay"))
    launched.process.kill()
    launched.wait()
    assert gone(harness)


FLUSHING = (
    "import json, os, sys, time\n"
    "me = os.getpid()\n"
    "sys.stdout.write(json.dumps({'stay': me, 'away': me}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "target = sys.stdin.readline().strip()\n"
    "time.sleep(0.1)\n"
    "open(target, 'w').close()\n"
    "time.sleep(60)\n"
)
"""A harness that, as Claude Code's transcript writer does, writes what it queued a
short time after it queued it."""


def test_under_leave_background_the_harness_may_flush_before_it_is_killed(
    tmp_path: Path, started: list[Launched]
) -> None:
    """N2. The daemon's close lets the harness run on for FLUSH_SECONDS, its input
    still open, before it is frozen and killed."""
    launched = Launched(
        tmp_path, FLUSHING, StopPolicy.LEAVE_BACKGROUND, stdin=subprocess.PIPE
    )
    started.append(launched)
    flushed = tmp_path / "flushed"
    assert launched.process.stdin is not None
    launched.process.stdin.write(f"{flushed}\n".encode())
    launched.process.stdin.close()
    assert launched.wait() == 128 + signal.SIGKILL
    assert flushed.exists()
    assert launcher.FLUSH_SECONDS > 0.1


def test_a_harness_that_does_not_read_its_input_does_not_hide_the_close(
    tmp_path: Path, started: list[Launched]
) -> None:
    """N3. More input than a pipe holds, which the harness never reads, is held by the
    relay, so the daemon's close is still seen and the harness is stopped."""
    launched = Launched(
        tmp_path,
        HARNESS + "time.sleep(60)\n",
        StopPolicy.LEAVE_BACKGROUND,
        stdin=subprocess.PIPE,
    )
    started.append(launched)
    assert launched.process.stdin is not None
    began = time.monotonic()
    launched.process.stdin.write(b"x" * (4 * 64 * 1024))
    launched.process.stdin.close()
    assert launched.wait() == 128 + signal.SIGKILL
    assert time.monotonic() - began < launcher.LAUNCHER_EXIT_SECONDS + 5
    assert launcher.RELAY_BACKLOG > 4 * 64 * 1024
