"""Row 16 of the T3 Code review. ``dromon probe --no-turn`` checks an engine's sign-in
without a turn, where the engine offers a check, and Dromon reads no credential store.
Every check here is scripted, so no engine starts."""

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dromon import capabilities, cli, exits, pi
from dromon.driver import Credential, Driver, Strategy
from dromon.engine import EngineRefused

PI = "pi:openai/gpt-6-luna"
HAIKU = "claude-haiku-5-5"


class Checks:
    """A scripted ``pi auth check`` that notes what it was asked."""

    def __init__(self, answer: Mapping[str, object]) -> None:
        self.answer = dict(answer)
        self.asked: list[tuple[str, dict[str, str]]] = []

    async def __call__(
        self, provider: str, environment: Mapping[str, str]
    ) -> Mapping[str, object]:
        self.asked.append((provider, dict(environment)))
        return self.answer


def ready(kind: str = "oauth") -> dict[str, object]:
    return {"status": "ready", "provider": "openai", "authType": kind}


def locked_home(tmp_path: Path) -> Path:
    """A home whose Pi credential store cannot be opened, so a read of it fails."""
    agent = tmp_path / pi.AGENT_DIR
    agent.mkdir(parents=True)
    store = agent / "auth.json"
    store.write_text("{}")
    store.chmod(0)
    return tmp_path


def test_pi_s_check_runs_in_a_turn_s_environment_and_reads_no_store(
    tmp_path: Path,
) -> None:
    home = locked_home(tmp_path)
    checks = Checks(ready())
    credential = asyncio.run(
        pi.check_sign_in(
            Driver.for_model(PI, Strategy.SUBSCRIPTION),
            {"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "sk-not-sent"},
            home,
            checks,
        )
    )
    assert credential is Credential.SUBSCRIPTION
    ((provider, environment),) = checks.asked
    assert provider == "openai"
    assert environment["PI_CODING_AGENT_DIR"] == str(home / pi.AGENT_DIR)
    assert (environment["PI_OFFLINE"], environment["PI_TELEMETRY"]) == ("1", "0")
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        (ready("api_key"), "refuses the api credential"),
        ({"status": "missing", "provider": "openai"}, "no usable credential"),
        ({**ready(), "provider": "other"}, "answered for 'other'"),
        (ready("token"), "cannot tell"),
    ],
)
def test_pi_s_check_refuses_what_the_driver_would(
    tmp_path: Path, answer: dict[str, object], match: str
) -> None:
    with pytest.raises(EngineRefused, match=match):
        asyncio.run(
            pi.check_sign_in(
                Driver.for_model(PI, Strategy.SUBSCRIPTION),
                {},
                locked_home(tmp_path),
                Checks(answer),
            )
        )


def test_pi_s_check_is_only_for_pi() -> None:
    with pytest.raises(EngineRefused, match="not a Pi model"):
        asyncio.run(
            pi.check_sign_in(
                Driver.for_model(HAIKU, Strategy.SUBSCRIPTION), {}, None, Checks({})
            )
        )


def test_the_checks_follow_the_tables() -> None:
    offered = {
        engine
        for engine, table in capabilities.TABLES.items()
        if table.sign_in_check is capabilities.Known.YES
    }
    assert set(cli.SIGN_IN_CHECKS) == offered
    unverified = {
        engine
        for engine, table in capabilities.TABLES.items()
        if table.sign_in_check is capabilities.Known.UNKNOWN
    }
    assert set(cli.UNVERIFIED_SIGN_IN_CHECKS) <= unverified
    assert pi.ENGINE_NAME in cli.UNVERIFIED_SIGN_IN_CHECKS


def no_engine(*_: Any, **__: Any) -> Any:  # noqa: ANN401
    raise AssertionError("--no-turn must build no engine")


@pytest.fixture
def no_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "engine_factory", no_engine)


@pytest.mark.usefixtures("no_turns")
def test_no_turn_on_an_engine_without_a_check_checks_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["probe", "--model", HAIKU, "--no-turn"]) == exits.FAILURE
    error = capsys.readouterr().err
    assert "Claude Code offers no sign-in check that Dromon knows" in error
    assert "its table says unknown" in error


@pytest.mark.usefixtures("no_turns")
def test_pi_s_unverified_check_runs_only_when_accepted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-27, C1. Pi was never run live, so its table says unknown,
    and its check runs only behind its own flag, which says the answer is
    unverified."""

    async def signed_in(driver: Driver) -> Credential:
        assert driver.settings.model == PI
        return Credential.SUBSCRIPTION

    monkeypatch.setitem(cli.UNVERIFIED_SIGN_IN_CHECKS, pi.ENGINE_NAME, signed_in)
    assert cli.main(["probe", "--model", PI, "--no-turn"]) == exits.FAILURE
    error = capsys.readouterr().err
    assert "its table says unknown" in error
    assert "runs with --accept-unverified-check" in error
    accepted = ["probe", "--model", PI, "--no-turn", "--accept-unverified-check"]
    assert cli.main(accepted) == exits.OK
    out = capsys.readouterr().out
    assert "signed in with a subscription credential" in out
    assert "No turn was run" in out
    assert "never run live, so its answer is unverified" in out


@pytest.mark.usefixtures("no_turns")
def test_the_unverified_flag_needs_no_turn(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["probe", "--model", PI, "--accept-unverified-check"]) == (
        exits.FAILURE
    )
    assert "only together with --no-turn" in capsys.readouterr().err


@pytest.mark.usefixtures("no_turns")
def test_the_unverified_flag_finds_no_check_where_none_was_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = ["probe", "--model", HAIKU, "--no-turn", "--accept-unverified-check"]
    assert cli.main(command) == exits.FAILURE
    error = capsys.readouterr().err
    assert "Claude Code offers no sign-in check" in error
    assert "--accept-unverified-check" not in error


@pytest.mark.usefixtures("no_turns")
def test_a_failed_check_fails_the_probe(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(driver: Driver) -> Credential:
        raise EngineRefused("Pi has no usable credential for openai")

    monkeypatch.setitem(cli.UNVERIFIED_SIGN_IN_CHECKS, pi.ENGINE_NAME, refused)
    command = ["probe", "--model", PI, "--no-turn", "--accept-unverified-check"]
    assert cli.main(command) == exits.FAILURE
    assert "sign-in check failed: Pi has no usable" in capsys.readouterr().err


@pytest.mark.usefixtures("no_turns")
def test_no_turn_takes_no_recording(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["probe", "--model", PI, "--no-turn", "--record"]) == (
        exits.FAILURE
    )
    assert "takes no --record" in capsys.readouterr().err
