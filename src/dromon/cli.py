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
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import dromon
from dromon import __version__, exits, restart
from dromon import proposals as proposal_store
from dromon.client import (
    SHUTDOWN_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    DaemonFailed,
    DaemonNotRunning,
    call,
    is_running,
    start,
    stop,
)
from dromon.paths import PathError, ProjectPaths, ensure_private_dir, project_paths
from dromon.driver import MODELS, Credential, Driver, DriverError, Strategy
from dromon import antigravity, capabilities, codex, cursor, evidence, grok, pi
from dromon.engine import ENGINE_NAME as CLAUDE_CODE_ENGINE
from dromon.engine import EngineRefused
from dromon.agents import LINE_TOKEN_ENV, TOKEN_ENV
from dromon.lineconfig import EFFORTS
from dromon.model import AgentAccess
from dromon.protocol import Operation, ProtocolError
from dromon.scheduler import LineSpec, engine_factory
from dromon import settings as engine_settings
from dromon.sandbox import SandboxConfig, merge, sandbox_from_settings
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
    "role": "show roles and their journals, or give, vacate or ask to hand over one",
    "budget": "list the providers' stated budgets, or state one",
    "reload": "read the project file again and apply its lines, limits and wake policy",
    "config": "check the project file and show what it declares",
    "probe": "run one short turn on the engine and report its capabilities",
    "teacher": "bring the project's local teacher model up, report it, or release it",
    "agent": "register, list or revoke the external agents that may connect",
    "peer": "list the declared peers, or issue, revoke or store their tokens",
    "mcp": "serve an external agent over MCP on standard input and output",
    "proposal": "list or show proposed changes from session lines",
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
    del arguments
    try:
        response = call(paths.socket, Operation.STATUS)
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
    return exits.OK


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


PREFLIGHT_ARGV: Final = (sys.executable, "-m", "dromon.daemon", "--version")
"""What ``dromon restart`` runs before it asks for the restart, which imports the
installed daemon and every module it needs, and starts nothing."""

PREFLIGHT_TIMEOUT_SECONDS: Final = 60.0

RESTART_POLL_SECONDS: Final = 0.25
"""How often ``dromon restart`` asks whether the successor answers."""


def _preflight(argv: Sequence[str] = PREFLIGHT_ARGV) -> str | None:
    """Why the installed daemon would not start, or ``None``. A restart whose
    successor cannot import its code leaves no daemon running, so this is checked
    before the old daemon is asked to stop."""
    try:
        done = subprocess.run(  # noqa: S603
            list(argv),
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    if done.returncode != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        return f"status {done.returncode}: {lines[-1] if lines else 'no output'}"
    return None


def _installed_commit() -> str | None:
    """The commit of the code this client, and so a successor, would run."""
    return read_head_commit(Path(dromon.__file__).parent)


def _restart(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Ask the daemon to restart, then wait until its successor answers and say
    which commit it runs. The daemon starts its successor itself, so a client that
    leaves early does not stop the restart."""
    problem = _preflight()
    if problem is not None:
        print(
            "dromon restart: the installed daemon does not start, so the running one"
            f" was not stopped: {problem}",
            file=sys.stderr,
        )
        return exits.FAILURE
    before = _call(paths, "restart", Operation.STATUS, {})
    if isinstance(before, int):
        return before
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
    answer = _call(paths, "restart", Operation.RESTART, args)
    if isinstance(answer, int):
        return answer
    warning = answer.get("warning")
    if isinstance(warning, str):
        print(f"dromon restart: {warning}")
    running = answer.get("running")
    count = len(cast(list[object], running)) if isinstance(running, list) else 0
    print(
        f"dromon restart: draining, with {count} turns running, for at most"
        f" {wait} seconds. The installed code is at commit"
        f" {_installed_commit() or 'unknown'}"
    )
    return _await_successor(paths, before, wait)


def _await_successor(
    paths: ProjectPaths, before: Mapping[str, object], wait: float
) -> int:
    """Poll until a daemon with another process id answers, or the restart is
    abandoned, or the bound passes. Bounded by its count of polls."""
    bound = wait + SHUTDOWN_TIMEOUT_SECONDS + STARTUP_TIMEOUT_SECONDS
    polls = math.ceil(bound / max(RESTART_POLL_SECONDS, 0.01)) + 1
    for _ in range(polls):
        try:
            response = call(paths.socket, Operation.STATUS)
        except (DaemonNotRunning, ProtocolError, TimeoutError, OSError):
            time.sleep(RESTART_POLL_SECONDS)
            continue
        result = response.result or {}
        if response.ok and result.get("pid") != before.get("pid"):
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
    print(
        f"dromon restart: no restarted daemon answered within {bound:g} seconds."
        f" See {paths.daemon_output}",
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
    if not any(given) and effort is None and workdir is None:
        return None
    document: dict[str, object] = {
        "builtin_tools": tools,
        "allowed_tools": allowed,
        "tool_policy": "allow_all" if allow_all else "allowlist",
        "inherit_account_mcp": inherit,
    }
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
    answer = _call(paths, "send", Operation.SEND, args)
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
    args: dict[str, object] = {
        "operator": arguments.operator,
        "unread_only": arguments.unread,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    if arguments.sender is not None:
        args["sender"] = arguments.sender
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
    from dromon.ui.link import SocketDaemon

    if not arguments.poll_seconds > 0:
        print("dromon attach: --poll-seconds must be positive", file=sys.stderr)
        return exits.USAGE
    daemon = SocketDaemon(paths)
    if arguments.gui:
        from dromon.ui import window

        return window.run(
            daemon, arguments.line_id, arguments.operator, arguments.poll_seconds
        )
    from dromon.ui import terminal

    return terminal.run(
        daemon, arguments.line_id, arguments.operator, arguments.poll_seconds
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
    limits = config.limits
    print(f"{file} is valid")
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
    for line_id, line in sorted(config.lines.items()):
        line_settings = engine_settings.merge([config.settings, line.config.settings])
        tools = ", ".join(line.config.builtin_tools) or "no built-in tools"
        servers = ", ".join(line.config.server_names()) or "no MCP servers"
        print(
            f"line {line_id}: {line.model} {line.strategy},"
            f" {line.config.tool_policy}, {tools}, {servers},"
            f" {_sandbox_summary(merge([config.sandbox, line.config.sandbox]))},"
            f" {_settings_summary(line_settings)}"
            + (f", effort {line.config.effort}" if line.config.effort else "")
            + (f", in {line.config.workdir}" if line.config.workdir else "")
        )
    shared_workdirs: dict[str, list[str]] = {}
    for line_id, line in sorted(config.lines.items()):
        tools_available = set(line.config.builtin_tools) | set(
            line.config.allowed_tools
        )
        has_write_tool = line.config.tool_policy == "allow_all" or bool(
            tools_available & {"Bash", "Write", "Edit", "NotebookEdit"}
        )
        # MCP servers can expose arbitrary tools, so treat any configured server as
        # potentially capable of writing.
        if not has_write_tool and not line.config.mcp_servers:
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


MAX_PEER_TOKEN_INPUT: Final = 1024
"""The most characters ``dromon peer store`` reads from standard input."""


def _peer(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """List the declared peers, or issue, revoke or store a peer's token, through the
    daemon. ``store`` reads the token from standard input, never from an argument, so
    it does not appear in the process list or the shell's history."""
    action: str = arguments.action
    name: str | None = arguments.name
    if action != "list" and name is None:
        print(f"dromon peer {action}: name the peer", file=sys.stderr)
        return exits.USAGE
    args: dict[str, object] = {} if name is None else {"peer": name}
    operation = {
        "list": Operation.PEER_LIST,
        "token": Operation.PEER_TOKEN,
        "revoke": Operation.PEER_REVOKE,
        "store": Operation.PEER_STORE,
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
    del arguments
    return _request(paths, "reload", Operation.RELOAD_PROJECT, {})


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


def _role(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Show roles, read a journal entry, or change or ask for a change of owner."""
    action: str = arguments.action
    name: str | None = arguments.name
    command = f"role {action}"
    operator: str = arguments.operator
    if action != "status" and name is None:
        what = "the journal entry" if action == "entry" else "the role"
        print(f"dromon {command}: name {what}", file=sys.stderr)
        return exits.USAGE
    needed = {"take": "seat", "handoff": "to"}.get(action)
    if needed is not None and getattr(arguments, needed) is None:
        print(f"dromon {command}: give --{needed}", file=sys.stderr)
        return exits.USAGE
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
            if not roles:
                print("no roles are declared")
            for role in roles:
                _print_role(role)
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


def _print_role(role: Mapping[str, object]) -> None:
    """One role of ``roles``, with its seats and the latest journal entries."""
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
    paths: ProjectPaths, command: str, operation: Operation, args: dict[str, object]
) -> dict[str, object] | int:
    """The result of one request, or the exit status that reports its failure."""
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
    "budget": _budget,
    "agent": _agent,
    "peer": _peer,
    "mcp": _mcp,
    "proposal": _proposal,
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
            command.add_argument("action", choices=["add", "list", "revoke"])
            command.add_argument(
                "name", nargs="?", help="the agent, for add and revoke"
            )
            command.add_argument(
                "--access",
                choices=[level.value for level in AgentAccess],
                default=AgentAccess.MESSAGE.value,
                help="what the agent may do, for add, default %(default)s",
            )
        if name == "peer":
            command.add_argument("action", choices=["list", "token", "revoke", "store"])
            command.add_argument(
                "name",
                nargs="?",
                help="the peer, for token, revoke and store. store reads the token"
                " from standard input",
            )
        if name == "role":
            command.add_argument(
                "action",
                choices=["status", "entry", "take", "vacate", "handoff"],
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
                "--to", help="for handoff, the seat the owner is asked to hand it to"
            )
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who makes the change or the request, default %(default)s",
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
        if name in ("mail", "read", "ack", "sent"):
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="whose mailbox, default %(default)s",
            )
        if name == "mail":
            command.add_argument("--unread", action="store_true", help="unread only")
            command.add_argument(
                "--from", dest="sender", default=None, help="one sender"
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
