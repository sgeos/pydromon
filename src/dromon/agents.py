"""External agents, harnesses outside the daemon that reach it as parties of their own.

An external agent, such as another Claude Code session, connects through ``dromon
mcp``, a stdio MCP server its harness starts. That server forwards each tool call to the
daemon's agent socket, which is separate from the operator socket, and the daemon
authenticates and authorizes every call. An agent is the party ``&name``, with its own
mailbox, and its messages are untrusted data, as a line's are.
``docs/protocol/AGENTS.md`` is normative.

Access. Each level includes every level before it.

- ``none``. Nothing.
- ``observe``. The daemon's status and a line's recent activity.
- ``message``. Send as itself, and list, read and acknowledge its own mail.
- ``manage``. Create, configure and release lines. A configuration can grant a line
  shell access and credentials, so this is close to operator trust.
- ``operator``. List and answer operator questions, as the operator asked, and stop
  the daemon.

Policy. The project file's ``[agents]`` table sets ``listen``, without which no agent
socket exists, ``anonymous``, the access of a connection that presents no token, and
``max_access``, which caps every agent. A registered agent has its own access and a
token, of which only the SHA-256 digest is stored. The range runs from no socket at all
to every connection trusted as an operator.

Trust. The agent socket is readable only by the operator's user account, like the
operator socket, so the daemon still trusts the operating system to keep other users
out. Tokens distinguish the agents of one user from each other. A registered name
cannot be claimed without its token, and an operator-level answer is recorded as the
operator's reply and noted in the daemon's output with the agent's name.

Porting note. The gateway is plain dispatch over a fixed tool table, and token checks
use the standard library's constant-time comparison.
"""

import hashlib
import hmac
import json
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool

from dromon import disclosure
from dromon.model import (
    ID_PATTERN,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    EventRejected,
    InvalidField,
    State,
)
from dromon.protocol import Operation, Request, Response

TOKEN_BYTES: Final = 32
TOKEN_ENV: Final = "DROMON_AGENT_TOKEN"  # noqa: S105, the variable's name
LINE_TOKEN_ENV: Final = "DROMON_LINE_TOKEN"  # noqa: S105, the variable's name
"""The variable through which ``dromon mcp --line`` receives a line's token."""
"""The variable through which ``dromon mcp`` receives an agent's token."""


class AgentError(Exception):
    """A connection was refused, or a call was not permitted."""


# Policy.


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    listen: bool = False
    """Whether the daemon opens the agent socket at all."""
    anonymous: AgentAccess = AgentAccess.NONE
    """The access of a connection that presents no token."""
    max_access: AgentAccess = AgentAccess.MESSAGE
    """The most any agent receives, registered or not."""


OFF: Final = AgentPolicy()
POLICY_KEYS: Final = frozenset({"listen", "anonymous", "max_access"})


def _access(value: object, key: str) -> AgentAccess:
    try:
        return AgentAccess(cast(str, value))
    except ValueError as error:
        choices = [level.value for level in AgentAccess]
        raise AgentError(f"agents.{key} must be one of {choices}") from error


def parse_policy(value: object) -> AgentPolicy:
    """Validate an ``[agents]`` table, or raise ``AgentError`` naming the key."""
    if not isinstance(value, dict):
        raise AgentError("agents must be a table")
    table = cast(dict[str, object], value)
    unknown = sorted(set(table) - POLICY_KEYS)
    if unknown:
        raise AgentError(
            f"agents.{unknown[0]} is not a known key. Known keys are"
            f" {sorted(POLICY_KEYS)}"
        )
    listen = table.get("listen", False)
    if type(listen) is not bool:
        raise AgentError("agents.listen must be a boolean")
    anonymous = _access(table.get("anonymous", "none"), "anonymous")
    ceiling = _access(table.get("max_access", "message"), "max_access")
    if anonymous.rank > ceiling.rank:
        raise AgentError(
            f"agents.anonymous {anonymous} exceeds agents.max_access {ceiling}"
        )
    return AgentPolicy(listen, anonymous, ceiling)


# Tokens.


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _capped(access: AgentAccess, ceiling: AgentAccess) -> AgentAccess:
    return access if access.rank <= ceiling.rank else ceiling


LINE_TOKEN_IDLE_SECONDS: Final = 3600.0
"""How long a line token stays valid without use between turns. **Row 12 of the T3
Code review, and review F15.** A token never expires while its line's turn runs, which
the turn's own deadline bounds, so a tool call late in a long turn is not refused.
Expiry bounds only a revocation that never came, such as one lost with an engine
process that outlived its turn."""


class LineTokens:
    """Tokens through which an engine that cannot host Dromon's tools in process,
    such as Grok Build, reaches them over ``dromon mcp --line``, held only in memory,
    so a restart invalidates them all.

    **Row 12 of the T3 Code review, and review F14.** A token is scoped to one engine
    process. The scheduler renews a line's token as a turn starts a new process, which
    revokes the one before, and revokes it when that process ends. Every engine that
    uses tokens today starts its process for the turn and ends it within the turn, so
    its token ends with the turn. An engine whose process outlives its turn keeps its
    token between turns, with ``finish``, and has a liveness check, with ``bind``, so
    that a call after its process ended is refused and revokes the token. A token
    also expires after ``LINE_TOKEN_IDLE_SECONDS`` without use between turns. So a
    bridge child that outlived its engine holds a token that no longer works.
    ``issue`` returns the line's current token, or a new one, for an engine being
    built."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._tokens: dict[str, str] = {}
        self._used: dict[str, float] = {}
        """When each token was issued, last checked or last left by a turn, on
        ``clock``."""
        self._busy: set[str] = set()
        """Lines whose turn is running, whose token does not expire."""
        self._alive: dict[str, Callable[[], bool]] = {}
        """Each line's check of whether the process its token belongs to still runs,
        for an engine whose process outlives its turn."""
        self._clock: Callable[[], float] = clock or _monotonic

    def issue(self, line_id: str) -> str:
        """The line's current token, or a new one when it has none."""
        _check_line(line_id)
        token = self._tokens.get(line_id)
        if token is None or self._expired(line_id):
            return self._new(line_id)
        return token

    def renew(self, line_id: str) -> str:
        """A new token for a turn that starts a new engine process, which revokes the
        one before. It does not expire until the turn ends."""
        token = self._new(line_id)
        self._busy.add(line_id)
        return token

    def begin(self, line_id: str) -> str | None:
        """Keep the line's token for a turn on the engine process that already holds
        it, and return it, or ``None`` when it has none or it expired."""
        _check_line(line_id)
        if line_id not in self._tokens or self._expired(line_id):
            self.revoke(line_id)
            return None
        self._busy.add(line_id)
        return self._tokens[line_id]

    def finish(self, line_id: str) -> None:
        """The line's turn ended and its engine's process still runs, so its token
        stays, and from now expires after ``LINE_TOKEN_IDLE_SECONDS`` unused."""
        self._busy.discard(line_id)
        if line_id in self._tokens:
            self._used[line_id] = self._clock()

    def bind(self, line_id: str, alive: Callable[[], bool]) -> None:
        """Check ``alive`` at each use of the line's token, which is refused and
        revoked once the engine's process has ended."""
        self._alive[line_id] = alive

    def revoke(self, line_id: str) -> None:
        """End the line's token, as its engine's process ends. A line with none is
        left as it is."""
        self._tokens.pop(line_id, None)
        self._used.pop(line_id, None)
        self._busy.discard(line_id)
        self._alive.pop(line_id, None)

    def check(self, line_id: object, token: object) -> str:
        """The line ``token`` belongs to, or ``AgentError``. A valid check counts as
        a use, which keeps the token from expiring."""
        if not isinstance(line_id, str) or not isinstance(token, str):
            raise AgentError("line and token must be text")
        issued = self._tokens.get(line_id)
        if issued is None or not hmac.compare_digest(issued, token):
            raise AgentError(f"line {line_id} needs its token")
        if self._expired(line_id):
            self.revoke(line_id)
            raise AgentError(f"line {line_id}'s token expired unused")
        alive = self._alive.get(line_id)
        if alive is not None and not _still(alive):
            self.revoke(line_id)
            raise AgentError(f"line {line_id}'s engine process has ended")
        self._used[line_id] = self._clock()
        return line_id

    def _new(self, line_id: str) -> str:
        _check_line(line_id)
        token = new_token()
        self._tokens[line_id] = token
        self._used[line_id] = self._clock()
        self._alive.pop(line_id, None)
        return token

    def _expired(self, line_id: str) -> bool:
        if line_id in self._busy:
            return False
        used = self._used.get(line_id)
        return used is None or self._clock() - used > LINE_TOKEN_IDLE_SECONDS


def _still(alive: Callable[[], bool]) -> bool:
    """``alive``'s answer, and ``False`` when it fails, which fails closed."""
    try:
        return alive()
    except Exception:  # noqa: BLE001
        return False


def _monotonic() -> float:
    return time.monotonic()


def _check_line(line_id: str) -> None:
    if ID_PATTERN.fullmatch(line_id) is None:
        raise AgentError(f"line {line_id!r} is not an identifier")


# Tools.


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    minimum: AgentAccess
    description: str
    schema: dict[str, object]


def _schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TEXT: Final[dict[str, object]] = {"type": "string"}
LINE_ID: Final[dict[str, object]] = {"type": "string", "description": "A line"}

DAEMON_TOOLS: Final = (
    ToolSpec(
        "dromon_status",
        AgentAccess.OBSERVE,
        "The daemon's status and a summary of every session line.",
        _schema({}, []),
    ),
    ToolSpec(
        "view_line",
        AgentAccess.OBSERVE,
        "One line's state and its recent messages, with bodies shortened.",
        _schema({"line_id": LINE_ID, "limit": {"type": "integer"}}, ["line_id"]),
    ),
    ToolSpec(
        "create_line",
        AgentAccess.MANAGE,
        "Create a session line on a model and credential strategy, optionally with a"
        " line configuration object.",
        _schema(
            {
                "line_id": LINE_ID,
                "model": TEXT,
                "strategy": {
                    "type": "string",
                    "enum": ["subscription", "api", "flexible"],
                },
                "config": {"type": "object"},
            },
            ["line_id", "model"],
        ),
    ),
    ToolSpec(
        "configure_line",
        AgentAccess.MANAGE,
        "Replace a session line's configuration object.",
        _schema(
            {"line_id": LINE_ID, "config": {"type": "object"}}, ["line_id", "config"]
        ),
    ),
    ToolSpec(
        "release_line",
        AgentAccess.MANAGE,
        "Resume a line the daemon held at a conversation limit.",
        _schema({"line_id": LINE_ID}, ["line_id"]),
    ),
    ToolSpec(
        "list_questions",
        AgentAccess.OPERATOR,
        "Every open operator question.",
        _schema({}, []),
    ),
    ToolSpec(
        "answer_question",
        AgentAccess.OPERATOR,
        "Answer an open question as the operator it was addressed to. The answer"
        " wakes the asking line.",
        _schema({"question_id": TEXT, "body": TEXT}, ["question_id", "body"]),
    ),
    ToolSpec(
        "stop_daemon",
        AgentAccess.OPERATOR,
        "Stop the daemon.",
        _schema({}, []),
    ),
)
"""Tools served by the daemon's own request handling, with the access each needs."""

MAIL_TOOLS: Final = (
    "send_message",
    "list_mail",
    "read_mail",
    "acknowledge_mail",
    "list_sent",
)
"""Tools a line also has, served to an agent with ``message`` access as its own."""


class Host(Protocol):
    """What the gateway needs from the daemon."""

    @property
    def state(self) -> State: ...

    def append(self, event: AgentRegistered) -> None: ...

    def execute(self, request: Request) -> Response: ...

    def mail_tools(self, owner: AgentAddress) -> list[SdkMcpTool[Any]]: ...

    def discloses(self, *texts: str) -> bool: ...


type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _text(value: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": str(value)}], "is_error": False}


def _error(message: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


class Gateway:
    """Authenticates agents and runs their calls against the daemon."""

    def __init__(self, policy: AgentPolicy, host: Host) -> None:
        self.policy = policy
        self.host = host

    def authenticate(self, agent_id: object, token: object) -> AgentAccess:
        """The access of ``agent_id`` presenting ``token``, or ``AgentError``.

        A registered agent must present its token. A connection without one is
        admitted at the anonymous level, and becomes a party the first time it is
        allowed to message, so that it has a mailbox.
        """
        if not isinstance(agent_id, str) or ID_PATTERN.fullmatch(agent_id) is None:
            raise AgentError("agent must be an identifier, such as claude-main")
        if token is not None and not isinstance(token, str):
            raise AgentError("token must be text")
        known = self.host.state.agents.get(agent_id)
        if known is not None and known.revoked:
            raise AgentError(f"agent {agent_id} is revoked")
        if known is not None and known.token_sha256:
            if token is None or not hmac.compare_digest(
                token_digest(token), known.token_sha256
            ):
                raise AgentError(f"agent {agent_id} needs its token")
            access = _capped(known.access, self.policy.max_access)
        elif token is not None:
            raise AgentError(f"agent {agent_id} is not registered with a token")
        else:
            # An anonymous agent names itself, and the name becomes the sender of
            # its mail, so it is held to the project's disclosure list. An agent the
            # operator registered with a token was named by the operator.
            if self.host.discloses(agent_id):
                raise AgentError(disclosure.REFUSAL)
            access = _capped(self.policy.anonymous, self.policy.max_access)
            if access.rank >= AgentAccess.MESSAGE.rank and (
                known is None or known.access != access
            ):
                self.host.append(AgentRegistered(agent_id, access, ""))
        if access is AgentAccess.NONE:
            raise AgentError(f"agent {agent_id} has no access")
        return access

    def tools(self, access: AgentAccess) -> list[dict[str, object]]:
        """The tools an agent with ``access`` may call, in the MCP listing shape."""
        listed: list[dict[str, object]] = []
        if access.rank >= AgentAccess.MESSAGE.rank:
            for tool in self.host.mail_tools(AgentAddress("tools")):
                listed.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                )
        for spec in DAEMON_TOOLS:
            if access.rank >= spec.minimum.rank:
                listed.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "inputSchema": spec.schema,
                    }
                )
        return sorted(listed, key=lambda tool: str(tool["name"]))

    async def call(
        self, agent_id: str, access: AgentAccess, tool: object, arguments: object
    ) -> dict[str, object]:
        """Run one tool call for an authenticated agent, as an MCP tool result."""
        if not isinstance(tool, str):
            return _error("tool must be a name")
        if not isinstance(arguments, dict):
            return _error("arguments must be an object")
        args = cast(dict[str, Any], arguments)
        if tool in MAIL_TOOLS:
            if access.rank < AgentAccess.MESSAGE.rank:
                return _error(
                    f"{tool} needs message access, and this agent has {access}"
                )
            owner = AgentAddress(agent_id)
            handler = next(
                t.handler for t in self.host.mail_tools(owner) if t.name == tool
            )
            result = await handler(args)
            return {
                "content": result.get("content", []),
                "is_error": bool(result.get("is_error", False)),
            }
        spec = next((s for s in DAEMON_TOOLS if s.name == tool), None)
        if spec is None:
            return _error(f"no tool {tool}")
        if access.rank < spec.minimum.rank:
            return _error(
                f"{tool} needs {spec.minimum} access, and this agent has {access}"
            )
        config = args.get("config")
        if (
            spec.name in ("create_line", "configure_line")
            and isinstance(config, dict)
            and "plugins" in config
            and access is not AgentAccess.OPERATOR
        ):
            return _error(
                f"plugins load code into a line, so {tool} may set them only with"
                f" operator access, and this agent has {access}"
            )
        return self._daemon_tool(agent_id, spec.name, args)

    def _daemon_tool(
        self, agent_id: str, tool: str, args: dict[str, Any]
    ) -> dict[str, object]:
        try:
            operation, request_args = self._request(agent_id, tool, args)
        except (AgentError, InvalidField, EventRejected) as error:
            return _error(str(error))
        response = self.host.execute(
            Request(f"agent-{secrets.token_hex(6)}", operation, request_args)
        )
        if not response.ok:
            return _error(response.error_message or "the daemon refused the call")
        return _text(json.dumps(response.result, ensure_ascii=False, sort_keys=True))

    def _request(
        self, agent_id: str, tool: str, args: Mapping[str, object]
    ) -> tuple[Operation, dict[str, object]]:
        match tool:
            case "dromon_status":
                return Operation.STATUS, dict(args)
            case "view_line":
                return Operation.LINE_VIEW, dict(args)
            case "create_line":
                return Operation.CREATE_LINE, {"strategy": "subscription", **args}
            case "configure_line":
                return Operation.CONFIGURE_LINE, dict(args)
            case "release_line":
                return Operation.RELEASE, dict(args)
            case "list_questions":
                return Operation.QUESTIONS, dict(args)
            case "answer_question":
                question = self.host.state.messages.get(str(args.get("question_id")))
                if question is None:
                    raise AgentError(f"no question {args.get('question_id')}")
                operator = str(question.recipient).removeprefix("@")
                print(
                    f"dromond: agent &{agent_id} answers {args.get('question_id')}"
                    f" as @{operator}",
                    file=sys.stderr,
                )
                return Operation.ANSWER, {**args, "operator": operator}
            case _:
                print(f"dromond: agent &{agent_id} stops the daemon", file=sys.stderr)
                return Operation.STOP, dict(args)
