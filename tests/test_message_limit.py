"""The longest engine message the Agent SDK reads, and a turn that ends on one longer.

The SDK's default of 1 MiB ended a line's turn on a tool result that carried a large
file, and the turn said only that the engine's process ended. Dromon passes
``MAX_MESSAGE_CHARACTERS`` as the SDK's buffer size, and a turn whose engine still
sends a longer message ends with ``MESSAGE_TOO_LONG``, which names the limit and the
likely cause."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    CLIJSONDecodeError,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from dromon.driver import Driver, Strategy
from dromon.engine import (
    DIED_NOTE,
    MAX_MESSAGE_CHARACTERS,
    MESSAGE_TOO_LONG,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    KeptSession,
    message_too_long,
)
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig

MODEL = "claude-haiku-5-5"
KEPT = LineConfig(keep_session=True)


class Stdout:
    """The engine's standard output as the SDK's reader takes it, in chunks."""

    def __init__(self, *chunks: str) -> None:
        self.chunks = chunks

    def __aiter__(self) -> AsyncIterator[str]:
        return self._each()

    async def _each(self) -> AsyncIterator[str]:
        for chunk in self.chunks:
            yield chunk


def sdk_refusal(limit: int, length: int) -> CLIJSONDecodeError:
    """The error the SDK's own reader raises for a message of ``length`` characters
    past a buffer of ``limit``, so that the text Dromon matches is the SDK's.
    Porting hazard: it sets the transport's private stream, as the SDK's connect
    would, since the reader is reached only through a running engine."""
    transport = SubprocessCLITransport(
        prompt="", options=ClaudeAgentOptions(max_buffer_size=limit)
    )
    transport._process = object()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    transport._stdout_stream = Stdout("x" * length)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    async def read() -> CLIJSONDecodeError:
        with pytest.raises(CLIJSONDecodeError) as raised:
            async for _ in transport.read_messages():
                pass
        return raised.value

    return asyncio.run(read())


def init() -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": MODEL,
            "claude_code_version": "2.1.310",
            "apiKeySource": "none",
            "tools": [],
            "permissionMode": "default",
        },
    )


def result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
        total_cost_usd=0.001,
        result="done",
    )


def test_the_option_given_to_the_sdk_is_the_named_bound(tmp_path: Path) -> None:
    target = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION), tmp_path, environment={}
    )
    options = target.options()
    assert options.max_buffer_size == MAX_MESSAGE_CHARACTERS
    # The SDK's reader takes it as its limit, where its default would be 1 MiB.
    transport = SubprocessCLITransport(prompt="", options=options)
    assert transport._max_buffer_size == MAX_MESSAGE_CHARACTERS  # pyright: ignore[reportPrivateUsage]
    default = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())
    assert 10 * default._max_buffer_size < MAX_MESSAGE_CHARACTERS  # pyright: ignore[reportPrivateUsage]


def test_the_sdks_refusal_of_a_long_message_is_recognised() -> None:
    error = sdk_refusal(16, 40)
    assert message_too_long(error)
    assert "16" in error.line


def test_other_decode_errors_and_other_errors_are_not_taken_for_one() -> None:
    assert not message_too_long(CLIJSONDecodeError("{not json", ValueError("bad")))
    assert not message_too_long(EngineRefused("the engine is not connected"))
    assert not message_too_long(ValueError("JSON message exceeded maximum buffer size"))


def test_the_error_names_the_limit_and_the_likely_cause() -> None:
    assert str(MAX_MESSAGE_CHARACTERS) in MESSAGE_TOO_LONG
    assert "MAX_MESSAGE_CHARACTERS" in MESSAGE_TOO_LONG
    for cause in ("Edit", "Read", "large file"):
        assert cause in MESSAGE_TOO_LONG
    assert "process ended" not in MESSAGE_TOO_LONG


class Overflowing:
    """A one-shot session whose engine sends a message the SDK refuses, or another
    error when ``error`` is given."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[Message]:
        yield init()
        raise self.error

    async def interrupt(self) -> None:
        pass

    def factory(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[AgentSession]:
        del options

        @asynccontextmanager
        async def opened() -> AsyncGenerator[AgentSession]:
            yield self

        return opened()


def one_shot(session: Overflowing, tmp_path: Path) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=DEFAULT_CONFIG,
        environment={},
        session_factory=session.factory,
    )


def test_a_turn_that_ends_on_a_long_message_says_so(tmp_path: Path) -> None:
    target = one_shot(Overflowing(sdk_refusal(16, 40)), tmp_path)
    with pytest.raises(EngineRefused) as raised:
        asyncio.run(target.run_turn("edit the large file"))
    assert str(raised.value) == MESSAGE_TOO_LONG
    assert message_too_long(raised.value.__cause__ or ValueError())


def test_a_turn_that_ends_on_another_decode_error_raises_it_unchanged(
    tmp_path: Path,
) -> None:
    other = CLIJSONDecodeError("{not json", ValueError("bad"))
    target = one_shot(Overflowing(other), tmp_path)
    with pytest.raises(CLIJSONDecodeError) as raised:
        asyncio.run(target.run_turn("hello"))
    assert raised.value is other


class KeptConnection:
    """A kept engine whose ``turns`` say, prompt by prompt, either to answer or to
    send a message the SDK refuses, raised as ``error``."""

    def __init__(self, turns: list[str], error: Exception) -> None:
        self.turns = turns
        self.error = error
        self.prompts: list[str] = []
        self.stream: asyncio.Queue[Message | str] = asyncio.Queue()

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        turn = self.turns.pop(0)
        self.stream.put_nowait(init())
        self.stream.put_nowait(result() if turn == "answer" else turn)

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self.stream.get()
            if isinstance(message, str):
                raise self.error
            yield message

    async def interrupt(self) -> None:
        pass

    async def set_permission_mode(self, mode: Any) -> None:  # noqa: ANN401
        del mode


class KeptFactory:
    def __init__(self, turns: list[str], error: Exception) -> None:
        self.turns = turns
        self.error = error
        self.connections: list[KeptConnection] = []

    def __call__(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[KeptSession]:
        del options

        @asynccontextmanager
        async def connect() -> AsyncGenerator[KeptSession]:
            connection = KeptConnection(self.turns, self.error)
            self.connections.append(connection)
            yield connection

        return connect()


def kept(factory: KeptFactory, tmp_path: Path) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={"SHELL": "/bin/bash"},
        kept_factory=factory,
        poll_seconds=3600.0,
    )


def test_a_kept_turn_that_ends_on_a_long_message_says_so(tmp_path: Path) -> None:
    """The pump that reads a kept connection stops on the SDK's error, and the turn
    names it, where before it said only that the engine's process ended. The next
    turn starts a new engine, and its prompt says the old one ended."""
    factory = KeptFactory(["overflow", "answer"], sdk_refusal(16, 40))
    target = kept(factory, tmp_path)

    async def go() -> str:
        with pytest.raises(EngineRefused) as raised:
            await target.run_turn("edit the large file")
        report = await target.run_turn("again")
        await target.close()
        return str(raised.value) + "|" + str(report.session_id)

    failed, _, session = asyncio.run(go()).partition("|")
    assert failed == MESSAGE_TOO_LONG
    assert session == "session-1"
    assert len(factory.connections) == 2
    assert factory.connections[1].prompts[0].startswith(DIED_NOTE)


def test_a_kept_turn_that_ends_on_another_error_says_the_process_ended(
    tmp_path: Path,
) -> None:
    factory = KeptFactory(["overflow"], CLIJSONDecodeError("{not json", ValueError()))
    target = kept(factory, tmp_path)

    async def go() -> str:
        with pytest.raises(EngineRefused) as raised:
            await target.run_turn("hello")
        await target.close()
        return str(raised.value)

    assert asyncio.run(go()) == "the engine's process ended during the turn"
