"""The engine-neutral Agent Client Protocol helpers the Grok and Antigravity adapters
share. Their behaviour within a turn is tested through each adapter."""

import asyncio
from collections import deque
from collections.abc import Mapping
from typing import Any

import pytest

from dromon import acp
from dromon.engine import EngineRefused

OPTIONS = [
    {"optionId": "always", "kind": "allow_always"},
    {"optionId": "never", "kind": "reject_always"},
    {"optionId": "no", "kind": "reject_once"},
    {"optionId": "once", "kind": "allow_once"},
]


def test_a_permission_is_granted_once_and_refused_once_by_preference() -> None:
    assert acp.permission_result(OPTIONS, True, "read_file") == (
        {"outcome": {"outcome": "selected", "optionId": "once"}},
        None,
    )
    assert acp.permission_result(OPTIONS, False, "read_file") == (
        {"outcome": {"outcome": "selected", "optionId": "no"}},
        None,
    )
    only_always = [{"optionId": "never", "kind": "reject_always"}]
    assert acp.permission_result(only_always, False, "read_file") == (
        {"outcome": {"outcome": "selected", "optionId": "never"}},
        None,
    )


def test_refusal_selects_reject_when_a_valid_allow_once_is_also_offered() -> None:
    options = [
        {"optionId": "allow", "kind": "allow_once"},
        {"optionId": "reject", "kind": "reject_once"},
    ]

    assert acp.permission_result(options, False, "read_file") == (
        {"outcome": {"outcome": "selected", "optionId": "reject"}},
        None,
    )


@pytest.mark.parametrize(("permitted", "intent"), [(True, "allow"), (False, "refuse")])
def test_missing_permission_options_refuse_with_bounded_evidence(
    permitted: bool, intent: str
) -> None:
    options = [
        {"optionId": "later", "kind": "unexpected"},
        {"optionId": "", "kind": "allow_once"},
    ]
    answer, refusal = acp.permission_result(options, permitted, "a" * 400)

    assert answer == {"outcome": {"outcome": "cancelled"}}
    assert refusal is not None
    assert "ACP offered no option" in refusal
    assert intent in refusal
    assert "a" * acp.PERMISSION_TOOL_NAME_CHARACTERS in refusal
    assert "kind='unexpected', optionId='later'" in refusal
    assert "kind='allow_once', optionId=''" in refusal
    assert len(refusal) <= acp.PERMISSION_REFUSAL_CHARACTERS


def test_unexpected_permission_option_kinds_never_grant_broadly() -> None:
    answer, refusal = acp.permission_result(
        [{"optionId": "persistent", "kind": "allow_always"}], True, "write"
    )

    assert answer == {"outcome": {"outcome": "cancelled"}}
    assert refusal is not None
    assert "kind='allow_always', optionId='persistent'" in refusal


def test_permission_option_evidence_bounds_option_count_and_field_lengths() -> None:
    options = [{"optionId": "i" * 500, "kind": "k" * 500} for _ in range(20)]
    answer, refusal = acp.permission_result(options, True, "tool")

    assert answer == {"outcome": {"outcome": "cancelled"}}
    assert refusal is not None
    assert "… and 12 more" in refusal
    assert len(refusal) <= acp.PERMISSION_REFUSAL_CHARACTERS


def test_the_model_is_read_from_models_or_options() -> None:
    assert acp.reported_model({"models": {"currentModelId": "a"}}) == "a"
    option: dict[str, Any] = {"id": "model", "currentValue": "b", "options": []}
    assert acp.reported_model({"configOptions": [option]}) == "b"
    assert acp.reported_model({"configOptions": [{"id": "mode", "value": "x"}]}) is None
    assert acp.reported_model({}) is None


def test_a_tool_is_named_only_by_its_structured_field() -> None:
    """Antigravity review, F4. A title that equals a Dromon tool's name does not make
    the call one, and the label for a refusal never decides anything."""
    own = "dromon__send_message"
    assert acp.tool_name({"toolName": own, "title": "Running send_message"}) == own
    assert acp.tool_name({"title": own}) == ""
    assert acp.tool_name({"toolName": 7, "title": own}) == ""
    assert acp.tool_label({"title": "ls", "kind": "execute"}) == "ls"
    assert acp.tool_label({"kind": "execute"}) == "execute"
    assert acp.tool_label({}) == "an unnamed tool"
    long = "x" * (acp.LABEL_CHARACTERS * 2)
    assert len(acp.tool_label({"title": long})) == acp.LABEL_CHARACTERS


def test_a_recorded_tool_call_may_not_change_what_it_is() -> None:
    """Antigravity review, F2 and F3, shared by both adapters. Only the identity
    fields are recorded, an update may repeat or omit them, and one that changes or
    adds any of them is refused, as is a call that cannot be followed."""
    first = {"toolCallId": "a", "kind": "read", "title": "ls", "status": "pending"}
    recorded = acp.identity(first)
    assert recorded == {"kind": "read", "title": "ls"}
    calls = {"a": recorded}
    assert acp.identity_refusal("a", {"kind": "read", "status": "done"}, calls) is None
    assert acp.identity_refusal("a", {"status": "done"}, calls) is None
    assert acp.identity_refusal("b", {"kind": "execute"}, calls) is None
    for key in acp.IDENTITY_FIELDS:
        refusal = acp.identity_refusal("a", {key: "other value"}, calls)
        assert refusal is not None and f"changed the {key}" in refusal
    for identifier in (None, "", 7):
        refusal = acp.identity_refusal(identifier, first, calls)
        assert refusal == "the engine reported a tool call without an identifier"


def test_every_model_value_of_an_update_is_found() -> None:
    """Antigravity review, F5. The schema's shape, the request's shape and malformed
    values are all returned, so that a caller can refuse any that differs."""
    option: dict[str, Any] = {"id": "model", "currentValue": "b", "options": []}
    assert acp.model_values({"configOptions": [option]}) == ["b"]
    assert acp.model_values({"configId": "model", "value": "c"}) == ["c"]
    assert acp.model_values({"configId": "model"}) == [None]
    assert acp.model_values({"models": {"currentModelId": "a"}}) == ["a"]
    assert acp.model_values({"configOptions": "x", "models": 3}) == [3, "x"]
    wrapped = {"id": "model", "currentValue": {"value": "d"}}
    assert acp.model_values({"configOptions": [wrapped]}) == ["d"]
    other = {"configOptions": [{"id": "mode", "currentValue": "yolo"}, 5]}
    assert acp.model_values(other) == []
    assert acp.model_values({}) == []


def test_model_choices_read_flat_and_grouped_options() -> None:
    option = {
        "id": "model",
        "options": [
            {"value": "a", "name": "A"},
            {"group": "g", "name": "G", "options": [{"value": "b", "name": "B"}]},
        ],
    }
    models = {"availableModels": [{"modelId": "c", "name": "C"}]}
    assert acp.model_choices({"configOptions": [option], "models": models}) == {
        "a",
        "b",
        "c",
    }
    assert acp.model_choices({}) == frozenset()


def test_the_mcp_server_carries_the_token_only_in_its_request() -> None:
    assert acp.mcp_servers(["b", "--line", "x"], "tok")[0]["env"] == [
        {"name": "DROMON_LINE_TOKEN", "value": "tok"}
    ]
    assert acp.mcp_servers([], "tok") == []
    assert acp.mcp_servers(["b"], None) == []
    assert acp.own_tool_names(["send_message"]) == {"dromon__send_message"}


class Scripted:
    def __init__(self, *messages: dict[str, Any] | None) -> None:
        self.messages: deque[dict[str, Any] | None] = deque(messages)
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: Mapping[str, object]) -> None:
        self.sent.append(dict(message))

    async def receive(self) -> dict[str, Any] | None:
        if self.messages:
            return self.messages.popleft()
        await asyncio.sleep(3600)
        return None


def test_a_call_dispatches_what_arrives_before_its_answer() -> None:
    seen: list[dict[str, Any]] = []

    async def dispatch(message: dict[str, Any]) -> None:
        seen.append(message)

    note: dict[str, Any] = {"jsonrpc": "2.0", "method": "session/update", "params": {}}
    transport = Scripted(note, {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}})
    client = acp.Client(transport, dispatch)
    assert asyncio.run(client.call("m", {})) == {"x": 1}
    assert seen == [note]
    assert transport.sent[0]["method"] == "m" and transport.sent[0]["id"] == 1


def test_errors_closing_and_the_deadline() -> None:
    async def ignore(message: dict[str, Any]) -> None:
        del message

    failing = acp.Client(
        Scripted({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}), ignore
    )
    with pytest.raises(acp.RpcError) as error:
        asyncio.run(failing.call("authenticate", {}))
    assert error.value.code == -32000
    shapeless = acp.Client(Scripted({"jsonrpc": "2.0", "id": 1, "error": "no"}), ignore)
    with pytest.raises(acp.RpcError) as error:
        asyncio.run(shapeless.call("m", {}))
    assert error.value.code is None
    with pytest.raises(EngineRefused, match="closed its connection"):
        asyncio.run(acp.Client(Scripted(None), ignore).call("m", {}))

    async def late() -> None:
        client = acp.Client(Scripted(), ignore)
        client.deadline = asyncio.get_running_loop().time() + 0.01
        await client.call("m", {})

    with pytest.raises(TimeoutError):
        asyncio.run(late())


def test_a_request_dromon_does_not_serve_is_answered_with_an_error() -> None:
    async def ignore(message: dict[str, Any]) -> None:
        del message

    transport = Scripted()
    asyncio.run(acp.Client(transport, ignore).refuse("r", "fs/read_text_file"))
    assert transport.sent == [
        {
            "jsonrpc": "2.0",
            "id": "r",
            "error": {
                "code": acp.METHOD_NOT_FOUND,
                "message": "Dromon does not serve fs/read_text_file",
            },
        }
    ]
