"""The Pi engine adapter, which runs session line turns through ``pi --mode rpc``.

**Unverified against a live turn.** Written on 2026-10-26 against Pi 1.4.0's own
documentation and its installed source, without running a model, since a turn spends
the operator's subscription. ``docs/protocol/PI.md`` records which facts were read and
which were inferred, and lists what to check first in a live turn.

Dromon drives Pi from outside. Each turn starts ``pi --mode rpc``, which reads JSON
commands on its standard input and writes responses and session events on its standard
output, one object per line. The adapter reads the session's model, sends the prompt,
feeds every event to the line's driver, and closes the input once Pi has settled.

Isolation. Pi documents that it has no sandbox and does not ask before a tool call, so
Dromon cannot keep its file and shell tools away from Dromon's state, the operator's
credentials or paths outside the line's working directory. A Pi line is therefore
refused any built-in tool, and runs with Dromon's own tools alone, which Pi's
``--tools`` allowlist and Dromon's extension both enforce. Every source of tools,
extensions, skills, prompt templates, themes, context files and project resources is
switched off by flag.

Configuration. Pi reads MCP servers, custom providers and settings from its agent
directory, which may hold the operator's own servers and credentials. Dromon runs Pi
on a dedicated agent directory, ``~/.pi/dromon-agent``, where the operator signs in
once, and refuses it while it holds an ``mcp.json`` or ``models.json`` or a setting
outside a known safe set. Dromon never opens its ``auth.json``.

Credentials. Before each turn Dromon runs ``pi auth check`` for the model's provider,
which reports whether Pi would use a sign-in or an API key without printing either.
A sign-in to a provider whose Pi sign-in is a subscription counts as a subscription,
and an API key as usage billed per token. A subscription line's Pi starts without any
variable that could carry an API key.

Porting note. The protocol is JSON lines over a pipe, and the adapter is sequential,
as the Codex and Grok adapters are.
"""

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import SdkMcpTool

from dromon import capabilities
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import (
    ActionTag,
    Credential,
    Driver,
    Event,
    Strategy,
    harness_id,
)
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon.jsonlines import (
    TURN_SECONDS,
    Connection,
    ConnectionFactory,
    is_cloud_or_credential,
    json_lines_factory,
)
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, WorkdirAccess
from dromon.launcher import STOP_POLICY_VARIABLE
from dromon.processes import MARK_VARIABLE

ENGINE_NAME: Final = "pi"
"""The engine this adapter runs, as drivers name it."""

MODEL_PREFIX: Final = "pi:"
"""Dromon's names for Pi models are this prefix and Pi's ``provider/id``."""

COMMAND: Final = "pi"

AGENT_DIR: Final = Path(".pi") / "dromon-agent"
"""The dedicated agent directory, relative to the home directory."""

MCP_SERVER_NAME: Final = "dromon"

EXTENSION_RESOURCE: Final = "pi_extension.js"
"""Dromon's packaged extension, which registers its tools and guards every call."""

SUBSCRIPTION_PROVIDERS: Final = frozenset(
    {
        "github-copilot",
        "kimi-coding",
        "meta",
        "openai",
        "openai-codex",
        "xai",
    }
)
"""Providers whose sign-in Pi 1.4.0 marks ``isSubscription``, read in its source,
except ``anthropic``. **Pi review, F7, decided by the operator on 2026-10-26.** Whether
a Claude plan may be drawn through a third-party harness is not established, so a
Claude sign-in through Pi is not taken to be a subscription and refuses the turn."""

API_ENVIRONMENT: Final = frozenset(
    {
        "AI_GATEWAY_API_KEY",
        "ANT_LING_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_OPENAI_API_KEY",
        "CLOUDFLARE_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_API_KEY",
        "HF_TOKEN",
    }
)
"""Variables Pi 1.4.0 reads a credential from, besides those ``_is_api_variable``
matches by name. Collected from its help, its provider guide and its source."""

LOCATION_ENVIRONMENT: Final = frozenset(
    {
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_LOGIN_CACHE_DIRECTORY",
        "AZURE_OPENAI_BASE_URL",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_RESOURCE_NAME",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_GATEWAY_ID",
        "CLOUDSDK_CONFIG",
        "GCE_METADATA_HOST",
        "GCE_METADATA_IP",
        "GCLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "LLAMA_BASE_URL",
    }
)
"""Variables Pi 1.4.0 reads that name an endpoint, a credential store or the account a
credential belongs to. **Pi review, F5.** A subscription line's Pi starts without them,
so the operator's environment cannot send a credential elsewhere or select a store
other than the one ``pi auth check`` reports."""


DROMON_CONTROLLED: Final = (
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_OFFLINE",
    "PI_TELEMETRY",
    "DROMON_PI_BRIDGE",
    "DROMON_PI_TOOLS",
    "DROMON_LINE_TOKEN",
)
"""Variables Dromon sets or clears itself."""

REFUSED_ENVIRONMENT: Final = ("PI_PACKAGE_DIR",)
"""Variables that would make Pi load its code from elsewhere."""

REFUSED_AGENT_FILES: Final = (
    "mcp.json",
    "models.json",
    "SYSTEM.md",
    "APPEND_SYSTEM.md",
)
"""Files in the agent directory that would add MCP servers, redirect a provider, or,
**Pi review, F4**, replace or extend the system prompt, which Pi reads from the agent
directory whatever the project's trust."""

SAFE_SETTINGS: Final = frozenset(
    {
        "$schema",
        "autocompleteMaxVisible",
        "branchSummary",
        "cacheWarming",
        "collapseChangelog",
        "compaction",
        "defaultModel",
        "defaultProvider",
        "defaultThinkingLevel",
        "deviceId",
        "doubleEscapeAction",
        "editorPaddingX",
        "enableAnalytics",
        "enableInstallTelemetry",
        "enabledModels",
        "followUpMode",
        "fullscreenCopyOnSelect",
        "fullscreenExitOutput",
        "fullscreenScrollbar",
        "fullscreenWheelScrollLines",
        "hideThinkingBlock",
        "httpIdleTimeoutMs",
        "images",
        "lastChangelogVersion",
        "markdown",
        "modelThinkingLevels",
        "outputPad",
        "quietStartup",
        "retry",
        "showCacheMissNotices",
        "showHardwareCursor",
        "steeringMode",
        "terminal",
        "theme",
        "thinkingBudgets",
        "trackingId",
        "transport",
        "treeFilterMode",
        "tuiMode",
        "warnings",
        "websocketConnectTimeoutMs",
    }
)
"""Settings the agent directory may hold, those Pi writes for itself and those that
change only display, retries and compaction. Any other setting refuses the line,
since settings such as ``packages``, ``shellCommandPrefix`` and ``httpProxy`` load
code, run commands or redirect requests."""

MAX_SETTINGS_BYTES: Final = 1024 * 1024

WARMING_ADVICE: Final = (
    ' Set "cacheWarming": "off" in it. **Pi review, F1.** Pi keeps a provider\'s prompt'
    " cache warm with requests of its own, by default for up to an hour after a turn's"
    " last request, which no event reports, so they would bill or draw on a plan"
    " uncounted."
)
MAX_PROMPT_ARGUMENT_BYTES: Final = 256 * 1024
"""The largest instructions passed on Pi's command line, well under macOS's limit on
arguments and environment together."""

THINKING: Final[dict[str, str]] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
"""Dromon's efforts as Pi's thinking levels, which include all of them."""

AUTH_CHECK_SECONDS: Final = 30
ABORT_SECONDS: Final = 60
"""How long an aborted run may take to settle before the turn is refused and Pi is
stopped."""
UI_DIALOGS: Final = frozenset({"select", "confirm", "input", "editor"})
"""Extension requests that wait for an answer. Dromon cancels each."""

DIALOG_REFUSED: Final = (
    "an extension asked for a {} dialog, which Dromon cancelled, since nobody is at"
    " this line's prompt. A line asks an operator with ask_operator"
)

PI_AFTER_ESTIMATE_NOTE: Final = (
    "Pi's own estimate of the size its compaction left, not a measured size"
)

PI_USAGE_NOTE: Final = (
    "the engine's tokens and cost for this turn, summed over its assistant messages"
    " and the summary calls of its own compactions."
    " Pi computes the cost from its model catalogue's prices, so on a subscription it"
    " is an estimate, not a charge"
)


def harness_model(model: str) -> str:
    """Pi's ``provider/id`` for Dromon's model name."""
    rest = harness_id(model)
    provider, _, identifier = rest.partition("/")
    if not model.startswith(MODEL_PREFIX) or not provider or not identifier:
        raise EngineRefused(
            f"{model!r} is not a Pi model name, {MODEL_PREFIX}PROVIDER/ID"
        )
    return rest


def unsupported(config: LineConfig) -> str | None:
    """Why a Pi line cannot run ``config``, or ``None`` if it can."""
    if config.workdir_access is WorkdirAccess.READ:
        return "Pi cannot confine a line to a read-only working directory"
    if config.builtin_tools:
        return (
            "Pi lines take no built-in tools, because Pi has no sandbox and does not"
            " ask before a tool call, so Dromon could not keep them from its state,"
            " the operator's credentials or paths outside the line's working directory"
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Pi lines do not yet support MCP servers of their own"
    if config.claude_code_prompt:
        return "Pi lines run on Pi's own prompt, not Claude Code's"
    if config.project_settings:
        return "Pi lines do not load Claude Code's project settings"
    if config.plugins:
        return "Pi lines do not load Claude Code plugins"
    if config.allow_compact:
        return "Pi lines cannot compact their session through Dromon"
    if config.keep_session:
        return capabilities.kept_session_refusal(capabilities.PI)
    if config.sandbox is not None or config.settings is not None:
        return "Pi lines take no sandbox or settings document"
    return None


def _is_api_variable(name: str) -> bool:
    return (
        name in API_ENVIRONMENT
        or name in LOCATION_ENVIRONMENT
        or is_cloud_or_credential(name)
    )


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment Pi starts with, without Dromon's own variables and, for a
    subscription, without any credential variable. Raises ``EngineRefused`` when it
    would make Pi load its code from elsewhere."""
    present = [name for name in REFUSED_ENVIRONMENT if environment.get(name)]
    if present:
        raise EngineRefused(
            "the environment conflicts with a Pi line: " + ", ".join(present)
        )
    removed = set(DROMON_CONTROLLED)
    return {
        k: v
        for k, v in environment.items()
        if k not in removed
        and not (strategy is Strategy.SUBSCRIPTION and _is_api_variable(k))
    }


def check_agent_dir(agent_dir: Path) -> None:
    """Refuse an agent directory that is missing, or that would give a line an MCP
    server, a provider or a setting it was not given. ``auth.json`` is never opened."""
    advice = (
        f" Sign Pi in there with PI_CODING_AGENT_DIR={agent_dir} pi, then /login, and"
        " keep it for Dromon's lines alone."
    )
    if not agent_dir.is_dir():
        raise EngineRefused(
            f"the Pi agent directory {agent_dir} does not exist.{advice}"
        )
    found = [name for name in REFUSED_AGENT_FILES if os.path.lexists(agent_dir / name)]
    if found:
        raise EngineRefused(
            f"the Pi agent directory {agent_dir} holds " + ", ".join(found) + ", which"
            " would give a line MCP servers, providers or instructions it was not"
            " given."
            " Remove them, or keep them in Pi's own agent directory."
        )
    settings = agent_dir / "settings.json"
    if not os.path.lexists(settings):
        raise EngineRefused(f"{settings} is missing.{WARMING_ADVICE}")
    try:
        with settings.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except OSError as error:
        raise EngineRefused(f"cannot read {settings}: {error}") from error
    if len(raw) > MAX_SETTINGS_BYTES:
        raise EngineRefused(f"{settings} exceeds {MAX_SETTINGS_BYTES} bytes")
    try:
        document: object = json.loads(raw)
    except ValueError as error:
        raise EngineRefused(f"{settings} is not JSON") from error
    if not isinstance(document, dict):
        raise EngineRefused(f"{settings} is not a JSON object")
    unknown = sorted(set(cast(dict[str, object], document)) - SAFE_SETTINGS)
    if unknown:
        raise EngineRefused(
            f"{settings} sets " + ", ".join(unknown) + ", which Dromon does not accept"
            " for a Pi line, since such settings can load code, run commands or"
            " redirect requests"
        )
    if cast(dict[str, object], document).get("cacheWarming") != "off":
        raise EngineRefused(
            f"{settings} does not switch cache warming off.{WARMING_ADVICE}"
        )


def extension_path() -> Path:
    """Where Dromon's packaged extension is, as a file Pi can load."""
    resource = resources.files("dromon").joinpath(EXTENSION_RESOURCE)
    path = Path(str(resource))
    if not path.is_file():
        raise EngineRefused(f"Dromon's Pi extension is missing at {path}")
    return path


def pi_arguments(
    model: str,
    effort: str,
    *,
    agent_dir: Path,
    extension: Path,
    tools: Sequence[str],
    session_file: str | None,
    instructions: str,
) -> list[str]:
    """The ``pi`` arguments for one turn. Every source of tools and resources is
    switched off, and only Dromon's extension and, when the line has tools, Pi's
    built-in MCP support are loaded."""
    arguments = [
        "--mode",
        "rpc",
        "--model",
        model,
        "--thinking",
        THINKING[effort],
        "--session-dir",
        str(agent_dir / "sessions"),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--offline",
        "--extension",
        str(extension),
    ]
    if tools:
        arguments += ["--extension", "builtin:mcp", "--tools", ",".join(tools)]
    else:
        arguments += ["--no-tools", "--no-mcp"]
    if session_file is not None:
        arguments += ["--session", session_file]
    if instructions:
        arguments += ["--append-system-prompt", instructions]
    return arguments


type AuthChecker = Callable[[str, Mapping[str, str]], Awaitable[Mapping[str, object]]]
"""Reports, for a provider and the environment Pi starts with, what Pi's
``auth check`` says, without the credential."""


async def pi_auth_check(
    provider: str, environment: Mapping[str, str]
) -> dict[str, Any]:
    """Run ``pi auth check --provider PROVIDER --json --no-refresh``, which reads the
    credential store without refreshing it and never prints the credential."""
    found = shutil.which(COMMAND, path=environment.get("PATH"))
    if found is None:
        raise EngineRefused("the pi command is not installed or not on PATH")
    process = await asyncio.create_subprocess_exec(
        found,
        "auth",
        "check",
        "--provider",
        provider,
        "--json",
        "--no-refresh",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(environment),
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(), timeout=AUTH_CHECK_SECONDS
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise EngineRefused(
            f"pi auth check did not answer within {AUTH_CHECK_SECONDS} seconds"
        ) from error
    lines = output.decode("utf-8", "replace").strip().splitlines()
    try:
        decoded: object = json.loads(lines[-1]) if lines else None
    except ValueError as error:
        raise EngineRefused("pi auth check did not answer in JSON") from error
    if not isinstance(decoded, dict):
        raise EngineRefused("pi auth check did not answer with an object")
    return cast(dict[str, Any], decoded)


def credential_of(provider: str, answer: Mapping[str, object]) -> Credential:
    """The credential Pi would use, from its ``auth check`` answer."""
    status = answer.get("status")
    if status != "ready":
        raise EngineRefused(
            f"Pi has no usable credential for {provider}: {status!r},"
            f" {answer.get('reason')!r}"
        )
    if answer.get("provider") != provider:
        raise EngineRefused(
            f"pi auth check answered for {answer.get('provider')!r}, not {provider}"
        )
    kind = answer.get("authType")
    if kind == "oauth" and provider in SUBSCRIPTION_PROVIDERS:
        return Credential.SUBSCRIPTION
    if kind == "api_key":
        return Credential.API
    raise EngineRefused(
        f"Pi reports the credential type {kind!r} for {provider}, which Dromon cannot"
        " tell to be a subscription or an API key"
    )


def _quiet(agent_dir: Path) -> dict[str, str]:
    """The variables every Pi Dromon starts runs with, its agent directory, offline
    and without telemetry."""
    return {
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }


async def accepted_credential(
    driver: Driver,
    provider: str,
    environment: Mapping[str, str],
    auth_check: AuthChecker,
) -> Credential:
    """The credential ``pi auth check`` reports for ``provider``, once ``driver``
    accepts it. Raises ``EngineRefused`` otherwise."""
    credential = credential_of(provider, await auth_check(provider, environment))
    verdict = driver.step(Event("credential", credential.value))
    if verdict.tag is ActionTag.REFUSE:
        raise EngineRefused(
            f"the {verdict.text} driver refuses the {credential} credential"
        )
    return credential


async def check_sign_in(
    driver: Driver,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    auth_check: AuthChecker = pi_auth_check,
) -> Credential:
    """Pi's sign-in check for ``driver``'s model, which runs no turn, as ``dromon
    probe --no-turn`` does. It runs ``pi auth check`` in the environment a turn would
    have, so Pi reads its own credential store and Dromon reads none. Row 16 of the
    T3 Code review."""
    if driver.settings.engine != ENGINE_NAME:
        raise EngineRefused(f"{driver.settings.model} is not a Pi model")
    started = engine_environment(
        os.environ if environment is None else environment, driver.settings.strategy
    )
    started.update(_quiet((Path.home() if home is None else home) / AGENT_DIR))
    provider = harness_model(driver.settings.model).partition("/")[0]
    return await accepted_credential(driver, provider, started, auth_check)


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    aborted: bool = False
    settled: bool = False
    assistant_turns: int = 0
    stop_reason: str | None = None
    context: int | None = None
    calls: list[dict[str, int | bool]] = field(
        default_factory=list[dict[str, int | bool]]
    )
    cost: float | None = None


USAGE_KEYS: Final = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cache_read_input_tokens",
    "cacheWrite": "cache_creation_input_tokens",
}
"""Pi's usage fields under the names Dromon reports Claude Code's by."""


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _call_usage(usage: Mapping[str, object]) -> dict[str, int | bool]:
    call: dict[str, int | bool] = {
        name: _count(usage.get(key)) for key, name in USAGE_KEYS.items()
    }
    call["subagent"] = False
    return call


def _count_usage(usage: Mapping[str, object], turn: _Turn) -> dict[str, int | bool]:
    """Add one engine call's tokens and cost to the turn, and return the call."""
    call = _call_usage(usage)
    turn.calls.append(call)
    cost = usage.get("cost")
    total = cast(dict[str, Any], cost).get("total") if isinstance(cost, dict) else None
    if isinstance(total, int | float) and not isinstance(total, bool) and total >= 0:
        turn.cost = (turn.cost or 0.0) + float(total)
    return call


def _context_of(call: Mapping[str, int | bool]) -> int:
    """The context a call re-read, its input with what was read from or written to
    the cache, as Dromon estimates it for Claude Code."""
    return sum(
        int(call[key])
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


class PiLine:
    """One session line on the Pi engine, with its driver."""

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[Any]] = (),
        session_id: str | None = None,
        bridge: Sequence[str] = (),
        line_token: str | None = None,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
        connection_factory: ConnectionFactory | None = None,
        auth_check: AuthChecker = pi_auth_check,
        extension: Path | None = None,
        observe: Observer = ignore,
        mark: str | None = None,
        compacted: Callable[[Mapping[str, int | str | None]], object] = (
            lambda compaction: None
        ),
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
        self.bridge = tuple(bridge)
        self.line_token = line_token
        reachable = bool(self.bridge) and line_token is not None
        self.own_tools = frozenset(
            f"mcp__{MCP_SERVER_NAME}__{tool.name}" for tool in mcp_tools if reachable
        )
        self.session_id = session_id
        self._environment = os.environ if environment is None else environment
        self.agent_dir = (Path.home() if home is None else home) / AGENT_DIR
        self._connect = connection_factory or json_lines_factory(
            COMMAND, keep_input=True
        )
        self._auth_check = auth_check
        self._extension = extension
        self._observe = observe
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self._compacted = compacted
        self.turn_seconds = turn_seconds
        """How long a turn may run before it is aborted and refused."""
        self._deadline = 0.0
        self._next_id = 0
        self._connection: Connection | None = None
        self._interrupted_cost: float | None = None
        """What a cancelled turn had cost when it was cancelled, until taken."""

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        settings = self.driver.settings
        environment = engine_environment(self._environment, settings.strategy)
        check_agent_dir(self.agent_dir)
        effort = self.config.effort or settings.effort
        if effort not in THINKING:
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        model = harness_model(settings.model)
        provider = model.partition("/")[0]
        environment.update(_quiet(self.agent_dir))
        credential = await accepted_credential(
            self.driver, provider, environment, self._auth_check
        )
        tools = sorted(self.own_tools)
        environment["DROMON_PI_TOOLS"] = json.dumps(tools)
        if tools and self.line_token is not None:
            environment["DROMON_PI_BRIDGE"] = json.dumps(list(self.bridge))
            environment["DROMON_LINE_TOKEN"] = self.line_token
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
        arguments = pi_arguments(
            model,
            effort,
            agent_dir=self.agent_dir,
            extension=self._extension or extension_path(),
            tools=tools,
            session_file=self._session_to_resume(),
            instructions=self._instructions(),
        )
        turn = _Turn()
        self._deadline = asyncio.get_running_loop().time() + self.turn_seconds
        try:
            async with self._connect(arguments, self.cwd, environment) as connection:
                self._connection = connection
                try:
                    session_file = await self._converse(prompt, turn)
                finally:
                    self._connection = None
        except asyncio.CancelledError:
            self._interrupted_cost = turn.cost
            raise
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if session_file is not None and os.path.isfile(session_file):
            self.session_id = session_file
        return self._report(turn, credential)

    def take_interrupted_cost(self) -> float | None:
        """The cost Pi reported for the calls of a turn that was cancelled, such as
        by an operator's interrupt, once. **Review N8.** ``None`` when none was."""
        cost, self._interrupted_cost = self._interrupted_cost, None
        return cost

    def _session_to_resume(self) -> str | None:
        """The session file to resume, after checking that it is still a file under
        the agent directory's sessions. A session that is gone refuses the turn, so
        Pi never quietly starts a new one in its place."""
        if self.session_id is None:
            return None
        path = Path(self.session_id)
        sessions = Path(os.path.realpath(self.agent_dir / "sessions"))
        real = Path(os.path.realpath(path))
        if not path.is_absolute() or sessions not in real.parents:
            raise EngineRefused(
                f"the line's Pi session {self.session_id} is not under {sessions}"
            )
        if not real.is_file():
            raise EngineRefused(f"the line's Pi session {self.session_id} is gone")
        return str(real)

    def _instructions(self) -> str:
        text = read_instructions(self.config.instructions, self.cwd)
        if not text:
            return ""
        # Pi reads the value as a file when it names one, relative to its directory.
        if os.path.exists(self.cwd / text):
            raise EngineRefused(
                "the line's instructions name a file, which Pi would read"
            )
        if len(text.encode("utf-8")) > MAX_PROMPT_ARGUMENT_BYTES:
            raise EngineRefused(
                f"the line's instructions exceed {MAX_PROMPT_ARGUMENT_BYTES} bytes,"
                " which Pi takes on its command line"
            )
        return text

    def _report(self, turn: _Turn, credential: Credential) -> TurnReport:
        if turn.interrupted_for is not None or turn.aborted:
            subtype = "interrupted"
        elif turn.stop_reason in (None, "stop"):
            subtype = "success"
        else:
            subtype = str(turn.stop_reason)
        self.driver.step(Event("result", subtype, turn.assistant_turns))
        usage: dict[str, int] | None = None
        if turn.calls:
            usage = {
                name: sum(int(call[name]) for call in turn.calls)
                for name in USAGE_KEYS.values()
            }
        return TurnReport(
            subtype=subtype,
            num_turns=turn.assistant_turns,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=credential,
            session_id=self.session_id or "",
            capabilities=None,
            cost_estimate_usd=turn.cost,
            usage=usage,
            calls=tuple(dict(call) for call in turn.calls[-50:]),
            context_tokens=turn.context,
            usage_note=PI_USAGE_NOTE if turn.calls else None,
            # Pi documents that it has no sandbox, as the module says.
            sandbox_mode="none",
        )

    # The protocol.

    def _open(self) -> Connection:
        if self._connection is None:
            raise EngineRefused("the engine connection is not open")
        return self._connection

    async def _receive(self, seconds: float | None = None) -> dict[str, Any]:
        """The next record, waiting at most ``seconds`` or else until the turn's
        deadline. Raises ``TimeoutError`` when the wait ends first."""
        if seconds is None:
            seconds = max(0.0, self._deadline - asyncio.get_running_loop().time())
        record = await asyncio.wait_for(self._open().receive(), seconds)
        if record is None:
            raise EngineRefused("the engine closed its connection during the turn")
        return record

    async def _call(
        self, command: str, turn: _Turn, **fields: object
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = f"dromon-{self._next_id}"
        await self._open().send({"id": request_id, "type": command, **fields})
        while True:
            try:
                record = await self._receive()
            except TimeoutError as error:
                raise EngineRefused(self._deadline_refusal()) from error
            if record.get("type") == "response" and record.get("id") == request_id:
                if record.get("success") is not True:
                    raise EngineRefused(
                        f"the engine refused {command}: {record.get('error')!r}"
                    )
                data: object = record.get("data")
                return cast(dict[str, Any], data) if isinstance(data, dict) else {}
            await self._dispatch(record, turn)

    async def _converse(self, prompt: str, turn: _Turn) -> str | None:
        """The turn's exchange. Returns the session file Pi reports."""
        state = await self._call("get_state", turn)
        reported = state.get("model")
        name = ""
        if isinstance(reported, dict):
            entry = cast(dict[str, Any], reported)
            name = f"{entry.get('provider') or ''}/{entry.get('id') or ''}"
        verdict = self.driver.step(Event("init", name))
        if verdict.tag is ActionTag.REFUSE:
            turn.refusal = (
                f"the driver for {verdict.text} refuses the engine model {name!r}"
            )
            return None
        session_file = state.get("sessionFile")
        session_file = session_file if isinstance(session_file, str) else None
        expected = self._session_to_resume()
        if expected is not None and (
            session_file is None or os.path.realpath(session_file) != expected
        ):
            turn.refusal = (
                f"the engine opened the session {session_file!r}, not {expected}"
            )
            return None
        self.driver.step(Event("turn_start"))
        answer = await self._call("prompt", turn, message=prompt)
        disposition = answer.get("disposition")
        if disposition != "started":
            turn.refusal = (
                f"the engine did not start a run, its answer was {disposition!r}"
            )
            return session_file
        while not turn.settled:
            if not turn.aborted:
                try:
                    record = await self._receive()
                except TimeoutError:
                    await self._abort(turn, self._deadline_refusal())
                    continue
                await self._dispatch(record, turn)
                continue
            try:
                record = await self._receive(ABORT_SECONDS)
            except TimeoutError:
                turn.refusal = turn.refusal or (
                    f"the engine did not settle within {ABORT_SECONDS} seconds of"
                    " being aborted"
                )
                break
            await self._dispatch(record, turn)
        return session_file

    async def _dispatch(self, record: dict[str, Any], turn: _Turn) -> None:
        kind = record.get("type")
        if kind == "extension_ui_request":
            method = record.get("method")
            if method in UI_DIALOGS:
                await self._open().send(
                    {
                        "type": "extension_ui_response",
                        "id": record.get("id"),
                        "cancelled": True,
                    }
                )
                # Row 5 of the T3 Code review. The refusal is explicit to the engine,
                # and is surfaced, so that no request for a decision passes unseen.
                self._observe(ActivityKind.NOTICE, DIALOG_REFUSED.format(method))
        elif kind == "extension_error":
            await self._abort(
                turn, f"an extension failed in the engine: {record.get('error')!r}"
            )
        elif kind == "message_update":
            await self._update(
                cast(dict[str, Any], record.get("assistantMessageEvent") or {}), turn
            )
        elif kind == "message_end":
            self._message_end(cast(dict[str, Any], record.get("message") or {}), turn)
        elif kind == "turn_start":
            turn.assistant_turns += 1
            if turn.assistant_turns > self.driver.settings.max_turns:
                turn.interrupted_for = turn.interrupted_for or "max_turns"
                await self._abort(turn, None)
        elif kind == "tool_execution_start":
            await self._tool(str(record.get("toolName") or ""), turn)
        elif kind == "compaction_end":
            self._compaction(record, turn)
        elif kind == "agent_settled":
            turn.settled = True
            if record.get("aborted") is True:
                turn.aborted = True

    async def _update(self, event: dict[str, Any], turn: _Turn) -> None:
        kind = event.get("type")
        if kind == "text_delta":
            text = str(event.get("delta") or "")
            self._observe(ActivityKind.TEXT, text, chunk=True)
            await self._act(Event("text", "", len(text)), turn)
        elif kind == "thinking_start":
            self._observe(ActivityKind.THINKING, "")
            await self._act(Event("thinking"), turn)

    def _message_end(self, message: dict[str, Any], turn: _Turn) -> None:
        if message.get("role") != "assistant":
            return
        for block in cast(list[object], message.get("content") or []):
            if not isinstance(block, dict):
                continue
            entry = cast(dict[str, Any], block)
            if entry.get("type") == "text":
                turn.text.append(str(entry.get("text") or ""))
        stop = message.get("stopReason")
        turn.stop_reason = stop if isinstance(stop, str) else None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        call = _count_usage(cast(dict[str, Any], usage), turn)
        turn.context = _context_of(call)

    async def _tool(self, name: str, turn: _Turn) -> None:
        if name not in self.own_tools:
            await self._abort(
                turn, f"the engine used a tool the line was not given: {name}"
            )
            return
        turn.tool_uses.append(name)
        self._observe(ActivityKind.TOOL, name)
        await self._act(Event("tool_use", name), turn)

    def _compaction(self, record: dict[str, Any], turn: _Turn) -> None:
        """A compaction Pi made by itself. **Pi review, F2 and F3.** Its summary call's
        tokens and cost join the turn's, and Pi's estimate of the size after it is
        reported as an estimate."""
        result = record.get("result")
        if not isinstance(result, dict):
            return
        reported = cast(dict[str, Any], result)
        usage = reported.get("usage")
        if isinstance(usage, dict):
            _count_usage(cast(dict[str, Any], usage), turn)
        before = reported.get("tokensBefore")
        estimate = reported.get("estimatedTokensAfter")
        self._compacted(
            {
                "trigger": "manual" if record.get("reason") == "manual" else "auto",
                "before_tokens": before if type(before) is int else None,
                "after_tokens": None,
                "after_estimate_tokens": estimate if type(estimate) is int else None,
                "after_estimate_note": PI_AFTER_ESTIMATE_NOTE,
            }
        )

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._abort(turn, None)

    def _deadline_refusal(self) -> str:
        return f"the turn passed its deadline of {self.turn_seconds:g} seconds"

    async def _abort(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.aborted:
            return
        turn.aborted = True
        self._next_id += 1
        await self._open().send({"id": f"dromon-{self._next_id}", "type": "abort"})
