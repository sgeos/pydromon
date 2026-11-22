"""Exercise actual adapter readers with subprocess pipes and production limits."""

import asyncio
import contextlib
import sys
from typing import Literal

import pytest

from dromon import codex, evidence, grok, jsonlines, spawn
from dromon.engine import EngineRefused


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (b'{"id":7,"result":{"large":"', (7, None, True)),
        (b'{"method":"notice","params":{"id":7,"large":"', (None, "notice", False)),
        (b'{"result":{"id":7,"large":"', (None, None, True)),
        (b'{"id":123', (None, None, False)),
        (b'{"id":7,"id":8,"result":', (None, None, False)),
    ],
)
def test_frame_identity_uses_only_complete_root_fields(
    prefix: bytes, expected: tuple[int | str | None, str | None, bool]
) -> None:
    assert codex.frame_identity(prefix) == expected


@pytest.mark.parametrize("notification", [False, True])
def test_discarded_frame_identity_through_process_reader(
    monkeypatch: pytest.MonkeyPatch, notification: bool
) -> None:
    monkeypatch.setattr(codex, "MAX_LINE", 64)

    async def exercise() -> None:
        header = '{"method":"notice","params":' if notification else '{"id":7,"result":'
        process = await spawn.subprocess_exec(
            sys.executable,
            "-c",
            'import sys; sys.stdout.write(sys.argv[1]+\'{"text":"\''
            "+'x'*1024+'\"}}\\n')",
            header,
            stdout=asyncio.subprocess.PIPE,
        )
        connection = codex._Process(process)  # pyright: ignore[reportPrivateUsage]
        try:
            with pytest.raises(EngineRefused) as caught:
                await connection.receive()
            from dromon.engine import EngineStreamTooLong

            assert isinstance(caught.value, EngineStreamTooLong)
            assert caught.value.frame_id == (None if notification else 7)
            assert caught.value.frame_method == ("notice" if notification else None)
            assert caught.value.frame_is_response is not notification
        finally:
            await process.communicate()

    asyncio.run(exercise())


@pytest.mark.parametrize("adapter", ["codex", "grok", "jsonlines"])
@pytest.mark.parametrize("oversize", [False, True])
def test_adapter_pipe_names_overrun(
    adapter: Literal["codex", "grok", "jsonlines"], oversize: bool
) -> None:
    async def exercise() -> None:
        size = codex.MAX_LINE + 1 if oversize else 65537
        process = await spawn.subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b' ' * int(sys.argv[1]) + b'{}\\n')",
            str(size),
            stdout=asyncio.subprocess.PIPE,
            limit=codex.MAX_LINE,
        )
        connection = (
            codex._Process(process)  # pyright: ignore[reportPrivateUsage]
            if adapter == "codex"
            else grok._Process(process)  # pyright: ignore[reportPrivateUsage]
            if adapter == "grok"
            else jsonlines.Process(process)
        )
        try:
            if oversize:
                with pytest.raises(
                    EngineRefused, match="ENGINE_STREAM_TOO_LONG"
                ) as caught:
                    await connection.receive()
                assert str(codex.MAX_LINE) in str(caught.value)
                assert "stdout" in str(caught.value)
                assert "bytes" in str(caught.value)
            else:
                assert await connection.receive() == {}
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()

    asyncio.run(exercise())


def test_default_pipe_limit_does_not_limit_stderr_lines() -> None:
    async def exercise() -> None:
        process = await spawn.subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(b'x' * 65537)",
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stderr is not None
        await evidence.drain_stderr(process.stderr, None)
        assert await process.wait() == 0

    asyncio.run(exercise())


def test_legacy_readline_raises_the_reported_error_at_its_own_limit() -> None:
    async def exercise() -> None:
        reader = asyncio.StreamReader(limit=codex.MAX_LINE)
        reader.feed_data(b"x" * (codex.MAX_LINE + 1))
        with pytest.raises(ValueError, match="Separator is not found"):
            await reader.readline()

    asyncio.run(exercise())


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_codex_exact_frame_boundary_and_drain(delta: int) -> None:
    async def exercise() -> None:
        size = codex.MAX_LINE + delta
        process = await spawn.subprocess_exec(
            sys.executable,
            "-c",
            "import sys; n=int(sys.argv[1]); "
            "sys.stdout.buffer.write(b' '*(n-2)+b'{}\\n{}\\n')",
            str(size),
            stdout=asyncio.subprocess.PIPE,
            limit=codex.MAX_LINE,
        )
        connection = codex._Process(process)  # pyright: ignore[reportPrivateUsage]
        try:
            if delta > 0:
                with pytest.raises(EngineRefused, match=f"exactly {size} bytes"):
                    await connection.receive()
            else:
                assert await connection.receive() == {}
            assert await connection.receive() == {}
        finally:
            await process.communicate()

    asyncio.run(exercise())


@pytest.mark.parametrize("stall", [False, True])
def test_codex_unterminated_overrun_is_bounded(
    monkeypatch: pytest.MonkeyPatch, stall: bool
) -> None:
    monkeypatch.setattr(codex, "MAX_LINE", 16)
    monkeypatch.setattr(codex, "FRAME_DRAIN_SECONDS", 0.01)

    async def exercise() -> None:
        process = await spawn.subprocess_exec(
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('x'*17); sys.stdout.flush(); "
            + ("time.sleep(10)" if stall else "pass"),
            stdout=asyncio.subprocess.PIPE,
        )
        connection = codex._Process(process)  # pyright: ignore[reportPrivateUsage]
        try:
            with pytest.raises(EngineRefused, match="at least 17 bytes"):
                await asyncio.wait_for(connection.receive(), 5)
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()

    asyncio.run(exercise())
