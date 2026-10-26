"""The Antigravity engine adapter, which runs a turn through ``agy --print``.

**Unverified, and the tool is not installed.** Written on 2026-10-26 from the
operator's summary of Antigravity's headless documentation alone, since ``agy`` is not
installed on the operator's machine. ``docs/protocol/ANTIGRAVITY.md`` records what that
summary says, what is inferred, and what to check first.

Dromon would drive Antigravity from outside. Each turn starts ``agy`` in print mode with
``--output-format stream-json``, reads its ``init``, ``step_update`` and ``result``
events one per line, and feeds them to the line's driver. Print mode takes no command
after it starts, so an interrupt ends the process.

Isolation. The documentation does not say which built-in tools run without approval,
nor how to give Antigravity an MCP server, so Dromon can neither keep a line's reads
away from its state and the operator's credentials, nor give the line Dromon's own
tools. **The operator decided on 2026-10-26** that a daemon refuses Antigravity lines,
as Cursor Agent's, and that ``dromon probe --accept-partial-isolation``, which sets
``partial_isolation``, may run one turn, in which any tool stops and refuses the turn
after it starts. A line given Dromon's tools is refused in any case, since they could
not reach it.

Credentials. Antigravity signs in by itself and caches the sign-in, which Dromon never
reads. Its stream names no credential source, so a subscription line's Antigravity
starts without any Google, Gemini or cloud credential variable. **Antigravity review,
F2.** The credential is recorded as ``unverified``, not taken to be the subscription,
and is put to the driver before Antigravity starts. No packaged driver accepts it, so
every turn is refused before it can bill. **The operator decided on 2026-10-26** that
``dromon probe --accept-unverified-credential``, given only with
``--accept-partial-isolation``, which sets ``unverified_credential``, puts the
subscription to the driver instead, on the operator's confirmation by hand that the
sign-in is a plan. The turn's report still records ``unverified``, since Dromon saw
no credential. The API strategy is refused as before.

Porting note. JSON lines over a pipe, sequential, as the other adapters.
"""

import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import SdkMcpTool

from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy, harness_id
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon.jsonlines import (
    TURN_SECONDS,
    Connection,
    ConnectionFactory,
    is_cloud_or_credential,
    json_lines_factory,
)
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig
from dromon.processes import MARK_VARIABLE

ENGINE_NAME: Final = "antigravity"
"""The engine this adapter runs, as drivers name it."""

COMMAND: Final = "agy"

CREDENTIAL_PREFIXES: Final = ("GEMINI_",)
"""Variables a Gemini tool may read a key from, besides the cloud variables
``is_cloud_or_credential`` names, which include Google's. A line's Antigravity starts
without any of them."""

SETTINGS: Final = Path(".gemini") / "antigravity-cli" / "settings.json"
"""The user's settings, relative to the home directory, whose ``permissions.allow``
would approve tools Dromon did not give."""

GEMINI_SETTINGS: Final = Path(".gemini") / "settings.json"
"""**Re-review, N2.** The Gemini tools' own user settings, which Antigravity might also
read, so they are checked as ``SETTINGS`` is. Whether it reads them is unverified."""

SETTINGS_FILES: Final = (SETTINGS, GEMINI_SETTINGS)

KEY_FILE: Final = Path(".gemini") / ".env"
"""**Re-review, N2.** A file from which a Gemini tool may read a key that the
environment filter cannot see. Its existence alone refuses the turn, since such a key
would bill per token while the line is taken to be on a plan. Dromon never reads it."""

WORKSPACE_DIRECTORIES: Final = (".gemini", ".agy", ".antigravity")
"""**Antigravity review, F3.** Directories in which a project may hold settings,
permissions or MCP servers for Antigravity or the Gemini tools it descends from. The
documentation does not say which Antigravity reads, so any of them in the working
directory or a directory above it, other than the home directory, refuses the turn."""

STEP_TYPES: Final = frozenset({"user_input", "agent_response", "checkpoint"})
"""**Antigravity review, F1.** The step types a turn may carry without a tool. A
``tool`` step, and any type not named here, refuses the turn."""

TOOL_FIELDS: Final = ("tool_name", "tool_info", "subagent_info")
"""Step fields that report a tool or a subagent. Any of them refuses the turn."""

RECORD_TYPES: Final = frozenset({"init", "step_update", "result"})
"""The record types the stream is documented to carry. Any other refuses the turn."""

MAX_SETTINGS_BYTES: Final = 1024 * 1024

EFFORTS: Final[dict[str, str]] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
"""Dromon's efforts as Antigravity's, which stop at ``high``."""

PRINT_TIMEOUT: Final = f"{TURN_SECONDS // 60}m"
"""Antigravity ends a print run after five minutes by default, too short for a line's
turn, so it is given the turn's deadline, which Dromon also enforces itself."""

ASK_MODES: Final = frozenset({"default"})
"""Permission modes in which a tool that needs approval is denied in print mode."""

STATUSES: Final[dict[str, str]] = {
    "SUCCESS": "success",
    "ERROR": "error",
    "CANCELED": "interrupted",
    "INTERRUPTED": "interrupted",
    "INVALID": "error",
}
"""Antigravity's final statuses as Dromon's turn subtypes."""

CONVERSATION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ABORT_SECONDS: Final = 60

ISOLATION_REFUSAL: Final = (
    "Antigravity runs only in dromon probe with --accept-partial-isolation."
    " Antigravity's documentation does not say which built-in tools run without"
    " approval, so Dromon cannot show that a line's reads stay away from its state and"
    " the operator's credentials, and could only refuse a turn after a tool started"
)

TOOLS_REFUSAL: Final = (
    "Antigravity lines cannot take Dromon's own tools, because Antigravity's"
    " documentation does not say how to give it an MCP server"
)


def unsupported(config: LineConfig) -> str | None:
    """Why an Antigravity line cannot run ``config``, or ``None`` if it can."""
    if config.builtin_tools:
        return (
            "Antigravity lines take no built-in tools, because Dromon cannot show that"
            " they stay away from its state or the operator's credentials"
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Antigravity lines do not support MCP servers"
    if config.claude_code_prompt:
        return "Antigravity lines run on Antigravity's own prompt, not Claude Code's"
    if config.project_settings:
        return "Antigravity lines do not load Claude Code's project settings"
    if config.plugins:
        return "Antigravity lines do not load Claude Code plugins"
    if config.allow_compact:
        return "Antigravity lines cannot compact their session through Dromon"
    if config.keep_session:
        return "Antigravity lines do not keep their engine between turns"
    if config.sandbox is not None or config.settings is not None:
        return "Antigravity lines take no sandbox or settings document"
    return None


def line_refusal(config: LineConfig) -> str | None:
    """Why a daemon cannot hold an Antigravity line with ``config``. Every line a
    daemon holds has Dromon's own tools, which cannot reach Antigravity, so a line is
    refused when it is created. ``dromon probe``, which has no tools, is not."""
    return unsupported(config) or TOOLS_REFUSAL


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment Antigravity starts with, without any credential variable.
    Raises ``EngineRefused`` for any strategy but a subscription."""
    if strategy is not Strategy.SUBSCRIPTION:
        raise EngineRefused(
            "Antigravity lines support only the subscription strategy, because its"
            " stream names no credential, so Dromon could not tell a key from a"
            " sign-in"
        )
    return {
        k: v
        for k, v in environment.items()
        if not k.startswith(CREDENTIAL_PREFIXES)
        and not is_cloud_or_credential(k)
        and k != "DROMON_LINE_TOKEN"
    }


def check_workspace(workspace: Path, home: Path) -> None:
    """Refuse a workspace where Antigravity might load settings Dromon has not read, by
    the existence of ``WORKSPACE_DIRECTORIES`` alone."""
    directory = Path(os.path.realpath(workspace))
    own = Path(os.path.realpath(home))
    found = [
        str(candidate / name)
        for candidate in (directory, *directory.parents)
        if candidate != own
        for name in WORKSPACE_DIRECTORIES
        if os.path.lexists(candidate / name)
    ]
    if found:
        raise EngineRefused(
            "Antigravity might load settings Dromon has not read: " + ", ".join(found)
        )


def check_settings(home: Path) -> None:
    """Refuse when a key file exists in the user's Gemini directory, or when either
    user settings file approves any tool, which would give a line a tool Dromon did
    not give. A missing settings file approves nothing."""
    key = home / KEY_FILE
    if os.path.lexists(key):
        raise EngineRefused(
            f"{key} exists, from which agy might read a key billed per token that"
            " Dromon cannot see"
        )
    for relative in SETTINGS_FILES:
        _check_settings_file(home / relative)


def _check_settings_file(path: Path) -> None:
    """Refuse when the settings at ``path`` approve any tool."""
    if not os.path.lexists(path):
        return
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except OSError as error:
        raise EngineRefused(f"cannot read {path}: {error}") from error
    if len(raw) > MAX_SETTINGS_BYTES:
        raise EngineRefused(f"{path} exceeds {MAX_SETTINGS_BYTES} bytes")
    try:
        document: object = json.loads(raw)
    except ValueError as error:
        raise EngineRefused(f"{path} is not JSON") from error
    if not isinstance(document, dict):
        raise EngineRefused(f"{path} is not a JSON object")
    permissions = cast(dict[str, Any], document).get("permissions")
    if permissions is None:
        return
    if not isinstance(permissions, dict):
        raise EngineRefused(f"{path} has permissions that are not an object")
    allowed = cast(dict[str, Any], permissions).get("allow")
    if allowed:
        raise EngineRefused(
            f"{path} approves tools in permissions.allow, which would reach a line"
            " Dromon did not give them to"
        )


def agy_arguments(
    model: str, effort: str, *, conversation: str | None, prompt: str
) -> list[str]:
    """The ``agy`` arguments for one turn. The prompt is attached to its option, so a
    prompt that begins with a dash is not read as an option."""
    arguments = [
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--effort",
        EFFORTS[effort],
        "--sandbox",
        "--print-timeout",
        PRINT_TIMEOUT,
    ]
    if conversation is not None:
        arguments += ["--conversation", conversation]
    return [*arguments, f"--prompt={prompt}"]


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    started: bool = False
    result: dict[str, Any] | None = None
    stopped: bool = False


def _usage(value: object) -> dict[str, int] | None:
    """Antigravity's usage as it reports it, its whole-number fields only, or ``None``
    when it reports none. Its field names are not documented."""
    if not isinstance(value, dict):
        return None
    counts = {
        str(key): number
        for key, number in cast(dict[str, Any], value).items()
        if type(number) is int and number >= 0
    }
    return counts or None


ANTIGRAVITY_USAGE_NOTE: Final = (
    "the engine's own usage fields from its result, under its own names, which are not"
    " documented. It reports no cost"
)


class AntigravityLine:
    """One session line on the Antigravity engine, with its driver."""

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[Any]] = (),
        session_id: str | None = None,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
        connection_factory: ConnectionFactory | None = None,
        observe: Observer = ignore,
        mark: str | None = None,
        partial_isolation: bool = False,
        unverified_credential: bool = False,
        turn_seconds: float = TURN_SECONDS,
    ) -> None:
        if driver.settings.engine != ENGINE_NAME:
            raise EngineRefused(
                f"the driver for {driver.settings.model} is written for the"
                f" {driver.settings.engine} engine, not {ENGINE_NAME}"
            )
        self.driver = driver
        self.cwd = cwd
        self.config = config
        self.offered_tools = tuple(tool.name for tool in mcp_tools)
        """Dromon's tools for the line, which cannot reach Antigravity."""
        self.session_id = session_id
        self._environment = os.environ if environment is None else environment
        self._home = Path.home() if home is None else home
        self._connect = connection_factory or json_lines_factory(
            COMMAND, keep_input=False
        )
        self._observe = observe
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self.partial_isolation = partial_isolation
        """Whether the operator accepts that Antigravity's reads cannot be shown to be
        confined. Until then every turn is refused."""
        self.unverified_credential = unverified_credential
        """Whether the operator has confirmed by hand that ``agy`` is signed in to a
        plan and does not bill per token, so the driver is given the subscription in
        place of the unverified credential. Until then every turn is refused."""
        self.turn_seconds = turn_seconds
        """How long a turn may run before Antigravity is stopped and the turn
        refused."""
        self._connection: Connection | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        if not self.partial_isolation:
            raise EngineRefused(ISOLATION_REFUSAL)
        if self.offered_tools:
            raise EngineRefused(TOOLS_REFUSAL)
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        settings = self.driver.settings
        environment = engine_environment(self._environment, settings.strategy)
        check_workspace(self.cwd, self._home)
        check_settings(self._home)
        effort = self.config.effort or settings.effort
        if effort not in EFFORTS:
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        if self.session_id is not None and not CONVERSATION_PATTERN.fullmatch(
            self.session_id
        ):
            raise EngineRefused(
                f"the line's Antigravity conversation {self.session_id!r} is invalid"
            )
        credential = (
            Credential.SUBSCRIPTION
            if self.unverified_credential
            else Credential.UNVERIFIED
        )
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is ActionTag.REFUSE:
            if credential is Credential.UNVERIFIED:
                raise EngineRefused(
                    f"the {verdict.text} driver refuses the unverified credential."
                    " Antigravity's stream names no credential source, so Dromon"
                    " cannot tell its sign-in from a key before it bills. dromon"
                    " probe --accept-unverified-credential runs one turn once the"
                    " operator has confirmed by hand that agy is signed in to a plan"
                )
            raise EngineRefused(
                f"the {verdict.text} driver refuses the subscription the operator"
                " confirmed"
            )
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        text = prompt
        if self.session_id is None:
            instructions = read_instructions(self.config.instructions, self.cwd)
            text = f"{instructions}\n\n{prompt}" if instructions else prompt
        arguments = agy_arguments(
            harness_id(settings.model),
            effort,
            conversation=self.session_id,
            prompt=text,
        )
        turn = _Turn()
        async with self._connect(arguments, self.cwd, environment) as connection:
            self._connection = connection
            try:
                await self._converse(turn)
            finally:
                self._connection = None
        return self._report(turn)

    def _report(self, turn: _Turn) -> TurnReport:
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if not turn.started:
            raise EngineRefused("the engine ended before its init event")
        if turn.interrupted_for is not None:
            subtype = "interrupted"
        elif turn.result is None:
            raise EngineRefused("the engine ended without a result")
        else:
            status = str(turn.result.get("status") or "")
            subtype = STATUSES.get(status, "error")
            conversation = turn.result.get("conversation_id")
            if isinstance(conversation, str) and CONVERSATION_PATTERN.fullmatch(
                conversation
            ):
                if self.session_id not in (None, conversation):
                    raise EngineRefused(
                        f"the engine continued {conversation!r}, not {self.session_id}"
                    )
                self.session_id = conversation
        self.driver.step(Event("result", subtype, 1))
        usage = None if turn.result is None else _usage(turn.result.get("usage"))
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=Credential.UNVERIFIED,
            session_id=self.session_id or "",
            capabilities=None,
            cost_estimate_usd=None,
            usage=usage,
            usage_note=ANTIGRAVITY_USAGE_NOTE if usage is not None else None,
        )

    # The stream.

    def _open(self) -> Connection:
        if self._connection is None:
            raise EngineRefused("the engine connection is not open")
        return self._connection

    async def _converse(self, turn: _Turn) -> None:
        """Read the stream until its result, its end, or the turn's deadline, after
        which Antigravity is stopped and the stream read for at most
        ``ABORT_SECONDS``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.turn_seconds
        while turn.result is None:
            if turn.stopped:
                seconds = float(ABORT_SECONDS)
            else:
                seconds = max(0.0, deadline - loop.time())
            try:
                record = await asyncio.wait_for(self._open().receive(), seconds)
            except TimeoutError:
                if turn.stopped:
                    return
                await self._stop(
                    turn,
                    f"the turn passed its deadline of {self.turn_seconds:g} seconds",
                )
                continue
            if record is None:
                return
            await self._handle(record, turn)

    async def _handle(self, record: dict[str, Any], turn: _Turn) -> None:
        kind = record.get("type")
        if kind == "init":
            await self._init(record, turn)
            return
        if turn.stopped:
            return
        if kind not in RECORD_TYPES:
            await self._stop(
                turn, f"the engine sent a record Dromon does not know: {kind!r}"
            )
            return
        if not turn.started:
            await self._stop(turn, f"the engine sent {kind!r} before its init event")
            return
        if kind == "step_update":
            await self._step(record, turn)
        elif kind == "result":
            turn.result = record

    async def _init(self, record: dict[str, Any], turn: _Turn) -> None:
        if turn.started:
            await self._stop(turn, "the engine reported a second init event")
            return
        turn.started = True
        model = record.get("model")
        name = model if isinstance(model, str) else ""
        verdict = self.driver.step(Event("init", name))
        if verdict.tag is ActionTag.REFUSE:
            await self._stop(
                turn, f"the driver for {verdict.text} refuses the engine model {name!r}"
            )
            return
        mode = record.get("permission_mode")
        if mode not in ASK_MODES:
            await self._stop(turn, f"the engine runs in the permission mode {mode!r}")
            return
        # Antigravity review, F4. The line was given no tool, so init must list none,
        # and the run must be in the line's working directory.
        tools = record.get("tools")
        if tools != []:
            await self._stop(
                turn, f"the engine offers tools the line was not given: {tools!r}"
            )
            return
        cwd = record.get("cwd")
        if not isinstance(cwd, str) or os.path.realpath(cwd) != os.path.realpath(
            self.cwd
        ):
            await self._stop(turn, f"the engine runs in {cwd!r}, not {self.cwd}")
            return
        self.driver.step(Event("turn_start"))

    async def _step(self, record: dict[str, Any], turn: _Turn) -> None:
        step_type = record.get("step_type")
        if "subagent_info" in record:
            await self._stop(turn, "the engine started a subagent")
            return
        if step_type == "tool" or any(name in record for name in TOOL_FIELDS):
            name = str(record.get("tool_name") or "an unnamed tool")
            await self._stop(
                turn, f"the engine used a tool the line was not given: {name}"
            )
            return
        if step_type not in STEP_TYPES:
            await self._stop(
                turn, f"the engine sent a step Dromon does not know: {step_type!r}"
            )
            return
        if step_type == "agent_response":
            delta = record.get("text_delta")
            if isinstance(delta, str) and delta:
                turn.text.append(delta)
                self._observe(ActivityKind.TEXT, delta, chunk=True)
                await self._act(Event("text", "", len(delta)), turn)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._stop(turn, None)

    async def _stop(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.stopped:
            return
        turn.stopped = True
        await self._open().stop()
