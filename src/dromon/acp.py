"""The Agent Client Protocol as Dromon speaks it, shared by the ACP engine adapters.

The Agent Client Protocol, ACP, is JSON-RPC 2.0 with one message per line. Dromon is
the client and the engine is the agent. Dromon sends ``initialize``, opens a session,
sends ``session/prompt``, and meanwhile answers the agent's requests and reads its
``session/update`` notifications. The Grok Build and Antigravity adapters both speak it.

This module holds only what does not depend on the engine. That is the request loop,
the answers Dromon gives, and the shapes it reads from the agent. Each adapter supplies
its own transport, its policy for each request and update, and its own refusals. Where
an engine needs more than the shared part, such as Grok's sandbox profile or
Antigravity's sign-in method, the adapter adds it.

Trust. Everything the agent sends is untrusted data. The loop decodes only the
envelope, and the adapter decides what each message means.

Porting note. Sequential and plain data, as the adapters are. Each request waits for
its own answer, and messages that arrive meanwhile are dispatched in order.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol, cast

from dromon.engine import EngineRefused

PROTOCOL_VERSION: Final = 1
"""The ACP version Dromon speaks, and the only one it accepts in an answer."""

CLIENT_CAPABILITIES: Final[Mapping[str, object]] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}
"""Dromon serves no file system and no terminal, so an agent uses its own tools, which
Dromon governs through permission requests and tool updates."""

METHOD_NOT_FOUND: Final = -32601

PERMISSION_OPTION_COUNT: Final = 8
"""At most this many offered permission options are named in a refusal."""
PERMISSION_OPTION_FIELD_CHARACTERS: Final = 40
"""The most of an offered option kind or identifier included in a refusal."""
PERMISSION_TOOL_NAME_CHARACTERS: Final = 120
"""The most of a tool name included in a missing-option refusal."""
PERMISSION_REFUSAL_CHARACTERS: Final = 1200
"""The maximum length of evidence retained when no permission option is usable."""

MCP_SERVER_NAME: Final = "dromon"
"""The name under which a line's own tools reach the agent, as ``dromon__TOOL``."""

TOKEN_VARIABLE: Final = "DROMON_LINE_TOKEN"  # noqa: S105, a variable name, not a secret


class Transport(Protocol):
    """One running agent, as the client sees it."""

    async def send(self, message: Mapping[str, object]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...


type Dispatch = Callable[[dict[str, Any]], Awaitable[None]]
"""What the adapter does with a message that is not the answer awaited."""


class RpcError(Exception):
    """The agent answered a request with an error. ``code`` is its JSON-RPC error code
    when it gave a whole number."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class Client:
    """The client side of one ACP connection, for one turn.

    ``dispatch`` receives every request and notification from the agent that arrives
    while a request of Dromon's waits for its answer, in the order they arrive.
    ``deadline``, on the event loop's clock, bounds every wait for a message, and an
    adapter may move it earlier during a turn, as when it cancels.
    """

    def __init__(self, transport: Transport, dispatch: Dispatch) -> None:
        self._transport = transport
        self._dispatch = dispatch
        self._next_id = 0
        self.deadline: float | None = None

    async def receive(self) -> dict[str, Any]:
        """The next message, or ``EngineRefused`` when the agent closed the connection.
        Raises ``TimeoutError`` once the deadline passes."""
        if self.deadline is None:
            message = await self._transport.receive()
        else:
            remaining = max(0.0, self.deadline - asyncio.get_running_loop().time())
            message = await asyncio.wait_for(self._transport.receive(), remaining)
        if message is None:
            raise EngineRefused("the engine closed its connection during the turn")
        return message

    async def call(self, method: str, params: Mapping[str, object]) -> dict[str, Any]:
        """Send a request and return its result, dispatching whatever arrives first.
        Raises ``RpcError`` when the agent answers with an error."""
        self._next_id += 1
        request_id = self._next_id
        await self._transport.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = await self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error: object = message["error"]
                    code = (
                        cast(dict[str, Any], error).get("code")
                        if isinstance(error, dict)
                        else None
                    )
                    raise RpcError(
                        f"{method}: {error}", code if type(code) is int else None
                    )
                result: object = message.get("result")
                return cast(dict[str, Any], result) if isinstance(result, dict) else {}
            await self._dispatch(message)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._transport.send(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def respond(self, request_id: object, result: Mapping[str, object]) -> None:
        await self._transport.send(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def refuse(self, request_id: object, method: str) -> None:
        """Answer a request Dromon does not serve."""
        await self._transport.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": METHOD_NOT_FOUND,
                    "message": f"Dromon does not serve {method}",
                },
            }
        )


def waiting_for_permission(fields: Mapping[str, Any]) -> bool:
    """A pending announcement may wait for its first permission request."""
    return fields.get("status") == "pending"


REJECTION_UNCONFIRMED: Final = (
    "the engine did not confirm that a rejected tool call did not run"
)


def rejected_call_not_run(fields: Mapping[str, Any]) -> bool:
    """Require explicit non-execution evidence on every post-rejection update.
    The toolNotRun metadata assertion is trusted as an engine protocol report,
    not proof of no side effects. Conflicting execution status fails closed.
    Status alone, including pending or failed, establishes no such evidence.
    """
    meta = fields.get("_meta")
    return (
        isinstance(meta, Mapping)
        and cast(Mapping[str, object], meta).get("toolNotRun") is True
        and fields.get("status") in (None, "pending", "failed")
    )


def permission_result(
    options: object, permitted: bool, tool_name: str
) -> tuple[dict[str, object], str | None]:
    """The answer to ``session/request_permission``. Only a single use is ever granted,
    so no grant outlives the call. A refusal prefers a single rejection. ACP version
    1 carries only an option identifier, with no standard model-visible reason.
    Metadata and RPC errors do not establish that a model receives an explanation.
    If no option of the intended kind is offered, answer as cancelled and return
    bounded evidence so the adapter can cancel rather than leave the turn running."""
    wanted = ("allow_once",) if permitted else ("reject_once", "reject_always")
    if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        offered = cast(Sequence[Any], options)
        for kind in wanted:
            for option in offered:
                if not isinstance(option, Mapping):
                    continue
                entry = cast(Mapping[str, Any], option)
                chosen = entry.get("optionId")
                if entry.get("kind") == kind and isinstance(chosen, str) and chosen:
                    return (
                        {"outcome": {"outcome": "selected", "optionId": chosen}},
                        None,
                    )
    intent = "allow" if permitted else "refuse"
    label = tool_name[:PERMISSION_TOOL_NAME_CHARACTERS] or "an unnamed tool"
    evidence = _permission_options_evidence(cast(object, options))
    message = (
        f"ACP offered no option for Dromon to {intent} tool {label!r}; "
        f"offered kinds and ids: {evidence}"
    )[:PERMISSION_REFUSAL_CHARACTERS]
    return {"outcome": {"outcome": "cancelled"}}, message


def _permission_options_evidence(options: object) -> str:
    if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
        return "<invalid option list>"
    offered = cast(Sequence[Any], options)
    if not offered:
        return "<none>"
    entries: list[str] = []
    for option in offered[:PERMISSION_OPTION_COUNT]:
        if not isinstance(option, Mapping):
            entries.append("<non-object>")
            continue
        entry = cast(Mapping[str, Any], option)
        kind = entry.get("kind")
        identifier = entry.get("optionId")
        kind_text = (
            kind[:PERMISSION_OPTION_FIELD_CHARACTERS]
            if isinstance(kind, str)
            else "<missing>"
        )
        identifier_text = (
            identifier[:PERMISSION_OPTION_FIELD_CHARACTERS]
            if isinstance(identifier, str)
            else "<missing>"
        )
        entries.append(f"kind={kind_text!r}, optionId={identifier_text!r}")
    if len(offered) > PERMISSION_OPTION_COUNT:
        entries.append(f"… and {len(offered) - PERMISSION_OPTION_COUNT} more")
    return ", ".join(entries)


LABEL_CHARACTERS: Final = 200
"""The most of a tool call's display text a refusal quotes."""


def tool_name(tool_call: Mapping[str, Any]) -> str:
    """The name a tool call gives itself in its structured ``toolName`` field, or the
    empty string when it has none.

    **Antigravity review, F4.** A ``title`` is display text, which a file name or the
    model can make equal to a Dromon tool's name, so it never names a tool. A call
    without ``toolName`` is therefore never one of Dromon's own tools, which fails
    closed. **Hypothesis.** ACP's schema does not define ``toolName``, so an engine
    that names its MCP tools only in titles has every use of Dromon's tools refused
    until a structured field is shown."""
    name = tool_call.get("toolName")
    return name if isinstance(name, str) else ""


def tool_label(tool_call: Mapping[str, Any]) -> str:
    """How a refusal names a tool call, for display only and never for a decision.
    The engine's own text, so it is cut to ``LABEL_CHARACTERS``."""
    for key in ("toolName", "title", "kind"):
        value = tool_call.get(key)
        if isinstance(value, str) and value:
            return value[:LABEL_CHARACTERS]
    return "an unnamed tool"


IDENTITY_FIELDS: Final = ("toolName", "title", "kind")
"""**Antigravity review, F1 to F3.** The fields of a tool call that say what it is. No
kind exempts a call from the check, and once a call is recorded, an update or a
permission request that gives any of these fields a value other than the recorded
one, or one the call did not have, refuses the turn."""


def identity(fields: Mapping[str, Any]) -> dict[str, object]:
    """The identity fields of a tool call as they are recorded when it is first seen."""
    return {key: fields[key] for key in IDENTITY_FIELDS if key in fields}


FINISHED_STATUSES: Final = frozenset({"completed", "failed"})
"""The tool call statuses after which the call's result has gone to the model."""


def newly_finished(
    identifier: str, update: Mapping[str, Any], finished: set[str]
) -> bool:
    """Whether ``update`` first reports the recorded call ``identifier`` finished,
    adding it to ``finished``, so a turn feeds each result to its driver once, as the
    safe point of priority mail needs."""
    if update.get("status") not in FINISHED_STATUSES or identifier in finished:
        return False
    finished.add(identifier)
    return True


def identity_refusal(
    identifier: object,
    fields: Mapping[str, Any],
    calls: Mapping[str, Mapping[str, object]],
) -> str | None:
    """Why a tool call or an update to one refuses the turn before its name is
    judged, or ``None``. ``calls`` holds the identity of each call recorded so far.
    **Antigravity review, F2 and F3.** A call without an identifier cannot be
    followed, and a recorded call whose name, title or kind changes may no longer be
    the call that was checked or permitted."""
    if not isinstance(identifier, str) or not identifier:
        return "the engine reported a tool call without an identifier"
    recorded = calls.get(identifier)
    if recorded is None:
        return None
    for key in IDENTITY_FIELDS:
        if key in fields and (key not in recorded or fields[key] != recorded[key]):
            return (
                f"the engine changed the {key} of its tool call"
                f" {identifier[:LABEL_CHARACTERS]!r}, which Dromon checked as"
                f" {tool_label(recorded)}"
            )
    return None


def own_tool_names(mcp_tools: Sequence[str]) -> frozenset[str]:
    """How the agent names the line's own tools, given their bare names."""
    return frozenset(f"{MCP_SERVER_NAME}__{name}" for name in mcp_tools)


def mcp_servers(
    bridge: Sequence[str], line_token: str | None
) -> list[dict[str, object]]:
    """The ``mcpServers`` of a session, Dromon's own tools served by ``bridge`` with the
    line's token in its environment, or none without a bridge or a token. The token is
    passed in the request, never written to disk."""
    if not bridge or line_token is None:
        return []
    return [
        {
            "name": MCP_SERVER_NAME,
            "command": bridge[0],
            "args": list(bridge[1:]),
            "env": [{"name": TOKEN_VARIABLE, "value": line_token}],
        }
    ]


def reported_model(result: Mapping[str, Any]) -> str | None:
    """The model a session reports, from ``models`` or from its configuration options,
    or ``None`` when it reports none."""
    models = result.get("models")
    if isinstance(models, dict):
        current = cast(dict[str, Any], models).get("currentModelId")
        if isinstance(current, str):
            return current
    for option in cast(list[Any], result.get("configOptions") or []):
        if not isinstance(option, dict):
            continue
        entry = cast(dict[str, Any], option)
        if (entry.get("id") or entry.get("configId")) != "model":
            continue
        value = entry.get("currentValue", entry.get("value"))
        if isinstance(value, dict):
            value = cast(dict[str, Any], value).get("value")
        if isinstance(value, str):
            return value
    return None


def model_values(update: Mapping[str, Any]) -> list[object]:
    """Every value a message gives for the session's model, unchecked, so that a
    caller can refuse any that is not the model it expects, including one it cannot
    read. **Antigravity review, F5.** The values are read from ``models``, from each
    ``model`` entry of ``configOptions``, which is the shape ACP's schema gives
    ``config_option_update``, and from the message's own ``configId`` and ``value``,
    the shape of ``session/set_config_option``, since the runtime's own shape is not
    known. A ``models`` or ``configOptions`` of the wrong type is itself a value, so
    that it is refused too."""
    found: list[object] = []
    models = update.get("models")
    if isinstance(models, dict) and "currentModelId" in models:
        found.append(cast(dict[str, Any], models).get("currentModelId"))
    elif models is not None and not isinstance(models, dict):
        found.append(models)
    entries = update.get("configOptions")
    options = cast(list[Any], entries) if isinstance(entries, list) else []
    if entries is not None and not isinstance(entries, list):
        found.append(entries)
    for option in [update, *options]:
        if not isinstance(option, dict):
            continue
        entry = cast(dict[str, Any], option)
        if (entry.get("id") or entry.get("configId")) != "model":
            continue
        value = entry.get("currentValue", entry.get("value"))
        if isinstance(value, dict):
            value = cast(dict[str, Any], value).get("value")
        found.append(value)
    return found


def model_choices(result: Mapping[str, Any]) -> frozenset[str]:
    """The model identifiers a session offers, from its ``model`` select option or from
    ``models.availableModels``."""
    found: set[str] = set()
    for option in cast(list[Any], result.get("configOptions") or []):
        if not isinstance(option, dict):
            continue
        entry = cast(dict[str, Any], option)
        if (entry.get("id") or entry.get("configId")) != "model":
            continue
        for choice in cast(list[Any], entry.get("options") or []):
            if not isinstance(choice, dict):
                continue
            item = cast(dict[str, Any], choice)
            group = item.get("options")
            members = cast(list[Any], group) if isinstance(group, list) else [item]
            for member in members:
                if isinstance(member, dict):
                    value = cast(dict[str, Any], member).get("value")
                    if isinstance(value, str):
                        found.add(value)
    models = result.get("models")
    if isinstance(models, dict):
        for model in cast(
            list[Any], cast(dict[str, Any], models).get("availableModels") or []
        ):
            if isinstance(model, dict):
                identifier = cast(dict[str, Any], model).get("modelId")
                if isinstance(identifier, str):
                    found.add(identifier)
    return frozenset(found)


async def ensure_mode(
    client: Client,
    session_id: str,
    result: Mapping[str, Any],
    accepted: frozenset[str],
    wanted: str,
) -> str | None:
    """Put the session in a permission mode that asks before acting. Returns ``None``
    when the session reports a mode in ``accepted`` or has switched to ``wanted``, and
    otherwise the reason to refuse the turn."""
    modes = result.get("modes")
    if not isinstance(modes, dict):
        return "the engine does not report its permission mode"
    state = cast(dict[str, Any], modes)
    current = state.get("currentModeId")
    if current in accepted:
        return None
    available = {
        str(cast(dict[str, Any], mode).get("id"))
        for mode in cast(list[Any], state.get("availableModes") or [])
        if isinstance(mode, dict)
    }
    if wanted not in available:
        return f"the engine runs in the permission mode {current!r}"
    await client.call("session/set_mode", {"sessionId": session_id, "modeId": wanted})
    return None


def bridge_command(root: Path, runtime_dir: Path, line_id: str) -> list[str]:
    """The command an agent starts for a line's Dromon tools, which serves them over the
    daemon's line socket."""
    return [
        sys.executable,
        "-m",
        "dromon.cli",
        "--project",
        str(root),
        "--runtime-dir",
        str(runtime_dir),
        "mcp",
        "--line",
        line_id,
    ]
