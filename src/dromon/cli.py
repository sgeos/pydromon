"""The ``dromon`` command, the umbrella client for the Dromon daemon.

``start``, ``status`` and ``stop`` talk to the project's daemon over its socket. The
other subcommands are placeholders that report they are not implemented and exit with
``exits.NOT_IMPLEMENTED``. The subcommand list is expected to grow with the protocol.
"""

import argparse
import asyncio
import getpass
import json
import os
import math
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import dromon
from dromon import __version__, exits, fleet, land, registry, restart, runs
from dromon import proposals as proposal_store
from dromon.client import (
    REQUEST_TIMEOUT_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    DaemonFailed,
    DaemonNotRunning,
    call,
    is_running,
    start,
    stop,
)
from dromon.paths import (
    STATE_DIR_NAME,
    PathError,
    ProjectPaths,
    ensure_private_dir,
    project_paths,
)
from dromon.disclosure import DisclosureError, load_terms
from dromon.driver import ENGINE_OF, MODELS, Credential, Driver, DriverError, Strategy
from dromon import antigravity, capabilities, codex, cursor, evidence, grok, pi
from dromon.engine import ENGINE_NAME as CLAUDE_CODE_ENGINE
from dromon.engine import EngineRefused
from dromon.agents import LINE_TOKEN_ENV, TOKEN_ENV
from dromon.lineconfig import EFFORTS, can_write_workdir
from dromon.projectconfig import ProjectConfig
from dromon.model import AgentAccess
from dromon.protocol import Operation, ProtocolError
from dromon.scheduler import LineSpec, engine_factory
from dromon import settings as engine_settings
from dromon.sandbox import (
    SandboxConfig,
    effective as effective_sandbox,
    merge,
    sandbox_from_settings,
)
from dromon.settings import SettingsConfig, split_settings_file
from dromon.workdir import read_head_commit

PROBE_MODEL: Final = "claude-haiku-5-5"
"""The least expensive subscription model, used for probes and tests by default."""
PROBE_PROMPT: Final = "Reply with the single word: ready"

SUBCOMMANDS: Final[dict[str, str]] = {
    "start": "start the daemon for this project",
    "create": "create a session line",
    "configure": "replace a session line's tools, MCP servers and tool policy",
    "stop": "stop the daemon",
    "restart": "stop the daemon and start it again from the installed code",
    "status": "show the daemon and its session lines",
    "attach": "open an interactive client attached to one line",
    "send": "send a message to a session line or an operator",
    "questions": "list open operator questions",
    "answer": "answer an operator question",
    "mail": "look through an operator's mailbox",
    "wait": "wait for a reply to one of an operator's messages",
    "read": "open a message in an operator's mailbox",
    "ack": "acknowledge a message without replying",
    "sent": "show what became of the mail an operator sent",
    "release": "resume a line the daemon held at a conversation limit",
    "interrupt": "stop a line's turn in flight, as Escape does in an attached client",
    "hibernate": "start no more turns of a line, keeping its mail, session and wakes",
    "awaken": "let a hibernated line be woken again by the mail and wakes it kept",
    "rebind": "move a line to another model and strategy, keeping its identity",
    "settings": "show the project's settings, and a line's, with where each comes from",
    "set": "change a project or line setting, or return it to the layers below",
    "role": "show, create, update or delete roles, read their journals, or give,"
    " vacate or ask to hand over one",
    "run": "clear a lost supervised run after inspecting what is left of it",
    "budget": "list the providers' stated budgets, or state one",
    "reload": "read the project file again and apply its lines, limits and wake policy",
    "config": "check the project file and show what it declares",
    "probe": "run one short turn on the engine and report its capabilities",
    "teacher": "bring the project's local teacher model up, report it, or release it",
    "agent": "register, list or revoke the external agents that may connect",
    "peer": "list the declared peers, issue, revoke or store their tokens, or show"
    " this daemon's key",
    "mcp": "serve an external agent over MCP on standard input and output",
    "proposal": "list or show proposed changes from session lines",
    "land": "stage, screen, commit, gate and push a line's worktree and open a pull"
    " request for it",
    "fleet": "list every daemon of this user on this machine, and reach each by name",
}
"""Planned subcommands and their one-line help, in display order."""


def _start(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    wait: float = arguments.wait
    if is_running(paths):
        print(f"dromon start: already running for {paths.root}")
        return exits.OK
    try:
        pid = start(paths, wait_seconds=wait)
    except (DaemonFailed, PathError, ValueError) as error:
        print(f"dromon start: {error}", file=sys.stderr)
        return exits.FAILURE
    print(f"dromon start: started, pid {pid}")
    return exits.OK


def _status(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    correlation: str | None = arguments.correlation
    try:
        response = call(
            paths.socket,
            Operation.STATUS,
            args=None if correlation is None else {"correlation": correlation},
        )
    except DaemonNotRunning:
        print(f"dromon status: not running for {paths.root}")
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon status: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon status: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    print(f"running, pid {result.get('pid')}, since {result.get('started_at')}")
    print(f"project {result.get('project')}, tick {result.get('tick')}")
    print(
        f"daemon {result.get('daemon_version')},"
        f" protocol {result.get('protocol_version')}"
    )
    log = result.get("event_log")
    if isinstance(log, dict):
        counts: dict[str, object] = log  # pyright: ignore[reportUnknownVariableType]
        print(
            f"events {counts.get('last_seq')}, lines {counts.get('lines')},"
            f" operators {counts.get('operators')},"
            f" open questions {counts.get('open_questions')}"
        )
        print(_log_growth(counts))
    watch = result.get("scheduler")
    if isinstance(watch, dict):
        row = _scheduler_row(cast(Mapping[str, object], watch))
        if row is not None:
            print(row)
    found = result.get("lines")
    lines = cast(list[object], found) if isinstance(found, list) else []
    for entry in lines:
        if isinstance(entry, dict):
            print(_line_row(cast(Mapping[str, object], entry)))
    found = result.get("peers")
    peers = cast(list[object], found) if isinstance(found, list) else []
    for entry in peers:
        if isinstance(entry, dict):
            print(_peer_row(cast(Mapping[str, object], entry)))
    relays = result.get("relays")
    if isinstance(relays, dict):
        for row in _relay_rows(cast(Mapping[str, object], relays)):
            print(row)
    totals = result.get("correlation")
    if isinstance(totals, dict):
        print(_correlation_row(cast(Mapping[str, object], totals)))
    return exits.OK


def _relay_rows(relays: Mapping[str, object]) -> list[str]:
    """The lines of ``dromon status`` for the operators' relays: who is routed,
    what was relayed, what waits unrouted, and each failing notice with its reason.
    Nothing is printed for a project with no route and no operator mail waiting."""
    routed = relays.get("routed")
    names = cast(list[object], routed) if isinstance(routed, list) else []
    unrouted = relays.get("unrouted")
    if not names and not unrouted:
        return []
    rows = [
        "relays for "
        + (", ".join(f"@{name}" for name in names) if names else "no operator")
        + f", {relays.get('delivered')} relayed since the start,"
        f" {unrouted} waiting unrouted, {relays.get('failing')} failing"
    ]
    found = relays.get("failures")
    for entry in cast(list[object], found) if isinstance(found, list) else []:
        if isinstance(entry, dict):
            failure = cast(Mapping[str, object], entry)
            rows.append(
                f"  {failure.get('message_id')} for @{failure.get('operator')},"
                f" attempt {failure.get('attempts')}, again in"
                f" {failure.get('retry_in_seconds')}s: {failure.get('reason')}"
            )
    return rows


def _correlation_row(totals: Mapping[str, object]) -> str:
    """The line of ``dromon status --correlation`` for one key's totals. The cost
    counts every turn under each key it carried, so the shared turns are named."""
    key = totals.get("key")
    if not totals.get("recorded"):
        return f"correlation {key}, no turn recorded"
    unknown = totals.get("unknown_cost_turns")
    return (
        f"correlation {key}, turns {totals.get('turns')},"
        f" shared with other keys {totals.get('shared_turns')},"
        f" known cost ${totals.get('cost_usd')}"
        + (f", turns of unknown cost {unknown}" if unknown else "")
    )


def _scheduler_row(watch: Mapping[str, object]) -> str | None:
    """The line of ``dromon status`` for a fault the scheduler's watch keeps, or
    ``None`` while there is none."""
    fault = watch.get("fault")
    if fault is None:
        return None
    return f"scheduler fault {fault}, {watch.get('faults')} since the daemon started"


def _peer_row(peer: Mapping[str, object]) -> str:
    """One line of ``dromon status`` for a peer: its delivery, what waits and since
    when, when it last answered, and a fault in its forwarder."""
    parts = [f"peer {peer.get('peer')}", str(peer.get("state"))]
    waiting = peer.get("waiting")
    if waiting:
        since = peer.get("oldest_sent_at")
        parts.append(
            f"{waiting} waiting, the oldest {peer.get('oldest')}"
            + (f" since {since}" if since is not None else "")
        )
    else:
        parts.append("nothing waiting")
    contact = peer.get("last_contact")
    parts.append(
        "no contact since the daemon started"
        if contact is None
        else f"last contact {contact}"
    )
    if peer.get("fault") is not None:
        parts.append(f"fault {peer.get('fault')}")
    return ", ".join(parts)


def _line_row(line: Mapping[str, object]) -> str:
    """One line of ``dromon status`` for a session line, naming a hibernation and a
    hold, which each keep the line from waking."""
    parts = [
        f"line {line.get('line_id')}",
        str(line.get("condition", line.get("state"))),
        f"{line.get('pending')} pending",
    ]
    if line.get("hibernated") is True and line.get("condition") != "hibernated":
        parts.append("hibernated")
    if line.get("held_by") is not None and line.get("condition") != "held":
        parts.append(f"held by {line.get('held_by')}")
    return ", ".join(parts)


def _log_growth(log: Mapping[str, object]) -> str:
    """The event log's size and growth, as ``dromon status`` prints it, with an
    unknown value said to be unknown rather than shown as zero."""
    size = log.get("bytes")
    since: object = log.get("since_start")
    grown: Mapping[str, object] = (
        cast(Mapping[str, object], since) if isinstance(since, dict) else {}
    )
    added = grown.get("bytes")
    rate = log.get("events_per_hour")
    parts = [
        f"event log {_size(size)}",
        f"{grown.get('events', 'unknown')} events and"
        f" {_size(added)} since the daemon started",
        f"{rate} events an hour" if rate is not None else "rate not yet known",
        f"compaction {log.get('compaction', 'unknown')}",
    ]
    return ", ".join(parts)


def _size(value: object) -> str:
    if type(value) is not int:
        return "unknown size"
    if abs(value) < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _stop(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    del arguments
    try:
        answer = stop(paths)
    except DaemonNotRunning:
        print(f"dromon stop: not running for {paths.root}")
        return exits.NOT_RUNNING
    except (DaemonFailed, ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon stop: {error}", file=sys.stderr)
        return exits.FAILURE
    warning = answer.get("warning")
    if isinstance(warning, str):
        print(f"dromon stop: {warning}")
    print("dromon stop: stopped")
    return exits.OK


PREFLIGHT_TIMEOUT_SECONDS: Final = restart.CHECK_SECONDS
"""How long the installed daemon's start checks may take. They replay the whole
event log, as a start does."""

RESTART_POLL_SECONDS: Final = 0.25
"""How often ``dromon restart`` asks whether the successor answers."""


def _preflight_argv(paths: ProjectPaths, options: Sequence[str]) -> list[str]:
    """What ``dromon restart`` runs before it asks for the restart. It is the
    successor's own command, on the interpreter this client runs on, with the
    running daemon's options, and ``--check``, so the installed code runs its start
    checks against the project file and a copy of the event log, and starts
    nothing."""
    return [
        sys.executable,
        "-m",
        "dromon.daemon",
        "--project",
        str(paths.root),
        "--runtime-dir",
        str(paths.runtime_dir),
        *options,
        "--check",
    ]


def _options_of(status: Mapping[str, object]) -> list[str]:
    """The running daemon's command-line options from its status, or none when it
    does not say them, as a daemon from before 2026-10-31 does not, or says them in
    a form other than a list of strings."""
    options = status.get("options")
    if not isinstance(options, list):
        return []
    found = cast(list[object], options)
    if not all(isinstance(option, str) for option in found):
        return []
    return [str(option) for option in found]


def _preflight(argv: Sequence[str], runtime_dir: Path | None = None) -> str | None:
    """Why the installed daemon would not start, or ``None``. A restart whose
    successor refuses to start leaves no daemon running, so this is checked before
    the old daemon is asked to stop. The reason is the daemon's own, its lines that
    begin ``dromond:``, or else its last line, as for code that does not import. A
    check that times out is killed, which leaves its copy of the log, so the
    preflight directories in ``runtime_dir`` are then swept."""
    try:
        done = subprocess.run(  # noqa: S603
            list(argv),
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if runtime_dir is not None:
            restart.sweep_checks(runtime_dir, None)
        return str(error)
    except OSError as error:
        return str(error)
    if done.returncode == 0:
        return None
    lines = (done.stderr or done.stdout).strip().splitlines()
    own = [line for line in lines if line.startswith("dromond: ")]
    reason = " ".join(own) if own else (lines[-1] if lines else "no output")
    return f"status {done.returncode}: {reason}"


def _installed_commit() -> str | None:
    """The commit of the code this client, and so a successor, would run."""
    return read_head_commit(Path(dromon.__file__).parent)


def _restart(
    paths: ProjectPaths,
    arguments: argparse.Namespace,
    entry: fleet.IndexEntry | None = None,
) -> int:
    """Ask the daemon to restart, then wait until its successor answers and say
    which commit it runs. The daemon starts its successor itself, so a client that
    leaves early does not stop the restart. The installed code's start checks run
    after the status, which gives the options they run with, and before the
    request, so a successor that would refuse the project leaves the running daemon
    serving, as ``docs/protocol/RESTART.md`` describes.

    ``entry`` is given by ``dromon fleet restart``. **Review of 7ff033f, G1.** The
    daemon's status is then checked against it once more before the restart is asked,
    and the successor is the daemon the index then lists for the same root, under
    another pid, live by its start time, and saying that pid and root, rather than
    whichever process answers the socket under another pid."""
    command = "restart" if entry is None else "fleet restart"
    before = _call(paths, command, Operation.STATUS, {})
    if isinstance(before, int):
        return before
    if entry is not None:
        try:
            fleet.verify(entry, before)
        except fleet.FleetError as error:
            print(f"dromon {command}: {error}", file=sys.stderr)
            return exits.FAILURE
    problem = _preflight(_preflight_argv(paths, _options_of(before)), paths.runtime_dir)
    if problem is not None:
        print(
            f"dromon {command}: the installed daemon refuses to start, so the running"
            f" one was not stopped: {problem}",
            file=sys.stderr,
        )
        return exits.FAILURE
    wait: int = arguments.wait_seconds
    args: dict[str, object] = {
        "operator": arguments.operator,
        "mode": (
            restart.Mode.INTERRUPT if arguments.interrupt else restart.Mode.WAIT
        ).value,
        "wait_seconds": wait,
    }
    if arguments.end_tasks:
        args["end_tasks"] = True
    answer = _call(paths, command, Operation.RESTART, args)
    if isinstance(answer, int):
        return answer
    warning = answer.get("warning")
    if isinstance(warning, str):
        print(f"dromon {command}: {warning}")
    running = answer.get("running")
    count = len(cast(list[object], running)) if isinstance(running, list) else 0
    print(
        f"dromon {command}: draining, with {count} turns running, for at most"
        f" {wait} seconds. The installed code is at commit"
        f" {_installed_commit() or 'unknown'}"
    )
    if entry is None:
        return _await_successor(paths, before, wait)
    return _await_successor(
        paths, before, wait, _fleet_successor(entry), FLEET_TIMEOUT_SECONDS
    )


def _await_successor(
    paths: ProjectPaths,
    before: Mapping[str, object],
    wait: float,
    successor: Callable[[Mapping[str, object]], bool] | None = None,
    request_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> int:
    """Poll until a daemon with another process id answers, and ``successor``, when
    given, says that its status is the successor's, or the restart is abandoned, or
    the bound passes.

    **Review of 7ff033f, G2.** Bounded by its count of polls and by a deadline, and
    each request waits at most ``request_seconds`` and never past the deadline. A
    daemon that accepts and never answers made each poll wait out a whole request
    before, so the count of polls alone let one hold the client for hours. It now
    holds it for at most the drain wait, plus the shutdown and startup bounds of
    ``SHUTDOWN_TIMEOUT_SECONDS`` and ``STARTUP_TIMEOUT_SECONDS``, plus one poll's
    sleep."""
    bound = wait + SHUTDOWN_TIMEOUT_SECONDS + STARTUP_TIMEOUT_SECONDS
    deadline = time.monotonic() + bound
    polls = math.ceil(bound / max(RESTART_POLL_SECONDS, 0.01)) + 1
    for _ in range(polls):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = call(
                paths.socket,
                Operation.STATUS,
                timeout_seconds=min(request_seconds, remaining),
            )
        except (DaemonNotRunning, ProtocolError, TimeoutError, OSError):
            time.sleep(RESTART_POLL_SECONDS)
            continue
        result = response.result or {}
        if (
            response.ok
            and result.get("pid") != before.get("pid")
            and (successor is None or successor(result))
        ):
            print(_restarted(before, result))
            return exits.OK
        state = result.get("restart")
        if isinstance(state, dict):
            found = cast(Mapping[str, object], state)
            if found.get("state") == restart.Outcome.ABANDONED.value:
                print(
                    f"dromon restart: abandoned, so the daemon runs on as before:"
                    f" {found.get('reason')}",
                    file=sys.stderr,
                )
                return exits.FAILURE
        time.sleep(RESTART_POLL_SECONDS)
    awaited = (
        "no restarted daemon answered"
        if successor is None
        else "no restarted daemon that the fleet index lists for this root answered"
    )
    print(
        f"dromon restart: {awaited} within {bound:g} seconds. See"
        f" {paths.daemon_output}",
        file=sys.stderr,
    )
    return exits.FAILURE


def _restarted(before: Mapping[str, object], after: Mapping[str, object]) -> str:
    """What ``dromon restart`` says once the successor answers."""
    was = _commit_of(before)
    now = _commit_of(after)
    text = f"dromon restart: restarted, pid {after.get('pid')}, at commit {now}"
    if now == was:
        return text + ", the same commit as before"
    return text + f", which was {was}"


def _commit_of(status: Mapping[str, object]) -> str:
    daemon = status.get("daemon")
    if isinstance(daemon, dict):
        commit = cast(Mapping[str, object], daemon).get("commit")
        if isinstance(commit, str):
            return commit
    return "unknown"


PARTIAL_ISOLATION_ENGINES: Final = frozenset(
    {
        name
        for name, table in capabilities.TABLES.items()
        if capabilities.isolation_of(table) == "partial"
    }
)
"""Engines ``dromon probe --accept-partial-isolation`` lets run, by the operator's
decision of 2026-10-26. A daemon refuses lines on either."""

UNVERIFIED_CREDENTIAL_ENGINES: Final = frozenset({antigravity.ENGINE_NAME})
"""Engines ``dromon probe --accept-unverified-credential`` lets run on the operator's
confirmation of the credential by hand, by the operator's decision of 2026-10-26."""


RECORDING_ENGINES: Final = frozenset(
    {
        CLAUDE_CODE_ENGINE,
        pi.ENGINE_NAME,
        cursor.ENGINE_NAME,
        antigravity.ENGINE_NAME,
        codex.ENGINE_NAME,
        grok.ENGINE_NAME,
    }
)
"""Engines whose probe ``--record`` can record, as ``dromon.evidence`` says."""


def _probe(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Run one short turn with no tools and report what the engine says.

    The driver's strategy decides which credential is acceptable, so the default
    subscription driver can never start usage billed per token. With ``--scrub`` it
    runs no turn and scrubs a recording instead.
    """
    scrubbed: Path | None = arguments.scrub
    if scrubbed is not None:
        return _scrub(paths, scrubbed, arguments.scrub_to)
    if arguments.scrub_to is not None:
        print("dromon probe: --scrub-to is accepted only with --scrub", file=sys.stderr)
        return exits.FAILURE
    try:
        model: str = arguments.model
        strategy = Strategy(arguments.strategy)
        driver = Driver.for_model(model, strategy)
        accept: bool = arguments.accept_partial_isolation
        if accept and driver.settings.engine not in PARTIAL_ISOLATION_ENGINES:
            raise EngineRefused(
                "--accept-partial-isolation applies only to Cursor Agent and"
                " Antigravity models"
            )
        unverified: bool = arguments.accept_unverified_credential
        if unverified and driver.settings.engine not in UNVERIFIED_CREDENTIAL_ENGINES:
            raise EngineRefused(
                "--accept-unverified-credential applies only to Antigravity models"
            )
        if unverified and not accept:
            raise EngineRefused(
                "--accept-unverified-credential is accepted only together with"
                " --accept-partial-isolation"
            )
        recorded: bool = arguments.record
        with_stderr: bool = arguments.record_stderr
        unchecked: bool = arguments.accept_unverified_check
        if unchecked and not arguments.no_turn:
            raise EngineRefused(
                "--accept-unverified-check is accepted only together with --no-turn"
            )
        if arguments.no_turn:
            if recorded or with_stderr:
                raise EngineRefused(
                    "--no-turn exchanges no frames, so it takes no --record"
                )
            return _sign_in(driver, unchecked)
        if with_stderr and not recorded:
            raise EngineRefused(
                "--record-stderr is accepted only together with --record"
            )
        if recorded and driver.settings.engine not in RECORDING_ENGINES:
            # Every engine records since 2026-10-27. The refusal is kept for an
            # engine added later whose adapter does not record yet.
            raise EngineRefused(
                f"--record is not yet supported for {driver.settings.engine}, whose"
                " adapter does not record its frames"
            )
        line = engine_factory(
            str(paths.root),
            accept_partial_isolation=accept,
            accept_unverified_credential=unverified,
        )(LineSpec("probe", model, strategy), [])
        recording = (
            _recording(paths, driver.settings.engine, model, with_stderr)
            if recorded
            else None
        )
        token = evidence.RECORDING.set(recording)
        try:
            report = asyncio.run(line.run_turn(PROBE_PROMPT))
        finally:
            evidence.RECORDING.reset(token)
            if recording is not None:
                recording.close()
                _report_recording(recording)
    except (DriverError, EngineRefused) as error:
        print(f"dromon probe: {error}", file=sys.stderr)
        return exits.FAILURE
    except (OSError, PathError) as error:
        print(f"dromon probe: cannot record the frames: {error}", file=sys.stderr)
        return exits.FAILURE
    incomplete = recording is not None and recording.failure is not None
    settings = driver.settings
    print(
        f"driver {settings.model} {settings.strategy},"
        f" wcet {driver.wcet_per_iteration} per decision"
    )
    print(
        f"settings effort {settings.effort}, max turns {settings.max_turns},"
        f" tool budget {settings.tool_budget},"
        f" context window {settings.context_window}"
    )
    print(f"engine {settings.engine}, credential {report.credential}")
    for row in capabilities.report_lines(capabilities.table_of(settings.engine)):
        print(row)
    if report.sandbox_mode is not None:
        print(f"sandbox {report.sandbox_mode}")
    found = report.capabilities
    if found is not None:
        print(
            f"engine Claude Code {found.claude_code_version},"
            f" Agent SDK {found.sdk_version}, model {found.model}"
        )
        print(
            f"credential {report.credential} from {found.api_key_source!r},"
            f" tools {'unknown' if found.tools is None else len(found.tools)},"
            f" permission mode {found.permission_mode}"
        )
        print("capabilities " + ", ".join(found.capabilities))
    print(f"turn {report.subtype} in {report.num_turns}, reply {report.text!r}")
    return exits.OK if report.subtype == "success" and not incomplete else exits.FAILURE


def _recording(
    paths: ProjectPaths, engine: str, model: str, with_stderr: bool
) -> evidence.Recording:
    """A new recording under the daemon's state directory, which every line's sandbox
    closes. Raises ``PathError`` or ``OSError`` when the directory or the file cannot
    be made."""
    ensure_private_dir(paths.state_dir)
    ensure_private_dir(paths.state_dir / evidence.PROBES_DIRECTORY)
    return evidence.Recording(
        evidence.probe_path(paths.state_dir, engine), engine, model, stderr=with_stderr
    )


def _report_recording(recording: evidence.Recording) -> None:
    """Say where the raw recording is, how to scrub it, and whether a write failed,
    in which case the turn went on and the file is incomplete."""
    print(f"dromon probe: frames recorded to {recording.path}")
    print(
        "dromon probe: scrub a copy for lines and tests to read with"
        f" dromon probe --scrub {recording.path.name}"
    )
    if recording.failure is not None:
        print(
            "dromon probe: a write to the recording failed, so it stops early and is"
            f" incomplete: {recording.failure}",
            file=sys.stderr,
        )


def default_operator() -> str:
    """The operator identifier for this user, from the login name when it is valid."""
    try:
        name = getpass.getuser().lower()
    except OSError:
        return "operator"
    cleaned = re.sub(r"[^a-z0-9._-]", "-", name).strip("-._")[:64]
    return cleaned or "operator"


def _request(
    paths: ProjectPaths, command: str, operation: Operation, args: dict[str, object]
) -> int:
    try:
        response = call(paths.socket, operation, args=args)
    except DaemonNotRunning:
        print(f"dromon {command}: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon {command}: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    for key, value in sorted(response.result.items()):
        print(f"{key} {value}")
    return exits.OK


def _config_document(arguments: argparse.Namespace) -> dict[str, object] | None:
    """The configuration the tool options describe, or ``None`` if none was given.

    An ``--mcp-config`` file is read here, by the operator's client, and its contents
    travel to the daemon, which stores them in the event log, credentials included.
    A ``--sandbox-config`` file holds a Claude Code sandbox object, or a settings file
    whose ``sandbox`` key is taken. It is validated here with operator authority. A
    ``--settings-config`` file is a Claude Code settings file whose supported keys are
    taken, its ``sandbox`` key included, and any other key is an error.
    """
    tools: list[str] = arguments.tool or []
    allowed: list[str] = arguments.allow_tool or []
    mcp_file: Path | None = arguments.mcp_config
    sandbox_file: Path | None = arguments.sandbox_config
    settings_file: Path | None = arguments.settings_config
    effort: str | None = arguments.effort
    allow_all: bool = arguments.allow_all_tools
    inherit: bool = arguments.inherit_account_mcp
    instructions: list[str] = arguments.instructions or []
    prompt: bool = arguments.claude_code_prompt
    workdir: str | None = arguments.workdir
    given = (
        tools,
        allowed,
        mcp_file,
        allow_all,
        inherit,
        sandbox_file,
        settings_file,
        instructions,
        prompt,
    )
    lifecycle = {
        name: getattr(arguments, name, None)
        for name in (
            "workdir_access",
            "keep_session",
            "keep_session_idle_seconds",
            "keep_session_max_seconds",
            "keep_session_at_max",
            "keep_session_hard_seconds",
            "allow_compact",
            "auto_compact_tokens",
        )
        if getattr(arguments, name, None) is not None
    }
    if not any(given) and effort is None and workdir is None and not lifecycle:
        return None
    document: dict[str, object] = {
        "builtin_tools": tools,
        "allowed_tools": allowed,
        "tool_policy": "allow_all" if allow_all else "allowlist",
        "inherit_account_mcp": inherit,
    }
    document.update(lifecycle)
    if mcp_file is not None:
        document["mcp_servers"] = json.loads(mcp_file.read_text(encoding="utf-8"))
    if sandbox_file is not None:
        loaded: object = json.loads(sandbox_file.read_text(encoding="utf-8"))
        document["sandbox"] = sandbox_from_settings(loaded).document()
    if settings_file is not None:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
        settings, sandbox = split_settings_file(loaded)
        if settings is not None:
            document["settings"] = settings.document()
        if sandbox is not None:
            if "sandbox" in document:
                raise ValueError(
                    "the settings file has a sandbox, and so does --sandbox-config"
                )
            document["sandbox"] = sandbox_from_settings(sandbox).document()
    if effort is not None:
        document["effort"] = effort
    if instructions:
        document["instructions"] = instructions
    if prompt:
        document["claude_code_prompt"] = True
    if workdir is not None:
        document["workdir"] = workdir
    return document


def _create(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "line_id": arguments.line_id,
        "model": arguments.model,
        "strategy": arguments.strategy,
    }
    try:
        config = _config_document(arguments)
    except (OSError, ValueError) as error:
        print(
            f"dromon create: cannot read the line configuration: {error}",
            file=sys.stderr,
        )
        return exits.FAILURE
    if config is not None:
        args["config"] = config
    if arguments.session is not None:
        args["engine_session"] = arguments.session
    limits = {
        name: getattr(arguments, name)
        for name in ("max_autonomous_turns", "max_cost_usd", "cooldown_seconds")
        if getattr(arguments, name) is not None
    }
    if limits:
        args["limits"] = limits
    return _request(paths, "create", Operation.CREATE_LINE, args)


def _configure(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    try:
        config = _config_document(arguments) or {}
    except (OSError, ValueError) as error:
        print(
            f"dromon configure: cannot read the line configuration: {error}",
            file=sys.stderr,
        )
        return exits.FAILURE
    args: dict[str, object] = {"line_id": arguments.line_id, "config": config}
    return _request(paths, "configure", Operation.CONFIGURE_LINE, args)


def _send(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "recipient": arguments.recipient,
        "body": arguments.body,
        "operator": arguments.operator,
    }
    if arguments.reply_to is not None:
        args["reply_to"] = arguments.reply_to
    if arguments.priority:
        args["priority"] = True
    answer = _call(
        paths, "send", Operation.SEND, args, check_after_timeout="dromon sent"
    )
    if isinstance(answer, int):
        return answer
    # The message id alone, as before priority mail, so that a script reading it
    # still can. The result's priority flag is printed only when it is true.
    print(f"message_id {answer.get('message_id')}")
    if answer.get("priority") is True:
        print("priority mail")
    return exits.OK


def _questions(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {} if arguments.all else {"operator": arguments.operator}
    try:
        response = call(paths.socket, Operation.QUESTIONS, args=args)
    except DaemonNotRunning:
        print(f"dromon questions: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon questions: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon questions: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    found = response.result.get("questions")
    entries = cast(list[dict[str, object]], found) if isinstance(found, list) else []
    if not entries:
        print("no open questions")
    for entry in entries:
        print(
            f"{entry.get('question_id')} from {entry.get('line')}"
            f" to {entry.get('operator')}"
        )
        for text_line in str(entry.get("text", "")).splitlines() or [""]:
            print(f"    {text_line}")
        choices = entry.get("choices")
        if isinstance(choices, list) and choices:
            print(
                "    choices: "
                + " | ".join(str(c) for c in cast(list[object], choices))
            )
    return exits.OK


def _mail(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    if arguments.replies_to is not None:
        return _replies(paths, arguments)
    args: dict[str, object] = {
        "operator": arguments.operator,
        "unread_only": arguments.unread,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    if arguments.sender is not None:
        args["sender"] = arguments.sender
    if arguments.issues:
        args["issues_only"] = True
    response = _call(paths, "mail", Operation.MAIL, args)
    if isinstance(response, int):
        return response
    result = response
    entries = cast(list[dict[str, object]], result.get("messages", []))
    print(
        f"{result.get('total')} matching, {result.get('unread')} unread,"
        f" {result.get('unanswered')} unanswered"
    )
    for entry in entries:
        parts = [f"{entry.get('message_id')} from {entry.get('sender')}"]
        parts.append(str(entry.get("kind")))
        if entry.get("reply_to") is not None:
            parts.append(f"replying to {entry.get('reply_to')}")
        parts.append("read" if entry.get("read") is True else "unread")
        if entry.get("outcome") != "unanswered":
            parts.append(str(entry.get("outcome")))
        print(", ".join(parts))
    return exits.OK


def _replies(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Print the replies to one message with their bodies, marking none read.
    **Round 9, item 5.** A wait loop can count and read them in one query."""
    if arguments.sender is not None:
        print("dromon mail: --from does not combine with --replies-to", file=sys.stderr)
        return exits.USAGE
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.replies_to,
        "unread_only": arguments.unread,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    response = _call(paths, "mail", Operation.REPLIES, args)
    if isinstance(response, int):
        return response
    entries = cast(list[dict[str, object]], response.get("messages", []))
    print(
        f"{response.get('total')} matching of {response.get('replies')} replies to"
        f" {response.get('message_id')}, {response.get('unread')} unread"
    )
    for entry in entries:
        parts = [
            f"{entry.get('message_id')} from {entry.get('sender')}",
            str(entry.get("kind")),
            "read" if entry.get("read") is True else "unread",
        ]
        if entry.get("outcome") != "unanswered":
            parts.append(str(entry.get("outcome")))
        print(", ".join(parts))
        body = entry.get("body")
        if isinstance(body, str):
            for row in body.splitlines() or [""]:
                print(f"  {row}")
        else:
            print(
                f"  The body of {entry.get('body_bytes')} bytes is past this answer's"
                f" budget. dromon read {entry.get('message_id')} opens it and marks it"
                " read."
            )
    left_out = response.get("left_out")
    if type(left_out) is int and left_out > 0:
        print(
            f"{left_out} more did not fit this answer. --offset"
            f" {arguments.offset + len(entries)} gives them."
        )
    return exits.OK


WAIT_POLL_SECONDS: Final = 0.5
MAX_WAIT_SECONDS: Final = 86400.0


def _wait(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Poll the bounded replies query until a matching reply arrives or time expires.

    Polling is in the client process. Each socket call has a timeout no longer than the
    remaining wait, so neither the daemon's event loop nor this client waits without a
    bound.
    """
    timeout: float = arguments.timeout
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_WAIT_SECONDS:
        print(
            f"dromon wait: --timeout must be greater than 0 and at most"
            f" {MAX_WAIT_SECONDS:g} seconds",
            file=sys.stderr,
        )
        return exits.USAGE
    deadline = time.monotonic() + timeout
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.replies_to,
        "limit": 50,
    }
    if arguments.sender is not None:
        args["sender"] = arguments.sender
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _wait_timeout(arguments.replies_to, timeout)
        try:
            response = call(
                paths.socket,
                Operation.REPLIES,
                timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, remaining),
                args=args,
            )
        except DaemonNotRunning:
            print(f"dromon wait: daemon stopped for {paths.root}", file=sys.stderr)
            return exits.NOT_RUNNING
        except TimeoutError:
            if time.monotonic() >= deadline:
                return _wait_timeout(arguments.replies_to, timeout)
        except (ProtocolError, OSError) as error:
            print(f"dromon wait: {error}", file=sys.stderr)
            return exits.FAILURE
        else:
            if not response.ok or response.result is None:
                print(f"dromon wait: {response.error_message}", file=sys.stderr)
                return exits.FAILURE
            entries = response.result.get("messages")
            replies = response.result.get("replies")
            left_out = response.result.get("left_out")
            if (isinstance(entries, list) and entries) or (
                type(replies) is int and replies > 0
            ):
                _print_wait_replies(
                    arguments.replies_to,
                    cast(list[object], entries) if isinstance(entries, list) else [],
                )
                if type(left_out) is int and left_out > 0:
                    noun = "reply" if left_out == 1 else "replies"
                    print(
                        f"{left_out} matching {noun} did not fit this answer."
                        " Use dromon mail --replies-to to page through them."
                    )
                return exits.OK
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _wait_timeout(arguments.replies_to, timeout)
        time.sleep(min(WAIT_POLL_SECONDS, remaining))


def _wait_timeout(message_id: str, timeout: float) -> int:
    print(
        f"dromon wait: timed out after {timeout:g} seconds waiting for a reply to"
        f" {message_id}",
        file=sys.stderr,
    )
    return exits.FAILURE


def _print_wait_replies(message_id: str, value: object) -> None:
    """Print reply bodies returned by the query without marking them read."""
    if not isinstance(value, list):
        return
    for entry in cast(list[object], value):
        if not isinstance(entry, dict):
            continue
        reply = cast(dict[str, object], entry)
        reply_id = reply.get("message_id")
        sender = reply.get("sender")
        print(f"reply {reply_id} from {sender} to {message_id}")
        body = reply.get("body")
        if isinstance(body, str):
            for row in body.splitlines() or [""]:
                print(f"  {row}")
        else:
            print(
                f"  The {reply.get('body_bytes')} byte body did not fit the reply"
                " query. Use dromon read to open it."
            )


def _ack(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.message_id,
    }
    return _request(paths, "ack", Operation.ACKNOWLEDGE, args)


def _sent(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "unanswered_only": arguments.unanswered,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    response = _call(paths, "sent", Operation.SENT, args)
    if isinstance(response, int):
        return response
    entries = cast(list[dict[str, object]], response.get("messages", []))
    print(f"{response.get('total')} sent")
    for entry in entries:
        delivery = entry.get("delivery")
        if not isinstance(delivery, str):
            # A daemon before step 5 of federation sends no delivery.
            delivery = (
                "read"
                if entry.get("read") is True
                else "delivered"
                if entry.get("delivered") is True
                else "waiting"
            )
        print(
            f"{entry.get('message_id')} to {entry.get('recipient')},"
            f" {entry.get('kind')}, {delivery}, {entry.get('outcome')}"
        )
    return exits.OK


def _attach(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Wire a platform, the shared runtime and the daemon's socket together."""
    from dromon.ui.link import FleetProjects, SocketDaemon

    if not arguments.poll_seconds > 0:
        print("dromon attach: --poll-seconds must be positive", file=sys.stderr)
        return exits.USAGE
    daemon = SocketDaemon(paths)
    # The fleet index, through which /project moves to another project. Round 13,
    # item 4. An override that is malformed leaves the client without it, and the
    # client still attaches to this project.
    try:
        projects = FleetProjects(fleet.configured_index(os.environ), fleet_start_of)
    except fleet.FleetError as error:
        print(f"dromon attach: no fleet index, {error}", file=sys.stderr)
        projects = None
    given = (
        daemon,
        arguments.line_id,
        arguments.operator,
        arguments.poll_seconds,
        projects,
        paths.root.name,
    )
    if arguments.gui:
        from dromon.ui import window

        return window.run(*given)
    from dromon.ui import terminal

    return terminal.run(*given)


def _disclosure_summary(terms: str | None, root: Path) -> str:
    """Say which disclosure list the project file names, or that it names none.
    **Found by the round 3 reports of 2026-10-28.** A project file without
    ``[daemon] disclosure_terms`` checks nothing, and the team's own file had none
    until ``check_disclosure`` refused for want of a list, so the absence is said
    plainly. A list that cannot be read raises ``DisclosureError``, naming the file
    only, since the daemon refuses to load such a file too. The terms are never
    shown."""
    if terms is None:
        return (
            "disclosure list: none, so no line's mail or report is checked against one"
        )
    load_terms(root / terms)
    return f"disclosure list: {terms}"


def _git_ssh_summary(config: ProjectConfig, paths: ProjectPaths) -> str:
    """Say which agent socket and pinned hosts the project file forwards, and to
    which lines, or that it forwards none. The table is checked as the daemon checks
    it at start, against this command's own ``SSH_AUTH_SOCK``, so a refusal raises
    ``ValueError``. **Assumption.** The daemon's environment may differ from the
    shell that runs this command, so an inherited agent can pass here and be
    refused at start, or the reverse."""
    from dromon.daemon import git_ssh_access

    if config.git_ssh is None:
        return "git ssh: off, so no line's shell commands get an agent socket"
    given = git_ssh_access(config, paths, os.environ)
    access = next(iter(given.values()), None)
    lines = ", ".join(sorted(given)) or "no line"
    socket = config.git_ssh.agent if access is None else access.socket
    return (
        f"git ssh: agent {config.git_ssh.agent} at {socket},"
        f" known hosts {config.git_ssh.known_hosts},"
        f" public key {config.git_ssh.public_key}, for {lines}"
    )


def _config(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Validate ``.dromon/dromon.toml`` without a daemon and summarize it."""
    del arguments
    from dromon.projectconfig import ConfigFileError, config_path, load

    file = config_path(paths.state_dir)
    if not file.exists():
        print(f"no project file at {file}. Built-in defaults apply.")
        return exits.OK
    try:
        config = load(file, paths.root)
    except ConfigFileError as error:
        print(f"dromon config: {error}", file=sys.stderr)
        return exits.FAILURE
    try:
        disclosure = _disclosure_summary(config.disclosure_terms, paths.root)
        git_ssh = _git_ssh_summary(config, paths)
    except (DisclosureError, ValueError) as error:
        print(f"dromon config: {error}", file=sys.stderr)
        return exits.FAILURE
    limits = config.limits
    print(f"{file} is valid")
    print(disclosure)
    print(git_ssh)
    print(
        f"limits: {limits.max_autonomous_turns} autonomous turns,"
        f" ${limits.max_cost_usd} estimated cost, {limits.cooldown_seconds}s cooldown"
    )
    print(f"wake every {config.wake_every_ticks} ticks")
    print(
        f"project: {_sandbox_summary(config.sandbox)},"
        f" {_settings_summary(config.settings)}"
    )
    if config.teacher is not None:
        teacher = config.teacher
        print(
            f"teacher: {teacher.model} at {teacher.endpoint},"
            f" context {teacher.context},"
            f" keep alive {teacher.keep_alive},"
            + (" autostart" if teacher.autostart else " started with dromon teacher up")
        )
    print(
        f"daemon: operator {config.operator}, tick_seconds {config.tick_seconds},"
        f" require_sandbox {config.require_sandbox}"
    )
    print(
        f"federation: local {config.federation.local},"
        f" remote {config.federation.remote},"
        f" listen {config.federation.listen}, tls_listen {config.federation.tls_listen}"
    )
    print(f"federation policy: {config.federation}")
    raw_document = tomllib.loads(file.read_text(encoding="utf-8"))
    for name in sorted(cast(dict[str, object], raw_document.get("profiles", {}))):
        print(f"profile {name}: validated")
    for name, hashes in sorted(config.disclosure_accepted.items()):
        print(f"disclosure accepted {name}: {len(hashes)} exact line hashes")
    print(f"agents: {config.agents}")
    print(f"read_only_paths: {', '.join(config.read_only_paths) or 'none'}")
    print(
        f"leases: {', '.join(config.lease_names) or 'none'},"
        f" ttl_seconds {config.lease_ttl_seconds}"
    )
    for name, command in sorted(config.run_commands.items()):
        print(f"run {name}: {command}")
    for name, relay in sorted(config.relays.relays.items()):
        print(
            f"relay {name}: {relay.kind}, path {relay.path}, command {relay.command},"
            f" timeout_seconds {relay.timeout_seconds}"
        )
    for operator, relay in sorted(config.relays.routes.items()):
        print(f"operator {operator}: relay {relay}")
    for name, role in sorted(config.roles.items()):
        print(f"role {name}: {role}")
    for name, peer in sorted(config.federation.peers.items()):
        print(f"peer {name}: {peer}")
    for line_id, line in sorted(config.lines.items()):
        line_settings = engine_settings.merge([config.settings, line.config.settings])
        line_sandbox = merge([config.sandbox, line.config.sandbox])
        raw_workdir = line.config.workdir or "."
        workdir = Path(raw_workdir).expanduser()
        if not workdir.is_absolute():
            workdir = paths.root / workdir
        workdir = workdir.resolve()
        sandbox_document = effective_sandbox(line_sandbox, workdir, ())
        raw_filesystem = (
            sandbox_document.get("filesystem") if sandbox_document is not None else None
        )
        filesystem = (
            cast(dict[str, object], raw_filesystem)
            if isinstance(raw_filesystem, dict)
            else {}
        )
        raw_allow_write = filesystem.get("allowWrite", [])
        allow_write = (
            [
                path
                for path in cast(list[object], raw_allow_write)
                if isinstance(path, str)
            ]
            if isinstance(raw_allow_write, list)
            else []
        )
        writes = (
            ", ".join(sorted(os.path.expanduser(path) for path in allow_write))
            or "none"
        )
        tools = ", ".join(line.config.builtin_tools) or "no built-in tools"
        servers = ", ".join(line.config.server_names()) or "no MCP servers"
        print(
            f"line {line_id}: {line.model} {line.strategy},"
            f" {line.config.tool_policy}, {tools}, {servers},"
            f" {_sandbox_summary(merge([config.sandbox, line.config.sandbox]))},"
            f" {_settings_summary(line_settings)}"
            + (f", effort {line.config.effort}" if line.config.effort else "")
            + (f", in {line.config.workdir}" if line.config.workdir else "")
            + (", git ssh" if line.git_ssh else "")
        )
        print(f"  effective sandbox allowWrite: {writes}")
    shared_workdirs: dict[str, list[str]] = {}
    for line_id, line in sorted(config.lines.items()):
        if not can_write_workdir(line.config, ENGINE_OF.get(line.model)):
            continue
        raw_workdir = line.config.workdir or "."
        workdir_path = Path(raw_workdir).expanduser()
        if not workdir_path.is_absolute():
            workdir_path = paths.root / workdir_path
        resolved = str(workdir_path.resolve())
        shared_workdirs.setdefault(resolved, []).append(line_id)
    # The seats of one role share its worktree by design, since only its owner writes
    # there, so they count as one writer. A line of another role, or of none, beside
    # them is still warned about.
    role_of = {
        line_id: f"role {role_id}"
        for role_id, role in config.roles.items()
        for line_id in role.seats
    }
    for workdir, line_ids in sorted(shared_workdirs.items()):
        writers = {role_of.get(line_id, f"line {line_id}") for line_id in line_ids}
        if len(writers) > 1:
            print(
                "warning: writing lines "
                + ", ".join(line_ids)
                + f" share workdir {workdir}; give each writing line its own worktree"
            )
    return exits.OK


def _agent(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Register, list or revoke external agents, through the daemon."""
    action: str = arguments.action
    name: str | None = arguments.name
    if action == "send":
        return _agent_send(paths, arguments)
    if action != "list" and name is None:
        print(f"dromon agent {action}: name the agent", file=sys.stderr)
        return exits.USAGE
    if action == "list":
        operation, args = Operation.AGENT_LIST, {}
    elif action == "add":
        operation = Operation.AGENT_ADD
        args = {"agent": name, "access": arguments.access}
    else:
        operation, args = Operation.AGENT_REVOKE, {"agent": name}
    try:
        response = call(paths.socket, operation, args=cast(dict[str, object], args))
    except DaemonNotRunning:
        print(f"dromon agent: no daemon is running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon agent: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon agent: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    if action == "list":
        policy = cast(dict[str, object], result["policy"])
        listening = "listening" if policy["listen"] else "not listening"
        print(
            f"agents {listening}, anonymous {policy['anonymous']},"
            f" at most {policy['max_access']}"
        )
        for entry in cast(list[dict[str, object]], result["agents"]):
            how = "token" if entry["token"] else "anonymous"
            revoked = ", revoked" if entry["revoked"] else ""
            print(f"{entry['agent']} {entry['access']}, {how}{revoked}")
        return exits.OK
    if action == "revoke":
        print(f"revoked {result['agent']}")
        return exits.OK
    print(
        f"registered {result['agent']} with {result['access']} access,"
        f" effective {result['effective_access']}"
    )
    if not result["listening"]:
        print("the project file does not set [agents] listen, so it cannot connect yet")
    print(f"token {result['token']}")
    print("The token is shown once. An MCP configuration for the agent:")
    server = {
        "command": _dromon_command(),
        "args": ["--project", str(paths.root), "mcp", "--agent", name],
        "env": {TOKEN_ENV: result["token"]},
    }
    print(json.dumps({"mcpServers": {"dromon": server}}, indent=2))
    return exits.OK


AGENT_SEND_TIMEOUT_SECONDS: Final = 60.0
"""How long ``dromon agent send`` waits for the daemon, as ``dromon mcp`` does."""


def _agent_send(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Send one note as an external agent, through the agent socket, for a service
    that is not an MCP client. The note goes through the agent's ``send_message``
    tool, so the daemon authenticates the agent and applies its access as it does for
    ``dromon mcp``. The token is read from ``DROMON_AGENT_TOKEN``, never from an
    argument, which other processes could read."""
    agent: str | None = arguments.agent
    to: str | None = arguments.to
    body: str | None = arguments.name
    if agent is None or to is None or body is None:
        print(
            "dromon agent send: give --agent NAME, --to PARTY and the body",
            file=sys.stderr,
        )
        return exits.USAGE
    tool_arguments: dict[str, object] = {"to": to, "body": body}
    correlation: str | None = arguments.correlation
    if correlation is not None:
        tool_arguments["correlation"] = correlation
    args: dict[str, object] = {
        "agent": agent,
        "tool": "send_message",
        "arguments": tool_arguments,
    }
    token = os.environ.get(TOKEN_ENV) or None
    if token is not None:
        args["token"] = token
    try:
        response = call(
            paths.agent_socket, Operation.AGENT_CALL, AGENT_SEND_TIMEOUT_SECONDS, args
        )
    except DaemonNotRunning:
        print(
            f"dromon agent send: no daemon for {paths.root} lets agents connect",
            file=sys.stderr,
        )
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon agent send: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon agent send: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    texts = [
        str(item.get("text", ""))
        for item in cast(list[dict[str, object]], result.get("content", []))
    ]
    if result.get("is_error"):
        print(f"dromon agent send: {' '.join(texts)}", file=sys.stderr)
        return exits.FAILURE
    for text in texts:
        print(text)
    return exits.OK


MAX_PEER_TOKEN_INPUT: Final = 1024
"""The most characters ``dromon peer store`` reads from standard input."""


def _peer(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """List the declared peers, or issue, revoke or store a peer's token, through the
    daemon. ``store`` reads the token from standard input, never from an argument, so
    it does not appear in the process list or the shell's history."""
    action: str = arguments.action
    name: str | None = arguments.name
    if action == "key" and name is not None:
        print("dromon peer key: takes no peer name", file=sys.stderr)
        return exits.USAGE
    if action not in ("list", "key") and name is None:
        print(f"dromon peer {action}: name the peer", file=sys.stderr)
        return exits.USAGE
    args: dict[str, object] = {} if name is None else {"peer": name}
    operation = {
        "list": Operation.PEER_LIST,
        "token": Operation.PEER_TOKEN,
        "revoke": Operation.PEER_REVOKE,
        "store": Operation.PEER_STORE,
        "key": Operation.PEER_KEY,
    }[action]
    if action == "store":
        args["token"] = sys.stdin.readline(MAX_PEER_TOKEN_INPUT).strip()
    try:
        response = call(paths.socket, operation, args=args)
    except DaemonNotRunning:
        print(f"dromon peer: no daemon is running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon peer: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon peer: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    match action:
        case "list":
            listening = "listening" if result["listen"] else "not listening"
            print(f"peers {listening}")
            for entry in cast(list[dict[str, object]], result["peers"]):
                directions = [
                    direction
                    for direction in ("inbound", "outbound")
                    if entry[direction]
                ]
                issued = "issued" if entry["issued"] else "not issued"
                stored = entry["stored"]
                held = (
                    stored
                    if isinstance(stored, str)
                    else ("stored" if stored else "not stored")
                )
                print(
                    f"{entry['peer']} {entry['transport']}"
                    f" {' and '.join(directions) or 'closed'},"
                    f" its token {issued}, its token for us {held}"
                )
        case "token":
            print(f"token {result['token']}")
            print(
                "The token is shown once. Store it in the peer's project with"
                " dromon peer store, under the name that project gives this one."
            )
        case "revoke":
            print(f"revoked the token of peer {result['peer']}")
        case "key":
            made = "made now" if result["made"] else "made earlier"
            print(f"pin_sha256 {result['pin_sha256']}")
            print(f"certificate {result['certificate']}, {made}")
            print(
                "Give the pin and the certificate to the peer's operator, who writes"
                " them as pin_sha256 and certificate in that project's peer table."
                " The private key stays in .dromon/peers."
            )
        case _:
            print(f"stored the token for peer {result['peer']}")
    return exits.OK


def _dromon_command() -> str:
    """The path of this ``dromon``, so an MCP configuration starts the same one."""
    launched = Path(sys.argv[0])
    return str(launched.resolve()) if launched.is_file() else "dromon"


def _mcp(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Serve one external agent over MCP on standard input and output."""
    from dromon.mcp import Bridge, serve

    line: str | None = arguments.line
    if line is not None:
        bridge = Bridge(
            paths.line_socket,
            line,
            os.environ.get(LINE_TOKEN_ENV) or None,
            party="line",
        )
    else:
        bridge = Bridge(
            paths.agent_socket, arguments.agent, os.environ.get(TOKEN_ENV) or None
        )
    return serve(bridge, sys.stdin, sys.stdout)


def _teacher(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Bring the project's teacher up, report it, or release it, without a daemon."""
    from dromon.projectconfig import ConfigFileError, config_path, load
    from dromon.teacher import Teacher, TeacherError

    file = config_path(paths.state_dir)
    try:
        config = load(file) if file.exists() else None
    except ConfigFileError as error:
        print(f"dromon teacher: {error}", file=sys.stderr)
        return exits.FAILURE
    if config is None or config.teacher is None:
        print(f"dromon teacher: {file} declares no [teacher]", file=sys.stderr)
        return exits.FAILURE
    teacher = Teacher(config.teacher, paths.runtime_dir)
    action: str = arguments.action
    try:
        if action == "up":
            print(f"dromon teacher: loading {config.teacher.model}")
            found = teacher.up(force=arguments.force)
        elif action == "down":
            found = teacher.down()
        else:
            found = teacher.status()
    except TeacherError as error:
        print(f"dromon teacher: {error}", file=sys.stderr)
        return exits.FAILURE
    print(
        f"teacher {config.teacher.model} at {config.teacher.endpoint}:"
        f" {found.summary()}"
    )
    if action == "status":
        return exits.OK
    return exits.OK if found.loaded == (action == "up") else exits.FAILURE


def _settings_summary(settings: SettingsConfig | None) -> str:
    return "no engine settings" if settings is None else settings.summary()


def _sandbox_summary(sandbox: SandboxConfig | None) -> str:
    if sandbox is None:
        return "no sandbox settings"
    return "sandboxed" if sandbox.enabled else "not sandboxed"


def _release(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {"line_id": arguments.line_id}
    return _request(paths, "release", Operation.RELEASE, args)


def _interrupt(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Send ``interrupt``. A line with no turn in flight is not a failure, since the
    turn may have ended as the request was made, and the output says which."""
    line_id: str = arguments.line_id
    response = _call(paths, "interrupt", Operation.INTERRUPT, {"line_id": line_id})
    if isinstance(response, int):
        return response
    if response.get("interrupted") is True:
        print(f"interrupted the turn of {line_id}")
    else:
        print(f"{line_id} had no turn in flight, so nothing changed")
    return exits.OK


def _hibernate(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Send ``hibernate``, and say whether a turn in flight finishes first."""
    line_id: str = arguments.line_id
    response = _call(paths, "hibernate", Operation.HIBERNATE, {"line_id": line_id})
    if isinstance(response, int):
        return response
    if response.get("changed") is not True:
        print(f"{line_id} was already hibernated")
    elif response.get("running") is True:
        print(
            f"hibernated {line_id}, whose turn in flight finishes first."
            " dromon interrupt stops it now"
        )
    else:
        print(f"hibernated {line_id}")
    return exits.OK


def _awaken(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    line_id: str = arguments.line_id
    response = _call(paths, "awaken", Operation.AWAKEN, {"line_id": line_id})
    if isinstance(response, int):
        return response
    if response.get("changed") is True:
        print(f"awakened {line_id}")
    else:
        print(f"{line_id} was not hibernated, so nothing changed")
    return exits.OK


def _rebind(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "line_id": arguments.line_id,
        "model": arguments.model,
        "strategy": arguments.strategy,
    }
    return _request(paths, "rebind", Operation.REBIND_LINE, args)


def _settings(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {}
    if arguments.line_id is not None:
        args["line_id"] = arguments.line_id
    response = _call(paths, "settings", Operation.SETTINGS, args)
    if isinstance(response, int):
        return response
    from dromon.ui.data import settings_of

    found = settings_of(response)
    sections = [("project.", "Project", found.project)]
    if arguments.line_id is not None:
        sections.insert(0, ("", f"Line {found.line_id}", found.line))
    for prefix, title, entries in sections:
        print(title)
        for entry in entries:
            source = f"  [{entry.source}]" if entry.source else "  [read only]"
            print(f"  {prefix}{entry.key} = {entry.shown}{source}")
    return exits.OK


def _set(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Check the value against the daemon's settings, as the interactive client does,
    then send it. A line key needs ``--line``, and a project key its prefix."""
    from dromon.ui.commands import PROJECT_PREFIX, Set
    from dromon.ui.data import settings_of
    from dromon.ui.settings import request_of

    project = arguments.key.startswith(PROJECT_PREFIX)
    if not project and arguments.line_id is None:
        print(
            f"dromon set: {arguments.key} is a line setting and needs --line,"
            f" or write {PROJECT_PREFIX}{arguments.key} for the project",
            file=sys.stderr,
        )
        return exits.USAGE
    if arguments.unset == bool(arguments.value):
        print("dromon set: give a value or --unset, not both", file=sys.stderr)
        return exits.USAGE
    query: dict[str, object] = {}
    if arguments.line_id is not None:
        query["line_id"] = arguments.line_id
    response = _call(paths, "set", Operation.SETTINGS, query)
    if isinstance(response, int):
        return response
    text = None if arguments.unset else " ".join(arguments.value)
    checked = request_of(settings_of(response), Set(arguments.key, text))
    if isinstance(checked, str):
        print(f"dromon set: {checked}", file=sys.stderr)
        return exits.FAILURE
    args: dict[str, object] = {
        "scope": "project" if checked.project else "line",
        "key": checked.key,
    }
    if not checked.project:
        args["line_id"] = arguments.line_id
    if checked.value is not None:
        value = checked.value
        args["value"] = list(value) if isinstance(value, tuple) else value
    return _request(paths, "set", Operation.SET_SETTING, args)


def _reload(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Reload the project file, and print each role the file left as it was, one
    notice to a line, since a run-time change of a role is not undone silently."""
    del arguments
    result = _call(paths, "reload", Operation.RELOAD_PROJECT, {})
    if isinstance(result, int):
        return result
    notices = cast(list[object], result.get("role_notices", []))
    for key, value in sorted(result.items()):
        if key != "role_notices":
            print(f"{key} {value}")
    for notice in notices:
        print(f"notice: {notice}")
    return exits.OK


def _read(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.message_id,
    }
    response = _call(paths, "read", Operation.READ, args)
    if isinstance(response, int):
        return response
    reply = response.get("reply_to")
    suffix = f", replying to {reply}" if reply is not None else ""
    print(f"{response.get('message_id')} from {response.get('sender')}{suffix}")
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        print("choices: " + " | ".join(str(c) for c in cast(list[object], choices)))
    print()
    print(response.get("body"))
    return exits.OK


def _run(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Clear a lost run, so its scope may start another. Its command may still run,
    so the operator inspects what is left of it first."""
    run_id: str = arguments.run_id
    args: dict[str, object] = {"run_id": run_id, "operator": arguments.operator}
    result = _call(paths, "run clear", Operation.RUN_CLEAR, args)
    if isinstance(result, int):
        return result
    print(
        f"run {result['run_id']} cleared by {result['operator']} at"
        f" {result['cleared_at']}"
    )
    return exits.OK


def _role(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Show roles, read a journal entry, create, update or delete a role, or change
    or ask for a change of owner."""
    action: str = arguments.action
    name: str | None = arguments.name
    command = f"role {action}"
    operator: str = arguments.operator
    if action != "status" and name is None:
        what = "the journal entry" if action == "entry" else "the role"
        print(f"dromon {command}: name {what}", file=sys.stderr)
        return exits.USAGE
    needed = {"take": "seat", "handoff": "to", "create": "workdir"}.get(action)
    if action == "create" and arguments.seats is None:
        needed = "seats"
    if needed is not None and getattr(arguments, needed) is None:
        print(f"dromon {command}: give --{needed}", file=sys.stderr)
        return exits.USAGE
    if action in ("create", "update", "delete"):
        return _role_change(paths, arguments, command)
    match action:
        case "status":
            operation = Operation.ROLES
            args: dict[str, object] = {} if name is None else {"role_id": name}
        case "entry":
            operation = Operation.ROLE_ENTRY
            args = {"entry_id": name}
        case "take":
            operation = Operation.ROLE_TAKE
            args = {"role_id": name, "seat": arguments.seat, "operator": operator}
        case "vacate":
            operation = Operation.ROLE_VACATE
            args = {"role_id": name, "operator": operator}
        case _:
            operation = Operation.ROLE_HANDOFF
            args = {"role_id": name, "to": arguments.to, "operator": operator}
    result = _call(paths, command, operation, args)
    if isinstance(result, int):
        return result
    match action:
        case "status":
            roles = cast(list[dict[str, object]], result["roles"])
            retired = cast(list[dict[str, object]], result.get("retired", []))
            if not roles:
                print("no roles are declared")
            for role in roles:
                _print_role(role)
            for gone in retired:
                print(
                    f"retired role {gone['role_id']}, by {gone['by']} at"
                    f" {gone['retired_at']}"
                )
        case "entry":
            print(
                f"entry {result['entry_id']} of role {result['role_id']}, a"
                f" {result['kind']} by {result['line_id']} at {result['noted_at']},"
                f" worktree {result['workdir_commit'] or 'unknown'}, daemon"
                f" {result['daemon_commit'] or 'unknown'}"
            )
            print(result["body"])
        case "take" | "vacate":
            owner = result["owner"] or "no seat"
            print(
                f"role {result['role_id']} passed from"
                f" {result['previous'] or 'no seat'} to {owner}"
            )
            if "warning" in result:
                print(f"warning: {result['warning']}")
        case _:
            print(
                f"asked {result['owner']} to hand role {result['role_id']} to"
                f" {result['to']}, in priority mail {result['message_id']}"
            )
            if result["owner_hibernated"] is True:
                print(
                    f"{result['owner']} is hibernated, so it reads the request"
                    " once awakened"
                )
    return exits.OK


ROLE_TERMS: Final = (
    "workdir",
    "seats",
    "no_own",
    "may_own",
    "handoff_seconds",
    "failover",
    "handoff_below_percent",
)
"""Each term option of ``dromon role create`` and ``update``, whose attribute is
also its argument of ``role_create`` and ``role_update``."""


def _role_change(
    paths: ProjectPaths, arguments: argparse.Namespace, command: str
) -> int:
    """``dromon role create``, ``update`` or ``delete``, which only an operator may
    run, through the client socket."""
    action: str = arguments.action
    args: dict[str, object] = {
        "role_id": arguments.name,
        "operator": arguments.operator,
    }
    if action == "delete":
        result = _call(paths, command, Operation.ROLE_DELETE, args)
        if isinstance(result, int):
            return result
        unbound = cast(list[str], result["unbound"])
        print(
            f"role {result['role_id']} retired at {result['retired_at']}"
            + (
                f", seats {', '.join(unbound)} unbound and still lines"
                if unbound
                else ""
            )
        )
        if "warning" in result:
            print(f"warning: {result['warning']}")
        return exits.OK
    for key in ROLE_TERMS:
        value: object = getattr(arguments, key)
        if value is None:
            continue
        if key == "seats":
            value = [
                part.strip() for part in cast(str, value).split(",") if part.strip()
            ]
        args[key] = value
    if action == "create" and "may_own" in args:
        print(f"dromon {command}: --may-own is for update", file=sys.stderr)
        return exits.USAGE
    operation = Operation.ROLE_CREATE if action == "create" else Operation.ROLE_UPDATE
    result = _call(paths, command, operation, args)
    if isinstance(result, int):
        return result
    _print_role(cast(Mapping[str, object], result["role"]))
    for label, key in (("made seats", "bound"), ("unbound, still lines", "unbound")):
        lines = cast(list[str], result[key])
        if lines:
            print(f"{label}: {', '.join(lines)}")
    if "warning" in result:
        print(f"warning: {result['warning']}")
    return exits.OK


def _print_role(role: Mapping[str, object]) -> None:
    """One role of ``roles``, with every term, its seats and the latest journal
    entries."""
    print(
        f"role {role['role_id']} in {role['workdir']}, owner"
        f" {role['owner'] or 'none, vacant'}, handoff within"
        f" {role['handoff_seconds']} seconds"
    )
    below = role["handoff_below_percent"]
    print(
        f"  failover {role['failover']}, next seat {role['next_seat'] or 'none'},"
        " handoff asked " + (f"below {below} percent" if below else "never by budget")
    )
    change = cast(Mapping[str, object] | None, role["last_change"])
    if change is not None:
        back = f", back to {change['back_to']}" if change["back_to"] else ""
        print(
            f"  last change {change['cause']} by {change['by']} at"
            f" {change['changed_at']}, from {change['previous'] or 'no seat'} to"
            f" {change['owner'] or 'no seat'}{back}"
        )
    held = cast(Mapping[str, object] | None, role["held"])
    order = cast(list[str], role["order"])
    print(
        f"  held by the {role['held_by']}"
        + ("" if held is None else f", since {held['by']} at {held['at']}")
        + ", failover order "
        + (", ".join(order) or "none declared")
    )
    for seat in cast(list[Mapping[str, object]], role["seats"]):
        marks = [str(seat["condition"])]
        if seat["owner"] is True:
            marks.insert(0, "owner")
        if seat["may_own"] is not True:
            marks.append("may not own")
        if seat["hibernated"] is True:
            marks.append("hibernated")
        if seat["usable"] is not True and seat["owner"] is not True:
            marks.append("not usable for failover")
        if seat["waits_unconfined"] is True:
            marks.append("waits, its engine cannot confine it")
        budget = cast(Mapping[str, object] | None, seat["budget"])
        if budget is not None:
            marks.append(f"budget {_budget_text(budget)}")
        print(
            f"  seat {seat['line_id']} on {seat['model']}, {', '.join(marks)},"
            f" at {seat['workdir_commit'] or 'no commit'}"
        )
    for entry in cast(list[Mapping[str, object]], role["journal"]):
        print(
            f"  entry {entry['entry_id']}, a {entry['kind']} by {entry['line_id']} at"
            f" {entry['noted_at']}, {entry['chars']} characters"
        )
    # Round 10 review, F6 and F4. Printed last, so the lines above keep their places.
    for difference in cast(list[str], role["file_differences"]):
        print(f"  the project file is not applied, since {difference}")
    for line_id in cast(list[str], role["beside_owner"]):
        print(
            f"  line {line_id} waits, since it could write the worktree and is not a"
            " seat"
        )


def _budget_text(budget: Mapping[str, object]) -> str:
    """A stated budget, with its source and time, since it is a snapshot."""
    parts: list[str] = []
    if budget["remaining_percent"] is not None:
        parts.append(f"{budget['remaining_percent']} percent")
    if budget["resets_at"] is not None:
        parts.append(f"resets {budget['resets_at']}")
    parts.append(f"stated by {budget['source']} at {budget['stated_at']}")
    return ", ".join(parts)


def _budget(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """List the providers' stated budgets, or state one as an operator."""
    action: str = arguments.action
    command = f"budget {action}"
    if action == "list":
        result = _call(paths, command, Operation.BUDGETS, {})
        if isinstance(result, int):
            return result
        budgets = cast(list[Mapping[str, object]], result["budgets"])
        if not budgets:
            print("no budget is stated")
        for budget in budgets:
            spent = ", spent now" if budget["spent"] is True else ""
            note = f", {budget['note']}" if budget["note"] else ""
            print(f"{budget['provider']}: {_budget_text(budget)}{spent}{note}")
        providers = cast(list[str], result["providers"])
        print(f"providers {', '.join(providers)}")
        return exits.OK
    provider: str | None = arguments.provider
    percent: int | None = arguments.percent
    resets_at: str | None = arguments.resets_at
    if provider is None:
        print(f"dromon {command}: name the provider", file=sys.stderr)
        return exits.USAGE
    if percent is None and resets_at is None:
        print(f"dromon {command}: give --percent, --resets-at or both", file=sys.stderr)
        return exits.USAGE
    args: dict[str, object] = {"provider": provider, "note": arguments.note}
    if percent is not None:
        args["remaining_percent"] = percent
    if resets_at is not None:
        args["resets_at"] = resets_at
    result = _call(paths, command, Operation.BUDGET_SET, args)
    if isinstance(result, int):
        return result
    print(f"{result['provider']}: {_budget_text(result)}")
    for followed in cast(list[str], result["followed"]):
        print(followed)
    return exits.OK


def _call(
    paths: ProjectPaths,
    command: str,
    operation: Operation,
    args: dict[str, object],
    *,
    check_after_timeout: str | None = None,
) -> dict[str, object] | int:
    """The result of one request, or the exit status that reports its failure.

    **Found on 2026-10-29.** ``dromon send`` timed out while the daemon was busy, yet
    the message had been recorded, so a retry would have sent it twice. A timeout
    therefore says that the request may have taken effect, and ``check_after_timeout``
    names how to check before repeating it."""
    try:
        response = call(paths.socket, operation, args=args)
    except DaemonNotRunning:
        print(f"dromon {command}: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except TimeoutError as error:
        print(
            f"dromon {command}: {error}. The daemon may still have carried the"
            " request out, so check before repeating it"
            + (f", with {check_after_timeout}" if check_after_timeout else "")
            + ".",
            file=sys.stderr,
        )
        return exits.FAILURE
    except (ProtocolError, OSError) as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon {command}: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    return response.result


def _answer(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "question_id": arguments.question_id,
        "body": arguments.body,
        "operator": arguments.operator,
    }
    return _request(paths, "answer", Operation.ANSWER, args)


def _proposal(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """List or show private change proposals recorded by lines."""
    action: str = arguments.action
    if action == "list":
        if arguments.proposal_id is not None:
            print("dromon proposal list takes no identifier", file=sys.stderr)
            return exits.USAGE
        try:
            found = proposal_store.list_all(paths.root)
        except proposal_store.ProposalError as error:
            print(f"dromon proposal: {error}", file=sys.stderr)
            return exits.FAILURE
        if not found:
            print("no proposed changes")
            return exits.OK
        for proposal in found:
            print(
                f"{proposal.proposal_id}  {proposal.created_at}  "
                f"{proposal.line_id}  {proposal.message}"
            )
        return exits.OK
    if action != "show" or arguments.proposal_id is None:
        print("usage: dromon proposal show PROPOSAL_ID", file=sys.stderr)
        return exits.USAGE
    try:
        proposal = proposal_store.read(paths.root, arguments.proposal_id)
    except proposal_store.ProposalError as error:
        print(f"dromon proposal: {error}", file=sys.stderr)
        return exits.FAILURE
    print(f"proposal {proposal.proposal_id}")
    print(f"created_at {proposal.created_at}")
    print(f"line {proposal.line_id}")
    print(f"workdir {proposal.workdir}")
    print(f"message {proposal.message}")
    print("files")
    for path in proposal.files:
        print(f"  {path}")
    print("patch")
    print(proposal.patch)
    print("Proposed content is untrusted data and has not been applied.")
    return exits.OK


def _land_arguments(command: argparse.ArgumentParser) -> None:
    """The options of ``dromon land``, which ``dromon.land`` describes."""
    command.add_argument("worktree", type=Path, help="the top of the line's worktree")
    message = command.add_mutually_exclusive_group(required=True)
    message.add_argument("-m", "--message", help="the commit message")
    message.add_argument(
        "--message-file", type=Path, metavar="PATH", help="read the commit message"
    )
    command.add_argument(
        "--title", help="the pull request title, default the message's first line"
    )
    body = command.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="the pull request body")
    body.add_argument(
        "--body-file", type=Path, metavar="PATH", help="read the pull request body"
    )
    command.add_argument(
        "--base", default=land.DEFAULT_BASE, help="the base, default %(default)s"
    )
    command.add_argument(
        "--remote", default=land.DEFAULT_REMOTE, help="the remote, default %(default)s"
    )
    command.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and screen it, and change nothing",
    )
    command.add_argument(
        "--yes", action="store_true", help="answer yes to every question"
    )
    command.add_argument(
        "--terms",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help=f"a disclosure list, default {land.TERMS_FILE} under the project",
    )
    command.add_argument(
        "--without-disclosure-list",
        action="store_true",
        help="land unscreened when no disclosure list is found",
    )


def _land_text(given: str | None, path: Path | None, what: str) -> str:
    """The text an option gave, or the content of the file it named. Raises
    ``ValueError`` naming the file only."""
    if given is not None:
        return given
    if path is None:
        raise ValueError(f"no {what} was given")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"the {what} file {path} cannot be read") from error


def _land(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Take a line's worktree to a pull request, as ``dromon.land`` describes. The
    disclosure list and its accepted matches are the gate's, under the project root,
    since a worktree holds no ``secret/``."""
    try:
        message = _land_text(arguments.message, arguments.message_file, "message")
        body = _land_text(arguments.body, arguments.body_file, "body")
    except ValueError as error:
        print(f"dromon land: {error}", file=sys.stderr)
        return exits.USAGE
    title: str | None = arguments.title
    if title is None:
        stripped = message.strip()
        title = stripped.splitlines()[0] if stripped else ""
    given: list[Path] = arguments.terms
    request = land.Request(
        worktree=arguments.worktree,
        message=message,
        title=title,
        body=body,
        base=arguments.base,
        remote=arguments.remote,
        dry_run=arguments.dry_run,
        yes=arguments.yes,
        terms_files=tuple(given) or (paths.root / land.TERMS_FILE,),
        accepted_file=paths.root / land.ACCEPTED_FILE,
        without_list=arguments.without_disclosure_list,
    )
    return land.Lander(request).land()


# The fleet, every daemon of this user on this machine. Round 13. Each command reads
# the fleet index and sends the operation its single-project command sends, to the
# daemon's own operator socket and nowhere else, so it adds no authority.

FLEET_TIMEOUT_SECONDS: Final = 2.0
"""How long a fleet command waits for each daemon, so one that hangs delays the rest by
at most this, for each of its requests."""

fleet_start_of: registry.StartOf = runs.process_start
"""How the client reads a daemon's start time, which a test replaces, since a sandbox
denies ``ps``."""


class _NoAnswer(Exception):
    """A daemon of the fleet did not answer, or refused, with the reason."""


def _fleet_ask(
    entry: fleet.IndexEntry, operation: Operation, args: dict[str, object]
) -> dict[str, object]:
    """One request to ``entry``'s own socket. Raises ``_NoAnswer``."""
    try:
        response = call(entry.socket, operation, FLEET_TIMEOUT_SECONDS, args)
    except DaemonNotRunning as error:
        raise _NoAnswer(f"not answering, {error}") from error
    except (ProtocolError, TimeoutError, OSError) as error:
        raise _NoAnswer(f"not answering, {error}") from error
    if not response.ok or response.result is None:
        raise _NoAnswer(f"refused {operation}, {response.error_message}")
    return response.result


def _fleet_verified(entry: fleet.IndexEntry) -> dict[str, object]:
    """The status of ``entry``'s daemon, once it says the pid and root the index
    names. Raises ``_NoAnswer``."""
    status = _fleet_ask(entry, Operation.STATUS, {})
    try:
        fleet.verify(entry, status)
    except fleet.FleetError as error:
        raise _NoAnswer(str(error)) from error
    return status


def _fleet_successor(
    entry: fleet.IndexEntry,
) -> Callable[[Mapping[str, object]], bool]:
    """Whether a status is that of ``entry``'s successor, the daemon the index now
    lists for ``entry``'s root and socket under another pid, live by its start time,
    whose status gives that pid and root. Review of 7ff033f, G1. A successor that
    could not be listed is therefore never confirmed, and the wait reports that."""

    def confirmed(status: Mapping[str, object]) -> bool:
        try:
            found = fleet.entry_of(fleet.configured_index(os.environ), entry.root)
            if (
                found is None
                or found.pid == entry.pid
                or found.root != entry.root
                or found.socket != entry.socket
                or fleet.liveness(found, fleet_start_of) is not fleet.Liveness.LIVE
            ):
                return False
            fleet.verify(found, status)
        except fleet.FleetError:
            return False
        return True

    return confirmed


def _fleet_survey(command: str) -> list[fleet.Indexed] | None:
    try:
        return fleet.survey(fleet.configured_index(os.environ), fleet_start_of)
    except fleet.FleetError as error:
        print(f"dromon fleet {command}: {error}", file=sys.stderr)
        return None


def _fleet_paths(entry: fleet.IndexEntry) -> ProjectPaths:
    """The paths of ``entry``'s project, whose socket is the one the index names."""
    return ProjectPaths(entry.root, entry.root / STATE_DIR_NAME, entry.socket.parent)


def _fleet_mapping(value: object) -> Mapping[str, object]:
    """``value`` as a mapping, or an empty one, since a daemon's answer is data."""
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _fleet_count(value: object, key: str) -> object:
    return _fleet_mapping(value).get(key)


def _fleet_row(indexed: fleet.Indexed, operator: str) -> tuple[str, bool]:
    """The row of ``fleet list`` for one daemon, and whether it answered. A stale or
    unknown entry is shown and never asked."""
    entry = indexed.entry
    if indexed.liveness is not fleet.Liveness.LIVE:
        return f"{entry.name}: {indexed.liveness}, pid {entry.pid}, not asked", True
    try:
        status = _fleet_verified(entry)
        mail = _fleet_ask(
            entry,
            Operation.MAIL,
            {"operator": operator, "issues_only": True, "limit": 1},
        )
    except _NoAnswer as error:
        return f"{entry.name}: {error}", False
    daemon = status.get("daemon")
    stale = _fleet_count(daemon, "stale")
    commit = _fleet_count(daemon, "commit")
    behind = (
        ", behind its checkout"
        if stale is True
        else ""
        if stale is False
        else ", whether behind its checkout is unknown"
    )
    log = status.get("event_log")
    return (
        f"{entry.name}: running, commit"
        f" {str(commit)[:12] if isinstance(commit, str) else 'unknown'},"
        f" {_fleet_count(log, 'lines')} lines,"
        f" {_fleet_count(log, 'open_questions')} open questions,"
        f" {mail.get('unread')} unread, {mail.get('total')} open issues{behind}"
    ), True


def _fleet_list(arguments: argparse.Namespace) -> int:
    found = _fleet_survey("list")
    if found is None:
        return exits.FAILURE
    if not found:
        print("no daemon is in the fleet index")
        return exits.OK
    answered = True
    for indexed in found:
        row, ok = _fleet_row(indexed, arguments.operator)
        answered = answered and ok
        print(row)
        print(f"    {indexed.entry.root}")
    return exits.OK if answered else exits.FAILURE


def _fleet_each(
    command: str,
    found: list[fleet.Indexed],
    ask: Callable[[fleet.IndexEntry], None],
) -> int:
    """Run ``ask`` on each live daemon, reporting one that does not answer and going
    on with the rest. A stale or unknown entry is reported and never asked."""
    answered = True
    for indexed in found:
        entry = indexed.entry
        if indexed.liveness is not fleet.Liveness.LIVE:
            print(f"{entry.name}: {indexed.liveness}, not asked", file=sys.stderr)
            continue
        try:
            _fleet_verified(entry)
            ask(entry)
        except _NoAnswer as error:
            answered = False
            print(f"dromon fleet {command}: {entry.name}: {error}", file=sys.stderr)
    return exits.OK if answered else exits.FAILURE


def _fleet_questions(arguments: argparse.Namespace) -> int:
    found = _fleet_survey("questions")
    if found is None:
        return exits.FAILURE
    args: dict[str, object] = {} if arguments.all else {"operator": arguments.operator}

    def ask(entry: fleet.IndexEntry) -> None:
        result = _fleet_ask(entry, Operation.QUESTIONS, args)
        listed = result.get("questions")
        for item in cast(list[object], listed) if isinstance(listed, list) else []:
            question = _fleet_mapping(item)
            print(
                f"{entry.name}:{question.get('question_id')} from"
                f" {question.get('line')} to {question.get('operator')}"
            )
            for text_line in str(question.get("text", "")).splitlines() or [""]:
                print(f"    {text_line}")

    return _fleet_each("questions", found, ask)


def _fleet_mail(arguments: argparse.Namespace, issues: bool) -> int:
    command = "issues" if issues else "mail"
    found = _fleet_survey(command)
    if found is None:
        return exits.FAILURE
    args: dict[str, object] = {"operator": arguments.operator, "limit": arguments.limit}
    if issues:
        args["issues_only"] = True
    elif arguments.unread:
        args["unread_only"] = True

    def ask(entry: fleet.IndexEntry) -> None:
        result = _fleet_ask(entry, Operation.MAIL, args)
        listed = result.get("messages")
        messages = cast(list[object], listed) if isinstance(listed, list) else []
        if messages:
            print(f"{entry.name}: {result.get('total')} matching")
        for item in messages:
            message = _fleet_mapping(item)
            state = "read" if message.get("read") is True else "unread"
            print(
                f"{entry.name}:{message.get('message_id')} from"
                f" {message.get('sender')}, {message.get('kind')}, {state}"
            )

    return _fleet_each(command, found, ask)


def _fleet_target(command: str, project: str) -> fleet.IndexEntry | None:
    """The live, verified daemon ``project`` names, or ``None`` once the reason is
    printed."""
    found = _fleet_survey(command)
    if found is None:
        return None
    try:
        entry = fleet.resolve(found, project)
        _fleet_verified(entry)
    except (fleet.FleetError, _NoAnswer) as error:
        print(f"dromon fleet {command}: {error}", file=sys.stderr)
        return None
    return entry


def _fleet_routed(
    command: str, target: str, what: str
) -> tuple[ProjectPaths, str] | None:
    """The paths of the daemon ``target``, ``PROJECT:ITEM``, names, and the item."""
    try:
        project, item = fleet.split_target(target, what)
    except fleet.FleetError as error:
        print(f"dromon fleet {command}: {error}", file=sys.stderr)
        return None
    entry = _fleet_target(command, project)
    return None if entry is None else (_fleet_paths(entry), item)


def _fleet_hosts() -> int:
    try:
        hosts = fleet.read_hosts(fleet.configured_hosts_file(os.environ))
    except fleet.FleetError as error:
        print(f"dromon fleet hosts: {error}", file=sys.stderr)
        return exits.FAILURE
    if not hosts:
        print("no host is configured")
    for host in hosts.values():
        print(f"{host.name}: ssh {host.destination}, {host.remote_dromon} there")
    print(
        "Reaching these hosts is designed and not implemented, as"
        " docs/architecture/FLEET.md says"
    )
    return exits.OK


def _fleet(arguments: argparse.Namespace) -> int:
    """``dromon fleet``, which needs no project of its own."""
    action: str = arguments.fleet_action
    match action:
        case "list":
            return _fleet_list(arguments)
        case "questions":
            return _fleet_questions(arguments)
        case "mail" | "issues":
            return _fleet_mail(arguments, action == "issues")
        case "hosts":
            return _fleet_hosts()
        case "status" | "restart":
            entry = _fleet_target(action, arguments.project_name)
            if entry is None:
                return exits.FAILURE
            if action == "status":
                return _status(_fleet_paths(entry), arguments)
            return _restart(_fleet_paths(entry), arguments, entry)
        case "answer":
            routed = _fleet_routed(action, arguments.target, "QUESTION")
            if routed is None:
                return exits.FAILURE
            arguments.question_id = routed[1]
            return _answer(routed[0], arguments)
        case "send":
            routed = _fleet_routed(action, arguments.target, "RECIPIENT")
            if routed is None:
                return exits.FAILURE
            arguments.recipient = routed[1]
            return _send(routed[0], arguments)
        case _:
            routed = _fleet_routed(action, arguments.target, "MESSAGE")
            if routed is None:
                return exits.FAILURE
            arguments.message_id = routed[1]
            return _read(routed[0], arguments)


IMPLEMENTED: Final[dict[str, Callable[[ProjectPaths, argparse.Namespace], int]]] = {
    "start": _start,
    "status": _status,
    "stop": _stop,
    "restart": _restart,
    "probe": _probe,
    "create": _create,
    "configure": _configure,
    "send": _send,
    "questions": _questions,
    "answer": _answer,
    "mail": _mail,
    "wait": _wait,
    "read": _read,
    "ack": _ack,
    "sent": _sent,
    "release": _release,
    "interrupt": _interrupt,
    "hibernate": _hibernate,
    "awaken": _awaken,
    "rebind": _rebind,
    "settings": _settings,
    "set": _set,
    "reload": _reload,
    "attach": _attach,
    "config": _config,
    "teacher": _teacher,
    "role": _role,
    "run": _run,
    "budget": _budget,
    "agent": _agent,
    "peer": _peer,
    "mcp": _mcp,
    "proposal": _proposal,
    "land": _land,
    "fleet": lambda _paths, arguments: _fleet(arguments),
}


SignInCheck = Callable[[Driver], Coroutine[object, object, Credential]]
"""A sign-in check, which runs no turn and answers the credential the engine uses."""

SIGN_IN_CHECKS: Final[dict[str, SignInCheck]] = {}
"""Each engine's sign-in check, which runs no turn, for the engines whose table says
it has one. Row 16 of the T3 Code review. No check reads a credential store itself.
No table says so yet."""

UNVERIFIED_SIGN_IN_CHECKS: Final[dict[str, SignInCheck]] = {
    pi.ENGINE_NAME: pi.check_sign_in,
}
"""Sign-in checks written from an engine's documentation, for engines never run live,
whose table therefore says ``unknown``. **Review of 2026-10-27, C1.** Pi's table once
said ``yes`` without a live run. Its check runs only with
``--accept-unverified-check``, by the operator's decision."""


def _sign_in(driver: Driver, unverified: bool = False) -> int:
    """Check the engine's sign-in without a turn, where the engine offers a check, as
    ``dromon probe --no-turn`` does. With ``unverified``, a check its table cannot
    vouch for runs too, and the report says so."""
    table = capabilities.table_of(driver.settings.engine)
    check = SIGN_IN_CHECKS.get(table.engine)
    if table.sign_in_check is not capabilities.Known.YES:
        check = None
    if check is None and unverified:
        check = UNVERIFIED_SIGN_IN_CHECKS.get(table.engine)
    if check is None:
        hint = (
            ". Its check was never run live, and runs with --accept-unverified-check"
            if table.engine in UNVERIFIED_SIGN_IN_CHECKS
            else ""
        )
        print(
            f"dromon probe: {table.display} offers no sign-in check that Dromon knows,"
            f" its table says {table.sign_in_check}{hint}, so nothing was checked and"
            " no turn was run. dromon probe without --no-turn checks the sign-in with"
            " one short turn",
            file=sys.stderr,
        )
        return exits.FAILURE
    try:
        credential = asyncio.run(check(driver))
    except (DriverError, EngineRefused) as error:
        print(f"dromon probe: sign-in check failed: {error}", file=sys.stderr)
        return exits.FAILURE
    settings = driver.settings
    print(
        f"engine {settings.engine}, signed in with a {credential} credential, which"
        f" the {settings.strategy} driver accepts. No turn was run"
    )
    if table.sign_in_check is not capabilities.Known.YES:
        print(
            f"The check was written from {table.display}'s documentation and was"
            " never run live, so its answer is unverified"
        )
    return exits.OK


def _scrub(paths: ProjectPaths, source: Path, destination: Path | None) -> int:
    """Scrub the recording at ``source`` into a file that may become a fixture, as
    ``dromon.evidence.scrub_file`` says, without running a turn. A relative ``source``
    is read from the state directory's probes, where ``--record`` writes, and the copy
    goes by default to the project's ``tmp/probes``, where lines can read it."""
    if not source.is_absolute():
        source = paths.state_dir / evidence.PROBES_DIRECTORY / source
    target = destination or (
        paths.root
        / evidence.FIXTURES_DIRECTORY
        / (source.name.removesuffix(".jsonl") + ".scrubbed.jsonl")
    )
    try:
        written = evidence.scrub_file(source, target)
    except evidence.RecordingError as error:
        print(f"dromon probe: {error}", file=sys.stderr)
        return exits.FAILURE
    except OSError as error:
        print(
            f"dromon probe: cannot write the scrubbed recording: {error}",
            file=sys.stderr,
        )
        return exits.FAILURE
    print(f"dromon probe: scrubbed recording written to {written}")
    print(
        "dromon probe: the check is by pattern, so read the file before committing it"
    )
    return exits.OK


def _add_driver_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--model", choices=MODELS, default=PROBE_MODEL, help="default %(default)s"
    )
    command.add_argument(
        "--strategy",
        choices=[strategy.value for strategy in Strategy],
        default=Strategy.SUBSCRIPTION.value,
        help="the driver's credential strategy, default %(default)s",
    )


def _add_tool_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--tool", action="append", help="a built-in tool such as Read, repeatable"
    )
    command.add_argument(
        "--mcp-config",
        type=Path,
        default=None,
        help="a JSON file of MCP servers, in the mcpServers shape",
    )
    command.add_argument(
        "--sandbox-config",
        type=Path,
        default=None,
        help="a JSON file of Claude Code sandbox settings for the line's commands",
    )
    command.add_argument(
        "--settings-config",
        type=Path,
        default=None,
        help="a Claude Code settings file of permissions, hooks, env and attribution",
    )
    command.add_argument(
        "--instructions",
        action="append",
        help="a file appended to the system prompt, relative to the line's working"
        " directory, repeatable",
    )
    command.add_argument(
        "--claude-code-prompt",
        action="store_true",
        help="run on Claude Code's own system prompt instead of an empty one",
    )
    command.add_argument(
        "--workdir",
        default=None,
        help="the directory the line's engine runs in, relative to the project root,"
        " default the project root",
    )
    command.add_argument("--workdir-access", choices=("read", "write"))
    command.add_argument(
        "--keep-session", action=argparse.BooleanOptionalAction, default=None
    )
    command.add_argument(
        "--allow-compact", action=argparse.BooleanOptionalAction, default=None
    )
    for option in (
        "keep-session-idle-seconds",
        "keep-session-max-seconds",
        "keep-session-hard-seconds",
        "auto-compact-tokens",
    ):
        command.add_argument("--" + option, type=int)
    command.add_argument("--keep-session-at-max", choices=("close", "keep", "ask"))
    command.add_argument(
        "--effort",
        choices=EFFORTS,
        default=None,
        help="the reasoning effort, instead of the one the driver declares",
    )
    command.add_argument(
        "--allow-tool",
        action="append",
        help="an allowed tool name or mcp__server__*, repeatable",
    )
    command.add_argument(
        "--allow-all-tools",
        action="store_true",
        help="allow every tool the engine offers, instead of an allowlist",
    )
    command.add_argument(
        "--inherit-account-mcp",
        action="store_true",
        help="also load the account's MCP servers and claude.ai connectors",
    )


FLEET_COMMANDS: Final[dict[str, str]] = {
    "list": "each daemon with its state, commit, lines, questions, mail and issues",
    "questions": "the open questions of every daemon, each named PROJECT:QUESTION",
    "mail": "an operator's mail in every daemon, each named PROJECT:MESSAGE",
    "issues": "the open issues lines raised in every daemon",
    "answer": "answer PROJECT:QUESTION",
    "send": "send to PROJECT:LINE, or PROJECT:@OPERATOR",
    "read": "open PROJECT:MESSAGE",
    "status": "show one project's daemon, as dromon status does",
    "restart": "restart one project's daemon, as dromon restart does",
    "hosts": "list the hosts of other machines in the per-user hosts file",
}


def _add_fleet_commands(command: argparse.ArgumentParser) -> None:
    """The subcommands of ``dromon fleet``, each taking the options of the command it
    sends for one project."""
    actions = command.add_subparsers(
        dest="fleet_action", metavar="ACTION", required=True
    )
    for action, help_text in FLEET_COMMANDS.items():
        sub = actions.add_parser(action, help=help_text, description=help_text)
        if action != "hosts":
            sub.add_argument(
                "--operator",
                default=default_operator(),
                help="the operator, default %(default)s",
            )
        if action in ("status", "restart"):
            sub.add_argument(
                "project_name",
                metavar="PROJECT",
                help="the project, by its name or its root",
            )
        if action in ("answer", "send", "read"):
            what = {"answer": "QUESTION", "send": "LINE", "read": "MESSAGE"}[action]
            sub.add_argument(
                "target", metavar=f"PROJECT:{what}", help="the project and the item"
            )
        if action in ("answer", "send"):
            sub.add_argument("body", help="the text")
        if action == "questions":
            sub.add_argument(
                "--all", action="store_true", help="list every operator's questions"
            )
        if action in ("mail", "issues"):
            sub.add_argument(
                "--limit",
                type=int,
                default=20,
                help="at most this many from each daemon, default 20",
            )
        if action == "mail":
            sub.add_argument("--unread", action="store_true", help="unread only")
        if action == "send":
            sub.add_argument("--reply-to", default=None, help="the message answered")
            sub.add_argument(
                "--priority",
                action="store_true",
                help="end the line's running turn at a safe point and lead its next",
            )
        if action == "status":
            sub.add_argument(
                "--correlation",
                metavar="KEY",
                help="also total the turns and cost recorded under this key",
            )
        if action == "restart":
            sub.add_argument("--interrupt", action="store_true")
            sub.add_argument(
                "--wait-seconds",
                type=int,
                default=restart.DEFAULT_WAIT_SECONDS,
                metavar="SECONDS",
            )
            sub.add_argument("--end-tasks", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromon``."""
    parser = argparse.ArgumentParser(
        prog="dromon",
        description="Client for the Dromon multi-agent harness daemon.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--project", type=Path, default=Path(), help="project root, default ."
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="directory for the socket and lock, default .dromon/run",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text in SUBCOMMANDS.items():
        command = commands.add_parser(name, help=help_text, description=help_text)
        if name == "fleet":
            _add_fleet_commands(command)
        if name == "rebind":
            command.add_argument("line_id", help="the line to move")
            command.add_argument(
                "--model", required=True, choices=MODELS, help="the new model"
            )
            command.add_argument(
                "--strategy",
                required=True,
                choices=[strategy.value for strategy in Strategy],
                help="the new driver's credential strategy",
            )
        if name == "status":
            command.add_argument(
                "--correlation",
                metavar="KEY",
                help="also total the turns and cost recorded under this key",
            )
        if name == "release":
            command.add_argument("line_id", help="the held line")
        if name == "interrupt":
            command.add_argument("line_id", help="the line whose turn to stop")
        if name == "hibernate":
            command.add_argument("line_id", help="the line to hibernate")
        if name == "restart":
            command.add_argument(
                "--interrupt",
                action="store_true",
                help="ask running turns to end at their next safe point, rather than"
                " waiting for them to end by themselves",
            )
            command.add_argument(
                "--wait-seconds",
                type=int,
                default=restart.DEFAULT_WAIT_SECONDS,
                metavar="SECONDS",
                help="how long running turns may take to end before the restart is"
                " abandoned, in whole seconds, default %(default)s",
            )
            command.add_argument(
                "--end-tasks",
                action="store_true",
                help="restart even though background tasks of kept engine sessions"
                " stop with them",
            )
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who asks for the restart, default %(default)s",
            )
        if name == "land":
            _land_arguments(command)
        if name == "awaken":
            command.add_argument("line_id", help="the hibernated line")
        if name == "start":
            command.add_argument(
                "--wait",
                type=float,
                default=STARTUP_TIMEOUT_SECONDS,
                metavar="SECONDS",
                help="how long to wait for the daemon to answer, default %(default)g",
            )
        if name == "settings":
            command.add_argument(
                "line_id", nargs="?", help="the line whose settings to show as well"
            )
        if name == "set":
            command.add_argument(
                "key",
                help="a line key, or a project key such as project.wake.every_ticks",
            )
            command.add_argument(
                "value", nargs="*", help="the value, as the client takes it"
            )
            command.add_argument(
                "--line", dest="line_id", help="the line, for a line key"
            )
            command.add_argument(
                "--unset",
                action="store_true",
                help="return the key to the layers below",
            )
        if name == "attach":
            command.add_argument("line_id", help="the line to attach to")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who the client speaks for, default %(default)s",
            )
            command.add_argument(
                "--gui", action="store_true", help="open a graphical window instead"
            )
            command.add_argument(
                "--poll-seconds",
                type=float,
                default=1.0,
                help="how often to refresh, default %(default)s",
            )
        if name in ("create", "configure"):
            command.add_argument("line_id", help="the line's identifier")
            _add_tool_options(command)
        if name == "create":
            command.add_argument("--max-autonomous-turns", type=int)
            command.add_argument("--max-cost-usd")
            command.add_argument("--cooldown-seconds")
            command.add_argument(
                "--session",
                default=None,
                help="an existing engine session to continue, started in the line's"
                " working directory",
            )
        if name in ("create", "probe"):
            _add_driver_options(command)
        if name == "probe":
            command.add_argument(
                "--record",
                action="store_true",
                help="record every frame the adapter exchanged with the engine to a"
                " new file under .dromon/probes, readable by its owner alone and"
                " closed to every line, which --scrub copies for a test to replay"
                " through the adapter. Prompts and replies are recorded as they were"
                " exchanged. Every engine records",
            )
            command.add_argument(
                "--no-turn",
                action="store_true",
                help="run no turn, and check only the engine's sign-in, where the"
                " engine's table says it offers a check. No table says so yet."
                " Dromon reads no credential store",
            )
            command.add_argument(
                "--accept-unverified-check",
                action="store_true",
                help="with --no-turn, run a sign-in check written from an engine's"
                " documentation but never run live, so its table says unknown. Pi has"
                " one, pi auth check, which reads Pi's own credential store",
            )
            command.add_argument(
                "--scrub",
                type=Path,
                default=None,
                metavar="FILE",
                help="run no turn. Write a copy of the recording FILE with the account"
                " values, the home directory, session ids and uuids replaced by stable"
                " placeholders, which still replays, and refuse it if the home"
                " directory, the user name or an email address remains. A relative"
                " FILE is read from .dromon/probes",
            )
            command.add_argument(
                "--scrub-to",
                type=Path,
                default=None,
                metavar="PATH",
                help="with --scrub, where to write the copy, which must not exist."
                " Default tmp/probes and FILE's name with .scrubbed.jsonl in place of"
                " .jsonl",
            )
            command.add_argument(
                "--record-stderr",
                action="store_true",
                help="with --record, also keep the engine's standard error, at most"
                " 64 KiB, with credentials masked by pattern",
            )
            command.add_argument(
                "--accept-partial-isolation",
                action="store_true",
                help="let a Cursor Agent or Antigravity model run this one turn,"
                " with partial isolation. Cursor's built-in reads, writes, commands"
                " and web fetches are denied before they run, but its subagents,"
                " searches, image and screen tools are stopped only after they start."
                " Antigravity's tools are refused before they run when its ACP runtime"
                " asks first, and stopped only after they start when it does not."
                " Either way such a tool may act before the turn is refused. Neither"
                " this flag nor --accept-unverified-credential changes the strategy, so"
                " --strategy api still bills per token",
            )
            command.add_argument(
                "--accept-unverified-credential",
                action="store_true",
                help="let an Antigravity model run this one turn although Dromon"
                " cannot tell which credential the Antigravity ACP runtime uses, a"
                " sign-in to a plan or a key billed per token. First confirm by hand"
                " that the runtime is signed in to a plan and not billing per token,"
                " step 3 of the verify-first list in docs/protocol/ANTIGRAVITY.md."
                " Accepted only together with --accept-partial-isolation. It does not"
                " change the strategy",
            )
        if name == "agent":
            command.add_argument("action", choices=["add", "list", "revoke", "send"])
            command.add_argument(
                "name",
                nargs="?",
                metavar="NAME_OR_BODY",
                help="the agent, for add and revoke, or the note's body, for send",
            )
            command.add_argument(
                "--agent",
                help="for send, the agent that sends. Its token is read from"
                " DROMON_AGENT_TOKEN",
            )
            command.add_argument(
                "--to", help="for send, the party the note is for, such as a line"
            )
            command.add_argument(
                "--correlation",
                metavar="KEY",
                help="for send, a key naming the agent's task, under which the"
                " line's turns and their cost are totalled",
            )
            command.add_argument(
                "--access",
                choices=[level.value for level in AgentAccess],
                default=AgentAccess.MESSAGE.value,
                help="what the agent may do, for add, default %(default)s",
            )
        if name == "peer":
            command.add_argument(
                "action", choices=["list", "token", "revoke", "store", "key"]
            )
            command.add_argument(
                "name",
                nargs="?",
                help="the peer, for token, revoke and store. store reads the token"
                " from standard input. key shows this daemon's own pin",
            )
        if name == "role":
            command.add_argument(
                "action",
                choices=[
                    "status",
                    "entry",
                    "take",
                    "vacate",
                    "handoff",
                    "create",
                    "update",
                    "delete",
                ],
                nargs="?",
                default="status",
                help="default %(default)s",
            )
            command.add_argument(
                "name",
                nargs="?",
                help="the role, which status may omit for every role, or for entry"
                " the journal entry",
            )
            command.add_argument("--seat", help="for take, the seat given the role")
            command.add_argument(
                "--workdir", help="for create and update, the role's worktree"
            )
            command.add_argument(
                "--seats",
                help="for create and update, the seats in failover order, separated"
                " by commas, each a line that exists. For update it replaces the"
                " seats, so it adds, removes and reorders them",
            )
            command.add_argument(
                "--no-own",
                action="append",
                default=None,
                help="for create and update, a seat that may not own the role,"
                " repeatable",
            )
            command.add_argument(
                "--may-own",
                action="append",
                default=None,
                help="for update, a seat that may own the role again, repeatable",
            )
            command.add_argument(
                "--handoff-seconds",
                type=int,
                default=None,
                help="for create and update, how long a planned handoff may take",
            )
            command.add_argument(
                "--failover",
                choices=["automatic", "ask"],
                default=None,
                help="for create and update, the failover policy",
            )
            command.add_argument(
                "--handoff-below-percent",
                type=int,
                default=None,
                help="for create and update, the stated budget below which the owner"
                " is asked to prepare a handoff, 0 for never",
            )
            command.add_argument(
                "--to", help="for handoff, the seat the owner is asked to hand it to"
            )
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who makes the change or the request, default %(default)s",
            )
        if name == "run":
            command.add_argument("action", choices=["clear"])
            command.add_argument("run_id", help="the lost run to clear")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who clears it, default %(default)s",
            )
        if name == "budget":
            command.add_argument(
                "action",
                choices=["list", "set"],
                nargs="?",
                default="list",
                help="default %(default)s",
            )
            command.add_argument("provider", nargs="?", help="for set, the provider")
            command.add_argument(
                "--percent",
                type=int,
                default=None,
                help="for set, the whole percentage of the plan that remains",
            )
            command.add_argument(
                "--resets-at",
                default=None,
                help="for set, when the plan resets, as YYYY-MM-DDTHH:MM:SSZ",
            )
            command.add_argument(
                "--note", default="", help="for set, a note kept with the statement"
            )
        if name == "proposal":
            command.add_argument("action", choices=["list", "show"])
            command.add_argument(
                "proposal_id", nargs="?", help="the proposal identifier, for show"
            )
        if name == "mcp":
            party = command.add_mutually_exclusive_group(required=True)
            party.add_argument(
                "--agent",
                help="the agent's name. Its token is read from DROMON_AGENT_TOKEN",
            )
            party.add_argument(
                "--line",
                help="the line whose own tools to serve, for its engine. Its token is"
                " read from DROMON_LINE_TOKEN",
            )
        if name == "teacher":
            command.add_argument(
                "action",
                choices=["up", "status", "down"],
                nargs="?",
                default="status",
                help="default %(default)s",
            )
            command.add_argument(
                "--force",
                action="store_true",
                help="load the model even when too little memory is available",
            )
        if name == "questions":
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="whose questions to list, default %(default)s",
            )
            command.add_argument(
                "--all", action="store_true", help="list every operator's questions"
            )
        if name == "answer":
            command.add_argument("question_id", help="the question answered")
            command.add_argument("body", help="the answer")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="the operator answering, default %(default)s",
            )
        if name in ("mail", "read", "ack", "sent", "wait"):
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="whose mailbox, default %(default)s",
            )
        if name == "wait":
            command.add_argument(
                "--replies-to", required=True, metavar="MESSAGE", help="the message"
            )
            command.add_argument(
                "--from",
                dest="sender",
                default=None,
                metavar="LINE",
                help="wait only for a reply from this line",
            )
            command.add_argument(
                "--timeout",
                type=float,
                default=300.0,
                metavar="SECONDS",
                help="maximum wait, default 300 seconds",
            )
        if name == "mail":
            command.add_argument("--unread", action="store_true", help="unread only")
            command.add_argument(
                "--issues",
                action="store_true",
                help="open issues lines raised, closed by a reply or dromon ack",
            )
            command.add_argument(
                "--from", dest="sender", default=None, help="one sender"
            )
            command.add_argument(
                "--replies-to",
                default=None,
                metavar="MESSAGE",
                help="the replies to one message, with their bodies, marking none read",
            )
            command.add_argument(
                "--offset", type=int, default=0, help="matches to skip"
            )
            command.add_argument(
                "--limit", type=int, default=20, help="at most this many, default 20"
            )
        if name in ("read", "ack"):
            command.add_argument("message_id", help="the message")
        if name == "sent":
            command.add_argument(
                "--unanswered", action="store_true", help="only mail awaiting an answer"
            )
            command.add_argument(
                "--offset", type=int, default=0, help="messages to skip"
            )
            command.add_argument(
                "--limit", type=int, default=20, help="at most this many, default 20"
            )
        if name == "send":
            command.add_argument("recipient", help="a line, or an operator as @name")
            command.add_argument("body", help="the message text")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="the operator sending it, default %(default)s",
            )
            command.add_argument(
                "--reply-to", default=None, help="the message answered"
            )
            command.add_argument(
                "--priority",
                action="store_true",
                help="end the line's running turn at a safe point and lead its next",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromon`` and return its exit status."""
    arguments = build_parser().parse_args(argv)
    command: str = arguments.command
    handler = IMPLEMENTED.get(command)
    if handler is None:
        print(f"dromon {command}: not implemented in this version", file=sys.stderr)
        return exits.NOT_IMPLEMENTED
    if command == "fleet":
        # The fleet needs no project of its own, so a client run outside one works.
        return _fleet(arguments)
    project: Path = arguments.project
    runtime_dir: Path | None = arguments.runtime_dir
    try:
        paths = project_paths(project, runtime_dir)
    except PathError as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    return handler(paths, arguments)


if __name__ == "__main__":
    sys.exit(main())
