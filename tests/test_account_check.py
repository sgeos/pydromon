"""Account diagnostics run against scripted connections without a provider."""

import asyncio
from collections.abc import Sequence
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, daemon as daemon_module, exits
from dromon.codex import AccountCheckError, Connection
from dromon.daemon import Daemon
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.model import LineCreated
from dromon.paths import project_paths
from dromon.protocol import Operation, Request, Response, encode_request
from dromon.scheduler import LineEngine, LineSpec, Scheduler
from test_codex import FakeServer, line, LUNA


@pytest.mark.parametrize("keep", [False, True])
def test_account_check_never_touches_a_thread_or_session(keep: bool) -> None:
    server = FakeServer(
        account={
            "type": "chatgpt",
            "planType": "pro",
            "email": "private",
            "id": "private",
        }
    )
    target = line(
        server,
        config=LineConfig(keep_session=keep),
        session_id="saved",
        environment={"CODEX_HOME": "/account-home"},
    )
    assert asyncio.run(target.check_account()) == {"type": "chatgpt", "plan": "pro"}
    assert server.methods() == ["initialize", "initialized", "account/read"]
    assert target.session_id == "saved"
    ordinary = FakeServer()
    asyncio.run(
        line(
            ordinary,
            config=LineConfig(keep_session=keep),
            environment={"CODEX_HOME": "/account-home"},
        ).check_start()
    )
    assert server.environment == ordinary.environment
    assert server.arguments == ordinary.arguments


def test_account_check_does_not_reflect_unknown_plan() -> None:
    server = FakeServer(account={"type": "chatgpt", "planType": "private"})
    assert asyncio.run(line(server).check_account())["plan"] == "unknown"


def test_account_error_drops_private_error_data() -> None:
    server = FakeServer(
        failing="account/read",
        failing_error={"code": -32603, "message": "public", "data": "private"},
    )
    with pytest.raises(AccountCheckError) as found:
        asyncio.run(line(server).check_account())
    assert found.value.code == -32603
    assert found.value.message == "public"
    assert "private" not in str(found.value)
    assert "thread/start" not in server.methods()


@pytest.mark.parametrize(
    "mode", ["success", "error", "private", "disclosure", "timeout"]
)
def test_operator_check_is_private_and_preserves_cached_engine(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    with EventLog(tmp_path / "events.sqlite3") as log:
        log.append(LineCreated("alpha", LUNA, "subscription"))
        text = (
            "private@example.invalid account=private"
            if mode == "private"
            else "failed to load workspace requirements: invalid data"
        )
        server = FakeServer(
            failing=None if mode == "success" else "account/read",
            failing_error={"code": -32603, "message": text, "data": "private"},
        )
        target = line(server)
        cached = line(FakeServer(), session_id="saved")
        builds: list[LineEngine] = []

        def factory(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
            del spec, tools
            result = cached if not builds else target
            builds.append(result)
            return result

        scheduler = Scheduler(log, factory)
        assert scheduler.engine_for("alpha") is cached
        daemon = Daemon(paths, log, "2026-11-22T00:00:00Z", scheduler)
        if mode == "disclosure":

            def discloses(*texts: str) -> bool:
                return bool(texts)

            monkeypatch.setattr(scheduler, "discloses", discloses)
        if mode == "timeout":

            async def timeout() -> dict[str, str]:
                await asyncio.sleep(10)
                raise AssertionError("must time out")

            monkeypatch.setattr(target, "check_account", timeout)
            monkeypatch.setattr(daemon_module, "ENGINE_CHECK_SECONDS", 0.01)
        response = asyncio.run(
            daemon.respond_operator(
                encode_request(Request("r", Operation.ACCOUNT_CHECK, {"line": "alpha"}))
            )
        )
        assert response.ok
        result = response.result or {}
        assert result["passed"] is (mode == "success")
        assert "private" not in str(response)
        if mode == "error":
            assert result == {"passed": False, "code": -32603, "message": text}
        if mode == "disclosure":
            assert result["message"] == "withheld by the disclosure list"
        if mode == "timeout":
            assert result["message"] == "account check timed out"
        assert scheduler.engine_for("alpha") is cached
        assert cached.session_id == "saved"


def test_account_cli_sends_only_the_named_line(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def call(*args: object, **kwargs: object) -> Response:
        assert args[1] is Operation.ACCOUNT_CHECK
        assert kwargs["args"] == {"line": "alpha"}
        return Response(
            "r", True, result={"passed": True, "type": "chatgpt", "plan": "pro"}
        )

    monkeypatch.setattr(cli, "call", call)
    assert (
        cli.main(
            [
                "--project",
                str(tmp_path),
                "--runtime-dir",
                str(short_runtime),
                "check-engines",
                "--account",
                "alpha",
            ]
        )
        == exits.OK
    )
    assert capsys.readouterr().out == "account type chatgpt, plan pro\n"


@pytest.mark.parametrize(
    "case", ["missing", "unsupported", "busy", "malformed", "line-socket"]
)
def test_account_request_boundaries(
    tmp_path: Path, short_runtime: Path, case: str
) -> None:
    with EventLog(tmp_path / "events.sqlite3") as log:
        log.append(
            LineCreated(
                "alpha",
                "claude-opus-5-5" if case == "unsupported" else LUNA,
                "subscription",
            )
        )

        def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
            raise AssertionError("no engine may be built")

        scheduler = Scheduler(log, no_engine)
        daemon = Daemon(
            project_paths(tmp_path, short_runtime),
            log,
            "2026-11-22T00:00:00Z",
            scheduler,
        )
        if case == "busy":
            # An overlapping operator request must not launch another process.
            daemon._account_check_busy = True  # pyright: ignore[reportPrivateUsage]
        args: dict[str, object] = (
            {}
            if case == "malformed"
            else {"line": "absent" if case == "missing" else "alpha"}
        )
        wire = encode_request(Request("r", Operation.ACCOUNT_CHECK, args))
        response = asyncio.run(
            daemon.respond_line(wire)
            if case == "line-socket"
            else daemon.respond_operator(wire)
        )
        assert not response.ok


def test_account_diagnostic_refuses_unsolicited_actions() -> None:
    server = FakeServer()
    server.queue.append(
        {"id": "tool", "method": "item/tool/call", "params": {"tool": "record"}}
    )
    from dromon.engine import EngineRefused

    with pytest.raises(EngineRefused, match="requested an action"):
        asyncio.run(line(server).check_account())
    assert "turn/start" not in server.methods()


def test_ordinary_account_failure_is_currently_a_generic_refusal() -> None:
    from dromon.engine import EngineRefused

    server = FakeServer(
        failing="account/read",
        failing_error={
            "code": -32603,
            "message": "failed to load workspace requirements: invalid data",
        },
    )
    with pytest.raises(EngineRefused, match="refused a request"):
        asyncio.run(line(server).run_turn("task"))
    assert "turn/start" not in server.methods()


def test_account_timeout_closes_connection_and_preserves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer()
    target = line(server, session_id="saved")
    closed: list[bool] = []

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, environment: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, environment
        try:
            yield server
        finally:
            closed.append(True)

    async def receive() -> dict[str, Any] | None:
        await asyncio.sleep(10)
        return None

    monkeypatch.setattr(target, "_connect", connect)
    monkeypatch.setattr(server, "receive", receive)

    async def check() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await target.check_account()

    asyncio.run(check())
    assert closed == [True]
    assert target.session_id == "saved"
