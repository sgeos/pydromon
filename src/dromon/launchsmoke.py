"""The launch smoke check: a stand-in engine of every kind, through the real launcher,
and a daemon sweep while it lives.

**Round 18, item 1, the operator's top request.** Pull request 157 changed the command
that starts the launcher, and the daemon's sweep then took every live launcher of a
Claude Code or Grok line for a dead one and ended its engine. No gate, review or
continuous integration run saw it, because none kept a launched engine alive past a
sweep. The tests of the launcher started it, and the tests of the sweep gave it
hand-written rows, so the one thing that failed, the command that ``ps`` prints for a
launcher that the real module command started, was checked by neither. This module
checks that join and nothing else.

**What it does.** For each engine kind it starts a stand-in harness, a shell loop that
sleeps and starts no model, through the same function that the engine's adapter starts
its harness with, so the same ``dromon.launcher`` module command, environment and
start run. It waits for each launcher's launch record, then runs the daemon's own
sweep, ``launcher.sweep_launches``, twice while the launchers live, and requires that
the sweep ended nothing, that no record was removed and that every harness and
launcher still ran. Then it ends them as the adapters do. It costs nothing and touches
no credential, no network and no line's state. Its launches write their records to a
private directory of their own, so the sweep it runs can never see, and never signal, a
process that is not its own.

**The kinds.** ``claude-code`` starts the ``dromon-launch`` script with the environment
``engine.launch_environment`` gives, which re-executes itself through the module
command, as the Agent SDK's start does. ``codex`` and ``grok`` start through their own
``_app_server`` and ``_grok_agent``. ``pi``, ``cursor-agent`` and ``antigravity`` start
through ``jsonlines.json_lines_factory``, the one function their adapters share, with
each adapter's own input setting. Each runs under both stop policies, since the sweep
decides differently under each. **Limit.** Claude Code's real start goes through the
Agent SDK, which this does not run, since the SDK would need a model. The script, its
environment and its re-execution are the SDK's own inputs, and the check starts them
from the same functions.

**Where it runs.** The gate runs it in ``tests/test_launchsmoke.py``, and the restart
preflight runs it after the engine startup checks, so a successor whose launcher the
sweep would end is refused before the running daemon is stopped.

**A process table that cannot be read.** The sweep reads the table through ``ps``.
Where ``ps`` cannot run, as in an agent sandbox, the sweep itself touches nothing, so
the daemon is safe there, but nothing was shown either. The check then reports
``unverified`` with the reason and does not pass it off as a pass. The preflight prints
that and goes on, since it runs outside any sandbox, and the gate's tests read the
table through a model of it that they build from the launch records, which tests the
sweep's decisions and not ``ps``. **Residual.** In an agent sandbox the gate therefore
verifies recognition and the sweep's choices on a model of the table, and only a run
where ``ps`` works verifies the table itself. The declared ``full-gate`` run is such a
run.

**Porting note.** A port starts its own executable, so its launcher has one fixed name,
and the module-command part of this check goes away. The rest, a live launcher that a
sweep must not end, stays as a test of the port's launcher recognition.
"""

import asyncio
import os
import stat
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dromon import (
    antigravity,
    codex,
    cursor,
    engine,
    grok,
    jsonlines,
    launcher,
    pi,
    spawn,
)
from dromon.processes import TreeReadFailure, TreeRow

KINDS: Final = (
    engine.ENGINE_NAME,
    codex.ENGINE_NAME,
    grok.ENGINE_NAME,
    pi.ENGINE_NAME,
    cursor.ENGINE_NAME,
    antigravity.ENGINE_NAME,
)
"""The engine kinds, each the name its adapter gives, which are the values of
``dromon.driver.ENGINE_OF``. A test finds a kind added there and missing here."""

COMMAND_OF: Final = {
    engine.ENGINE_NAME: "claude",
    codex.ENGINE_NAME: "codex",
    grok.ENGINE_NAME: "grok",
    pi.ENGINE_NAME: pi.COMMAND,
    cursor.ENGINE_NAME: cursor.COMMAND,
    antigravity.ENGINE_NAME: "antigravity",
}
"""The program name each adapter looks up on ``PATH``, which the stand-in is written
under, so that the adapter's own lookup finds it and not an installed engine."""

POLICIES: Final = (launcher.StopPolicy.KILL_ALL, launcher.StopPolicy.LEAVE_BACKGROUND)
"""Both stop policies, since the sweep treats a launch record differently under each."""

SWEEPS: Final = 2
"""How many sweeps run while the launchers live."""

SWEEP_PAUSE_SECONDS: Final = 0.3
"""The pause between sweeps, so that a second sweep reads a table taken after the
first sweep's own signals would have landed."""

RECORD_SECONDS: Final = 60.0
"""How long every launcher has to write its launch record. A launcher imports Python
and starts its harness first, which takes about a second at rest and many under load."""

END_SECONDS: Final = 30.0
"""How long the stand-ins have to end once released, which the launchers' own bounds
fit within, since each stop is bounded by ``launcher.LAUNCHER_EXIT_SECONDS``."""

STAND_IN: Final = """#!/bin/sh
# The stand-in harness of dromon.launchsmoke. It starts no model and does nothing.
trap 'exit 0' TERM INT HUP
while :; do sleep 1; done
"""
"""The harness program. A shell script, so that the process table shows the script's
path in its command, as a script engine's does, which is what the sweep matches a
record's executable against. It exits on the signals a launcher's stop sends."""


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """What one launch of one kind under one policy did."""

    kind: str
    policy: launcher.StopPolicy
    survived: bool
    detail: str


@dataclass(frozen=True, slots=True)
class SmokeReport:
    """What the check found. ``unverified`` is why the process table could not be read,
    in which case no result says that a launch survived."""

    results: tuple[SmokeResult, ...]
    unverified: str | None = None

    @property
    def failed(self) -> tuple[SmokeResult, ...]:
        return tuple(result for result in self.results if not result.survived)

    @property
    def passed(self) -> bool:
        """Whether every launch was shown to survive. ``unverified`` is not a pass."""
        return self.unverified is None and bool(self.results) and not self.failed

    def lines(self) -> list[str]:
        """The report, one line for the verdict and one for each failure."""
        if self.unverified is not None:
            return [
                "launch smoke: UNVERIFIED, the process table could not be read, so"
                f" no launcher was shown to survive a sweep: {self.unverified}"
            ]
        found = [
            f"launch smoke: {len(self.results) - len(self.failed)} of"
            f" {len(self.results)} launches survived {SWEEPS} sweeps"
        ]
        found.extend(
            f"launch smoke: {result.kind} under {result.policy.value} FAILED:"
            f" {result.detail}"
            for result in self.failed
        )
        return found


def _write_stand_in(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text(STAND_IN)
    path.chmod(stat.S_IRWXU)
    return path


def _claude(
    stand_in: Path, environment: Mapping[str, str], cwd: Path
) -> AbstractAsyncContextManager[None]:
    """Claude Code's start, which is the launcher script with the environment of
    ``engine.launch_environment``, the Agent SDK having merged its host environment
    beneath it."""

    @asynccontextmanager
    async def start() -> AsyncGenerator[None]:
        policy = launcher.policy_of(environment)
        launched = {
            **environment,
            **engine.launch_environment(None, policy, None, target=str(stand_in)),
        }
        process = await spawn.subprocess_exec(
            launcher.script_path(),
            cwd=cwd,
            env=launched,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            yield
        finally:
            await launcher.end(process, policy, close_input=True)

    return start()


def _starter(
    kind: str, stand_in: Path, environment: Mapping[str, str], cwd: Path
) -> AbstractAsyncContextManager[object]:
    """The start of ``kind``, which holds a launched stand-in while it is open."""
    if kind == engine.ENGINE_NAME:
        return _claude(stand_in, environment, cwd)
    if kind == codex.ENGINE_NAME:
        return codex._app_server([], cwd, environment)  # pyright: ignore[reportPrivateUsage]
    if kind == grok.ENGINE_NAME:
        return grok._grok_agent([], cwd, environment)  # pyright: ignore[reportPrivateUsage]
    keep_input = kind != cursor.ENGINE_NAME
    # Antigravity's adapter is given its program as a path, and the others look theirs
    # up on ``PATH``, where the stand-in comes first.
    name = str(stand_in) if kind == antigravity.ENGINE_NAME else COMMAND_OF[kind]
    return jsonlines.json_lines_factory(name, keep_input=keep_input)(
        [], cwd, environment
    )


async def _hold(
    kind: str,
    stand_in: Path,
    environment: Mapping[str, str],
    cwd: Path,
    started: asyncio.Event,
    release: asyncio.Event,
) -> str | None:
    """Start ``kind`` and hold it until ``release``, then end it as its adapter does.
    Returns why it could not be started, or ``None``."""
    try:
        async with _starter(kind, stand_in, environment, cwd):
            started.set()
            await release.wait()
    except Exception as error:  # noqa: BLE001
        return f"{type(error).__name__}: {error}"
    return None


def _same(record: launcher.LaunchRecord, stand_in: Path, policy: object) -> bool:
    return record.command == str(stand_in) and record.policy == policy


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def judge(
    records: Sequence[launcher.LaunchRecord],
    rows: Sequence[TreeRow],
    stand_ins: Mapping[str, Path],
    swept: Sequence[launcher.Swept],
    kinds: Sequence[str],
) -> list[SmokeResult]:
    """One result per kind and policy, from the launch records read before the sweeps,
    the rows of the table after them, and what the sweeps did. A launch survives when
    its record is still there, its launcher and harness still run and the launcher is
    recognised as running by ``launcher_running``, asked with the start times the
    record carries, as the sweep asks it, and the sweeps ended nothing. **Audit's
    finding, 2026-11-18.** Without the start times the question was the weaker one."""
    results: list[SmokeResult] = []
    for kind in kinds:
        for policy in POLICIES:
            found = [
                record for record in records if _same(record, stand_ins[kind], policy)
            ]
            if len(found) != 1:
                results.append(
                    SmokeResult(
                        kind,
                        policy,
                        False,
                        f"{len(found)} launch records were written, not one",
                    )
                )
                continue
            record = found[0]
            results.append(_verdict(kind, policy, record, rows, swept))
    return results


def _verdict(
    kind: str,
    policy: launcher.StopPolicy,
    record: launcher.LaunchRecord,
    rows: Sequence[TreeRow],
    swept: Sequence[launcher.Swept],
) -> SmokeResult:
    if not record.path.exists():
        return SmokeResult(
            kind,
            policy,
            False,
            "the sweep removed its launch record, so it took the"
            " launcher for an exited one",
        )
    if not _alive(record.launcher):
        return SmokeResult(kind, policy, False, "its launcher no longer ran")
    if not _alive(record.harness):
        return SmokeResult(kind, policy, False, "its harness no longer ran")
    if not launcher.launcher_running(
        rows,
        record.launcher,
        record.harness,
        record.command,
        launcher_started=record.launcher_started,
        harness_started=record.harness_started,
    ):
        return SmokeResult(
            kind,
            policy,
            False,
            "launcher_running did not take its launcher as running, so the next sweep"
            " would end its harness",
        )
    ended = [item for item in swept if item.ended]
    if ended:
        return SmokeResult(kind, policy, False, f"a sweep ended {ended[0].ended}")
    return SmokeResult(kind, policy, True, "survived")


async def _records(
    directory: Path, wanted: int, tasks: Sequence[asyncio.Task[str | None]]
) -> list[launcher.LaunchRecord]:
    """The launch records in ``directory`` once ``wanted`` exist, or what exists when
    ``RECORD_SECONDS`` have passed or a start has ended, which a start that holds its
    launch open does not do until it is released, so it has failed."""
    deadline = time.monotonic() + RECORD_SECONDS
    found = launcher.read_launches(directory)
    while (
        len(found) < wanted
        and time.monotonic() < deadline
        and not any(task.done() for task in tasks)
    ):
        await asyncio.sleep(0.1)
        found = launcher.read_launches(directory)
    return found


async def check_launches(
    kinds: Sequence[str] = KINDS,
    read: Callable[
        [Sequence[launcher.LaunchRecord]], list[TreeRow] | TreeReadFailure | str
    ]
    | None = None,
) -> SmokeReport:
    """Start every kind under both policies, sweep while they live, and report.

    ``read`` gives the process table to the sweep, and is the daemon's own reading,
    ``launcher.read_rows``, when ``None``. A test that cannot run ``ps`` gives a model
    of the table built from the records it is passed. Every launch is ended before this
    returns, also when it raises. It must run on an event loop of its own, since it
    sets ``PATH`` and the launcher's report directory for its duration."""
    with tempfile.TemporaryDirectory(prefix="dromon-smoke-") as made:
        root = Path(made)
        root.chmod(stat.S_IRWXU)
        programs = root / "bin"
        reports = root / "left"
        work = root / "work"
        for directory in (programs, reports, work):
            directory.mkdir(mode=0o700)
        stand_ins = {
            kind: _write_stand_in(programs, COMMAND_OF[kind]) for kind in kinds
        }
        previous_reports = launcher.report_directory()
        previous_path = os.environ.get("PATH")
        os.environ["PATH"] = os.pathsep.join(
            [str(programs), *([previous_path] if previous_path else [])]
        )
        launcher.configure(reports)
        started: list[asyncio.Event] = []
        release = asyncio.Event()
        tasks: list[asyncio.Task[str | None]] = []
        try:
            for kind in kinds:
                for policy in POLICIES:
                    environment = {
                        **os.environ,
                        launcher.STOP_POLICY_VARIABLE: policy.value,
                    }
                    event = asyncio.Event()
                    started.append(event)
                    tasks.append(
                        asyncio.ensure_future(
                            _hold(
                                kind, stand_ins[kind], environment, work, event, release
                            )
                        )
                    )
            return await _swept(reports, stand_ins, kinds, tasks, release, read)
        finally:
            release.set()
            await _end_all(tasks)
            launcher.configure(previous_reports)
            if previous_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous_path


async def _end_all(tasks: Sequence[asyncio.Task[str | None]]) -> None:
    done, pending = await asyncio.wait(tasks, timeout=END_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    del done


async def _swept(
    reports: Path,
    stand_ins: Mapping[str, Path],
    kinds: Sequence[str],
    tasks: Sequence[asyncio.Task[str | None]],
    release: asyncio.Event,
    read: Callable[
        [Sequence[launcher.LaunchRecord]], list[TreeRow] | TreeReadFailure | str
    ]
    | None,
) -> SmokeReport:
    wanted = len(kinds) * len(POLICIES)
    records = await _records(reports, wanted, tasks)

    def table() -> list[TreeRow] | TreeReadFailure | str:
        if read is None:
            return launcher.read_rows()
        return read(records)

    swept: list[launcher.Swept] = []
    unverified: str | None = None
    for turn in range(SWEEPS):
        if turn:
            await asyncio.sleep(SWEEP_PAUSE_SECONDS)
        outcome = await launcher.sweep_launches(reports, table)
        if isinstance(outcome, (TreeReadFailure, str)):
            unverified = str(outcome)
            break
        swept.extend(outcome)
    failed_starts = [
        why
        for why in (task.result() for task in tasks if task.done())
        if why is not None
    ]
    if unverified is not None:
        return SmokeReport((), unverified)
    rows = table()
    if isinstance(rows, (TreeReadFailure, str)):
        return SmokeReport((), str(rows))
    results = judge(records, rows, stand_ins, swept, kinds)
    if failed_starts:
        results.append(
            SmokeResult(
                "(start)", launcher.StopPolicy.KILL_ALL, False, failed_starts[0]
            )
        )
    release.set()
    return SmokeReport(tuple(results))


def main() -> int:
    """Run the check and print its report. Exits 0 for a pass, 1 for a failure and 2
    when the process table could not be read, which is not a pass."""
    report = asyncio.run(check_launches())
    for line in report.lines():
        print(line)
    if report.passed:
        return 0
    return 2 if report.unverified is not None else 1


if __name__ == "__main__":
    sys.exit(main())
