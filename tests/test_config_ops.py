"""The daemon's settings operations: settings, set_setting, clear_session and
reload_project, driven in process without sockets."""

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import ENGINE_OF, Strategy
from dromon.eventlog import EventLog
from dromon.limits import DEFAULT_LIMITS
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig, config_path, load
from dromon.protocol import ErrorCode, Response
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class Client:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    def ask(self, op: str, args: dict[str, object] | None = None) -> Response:
        line = {"id": "r", "op": op, "v": 1, "args": args or {}}
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def ok(self, op: str, args: dict[str, object] | None = None) -> dict[str, object]:
        response = self.ask(op, args)
        assert response.ok, response.error_message
        assert response.result is not None
        return response.result

    def entry(self, scope: str, key: str) -> dict[str, object]:
        result = self.ok("settings", {"line_id": "alpha"})
        entries = cast(list[dict[str, object]], result[scope])
        return next(e for e in entries if e["key"] == key)


def write_file(paths: ProjectPaths, text: str) -> None:
    config_path(paths.state_dir).write_text("version = 1\n" + text, encoding="utf-8")


def loaded(paths: ProjectPaths, text: str) -> ProjectConfig:
    write_file(paths, text)
    return load(config_path(paths.state_dir))


@contextmanager
def attached(
    paths: ProjectPaths,
    project: ProjectConfig | None = None,
    command_line: Overrides | None = None,
) -> Generator[Client]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        if "alpha" not in log.state.lines:
            scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon = Daemon(
            paths,
            log,
            "2026-10-25T00:00:00Z",
            scheduler,
            project=project or ProjectConfig(),
            command_line=command_line or Overrides(),
            project_file_present=config_path(paths.state_dir).exists(),
        )
        daemon.apply_project_values()
        yield Client(daemon)


def test_settings_report_each_value_with_its_layer(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        result = client.ok("settings", {"line_id": "alpha"})
        assert result["line_id"] == "alpha" and HAIKU in cast(
            list[str], result["models"]
        )
        model = client.entry("line", "model")
        assert (model["value"], model["source"]) == (HAIKU, "created")
        assert model["choices"] == ["claude-opus-5-5", HAIKU, "claude-fable-5-1"]
        effort = client.entry("line", "effort")
        assert effort["source"] == "driver" and effort["editable"] is True
        strategy = client.entry("line", "strategy")
        assert (strategy["kind"], strategy["editable"]) == ("info", False)
        cost = client.entry("line", "limits.max_cost_usd")
        assert (cost["value"], cost["source"]) == ("2", "project, default")
        operator = client.entry("project", "daemon.operator")
        assert (operator["value"], operator["source"]) == ("operator", "default")
        assert "line" not in client.ok("settings")


def test_mcp_servers_are_shown_by_name_only(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        config = {
            "mcp_servers": {
                "notes": {
                    "type": "stdio",
                    "command": "notes",
                    "env": {"NOTES_TOKEN": "secret-value"},
                }
            }
        }
        client.ok("configure_line", {"line_id": "alpha", "config": config})
        text = json.dumps(client.ok("settings", {"line_id": "alpha"}))
        assert "notes" in text and "secret-value" not in text


def test_a_line_setting_is_set_reported_and_unset(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        result = client.ok(
            "set_setting",
            {"scope": "line", "line_id": "alpha", "key": "effort", "value": "high"},
        )
        assert result == {
            "scope": "line",
            "line_id": "alpha",
            "key": "effort",
            "value": "high",
            "source": "override",
            "shadowed": False,
        }
        client.ok(
            "set_setting",
            {
                "scope": "line",
                "line_id": "alpha",
                "key": "model",
                "value": "claude-opus-5-5",
            },
        )
        status = client.ok("status")
        (line,) = cast(list[dict[str, object]], status["lines"])
        assert line["engine"] == "claude-opus-5-5"
        cleared = client.ok(
            "set_setting", {"scope": "line", "line_id": "alpha", "key": "effort"}
        )
        assert cleared["source"] == "driver" and cleared["shadowed"] is False


def test_line_overrides_survive_a_restart(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        client.ok(
            "set_setting",
            {
                "scope": "line",
                "line_id": "alpha",
                "key": "builtin_tools",
                "value": ["Read"],
            },
        )
    with attached(paths) as client:
        entry = client.entry("line", "builtin_tools")
        assert (entry["value"], entry["source"]) == (["Read"], "override")


def test_a_project_setting_applies_and_the_command_line_shadows_it(
    paths: ProjectPaths,
) -> None:
    with attached(paths) as client:
        result = client.ok(
            "set_setting",
            {"scope": "project", "key": "limits.max_cost_usd", "value": "7.5"},
        )
        assert (result["value"], result["source"], result["shadowed"]) == (
            "7.5",
            "override",
            False,
        )
        assert client.daemon.scheduler.limits_for("alpha").max_cost_usd == 7.5
        line_cost = client.entry("line", "limits.max_cost_usd")
        assert line_cost["source"] == "project, override"
    flagged = Overrides(max_cost_usd=3.0)
    with attached(paths, command_line=flagged) as client:
        assert client.daemon.scheduler.limits_for("alpha").max_cost_usd == 3.0
        again = client.ok(
            "set_setting",
            {"scope": "project", "key": "limits.max_cost_usd", "value": "9"},
        )
        assert (again["value"], again["source"], again["shadowed"]) == (
            "3",
            "command line",
            True,
        )


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (
            {"scope": "line", "line_id": "alpha", "key": "effort", "value": "extreme"},
            ErrorCode.REJECTED,
        ),
        (
            {"scope": "line", "line_id": "alpha", "key": "colour", "value": "red"},
            ErrorCode.REJECTED,
        ),
        (
            {
                "scope": "line",
                "line_id": "alpha",
                "key": "model",
                "value": "gpt-6-luna",
            },
            ErrorCode.REJECTED,
        ),
        (
            {"scope": "line", "line_id": "nobody", "key": "effort", "value": "low"},
            ErrorCode.REJECTED,
        ),
        ({"scope": "line", "key": "effort", "value": "low"}, ErrorCode.BAD_REQUEST),
        (
            {
                "scope": "project",
                "line_id": "alpha",
                "key": "wake.every_ticks",
                "value": 2,
            },
            ErrorCode.BAD_REQUEST,
        ),
        ({"scope": "galaxy", "key": "effort", "value": "low"}, ErrorCode.BAD_REQUEST),
        (
            {"scope": "project", "key": "daemon.tick_seconds", "value": "99"},
            ErrorCode.REJECTED,
        ),
        (
            {"scope": "project", "key": "daemon.operator", "value": "not an id"},
            ErrorCode.REJECTED,
        ),
        ({"scope": "project", "key": 3}, ErrorCode.BAD_REQUEST),
        ({"scope": "project", "key": "effort", "extra": 1}, ErrorCode.BAD_REQUEST),
    ],
)
def test_bad_settings_are_refused_and_change_nothing(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode
) -> None:
    with attached(paths) as client:
        before = client.daemon.log.last_seq
        response = client.ask("set_setting", args)
        assert (response.ok, response.error_code) == (False, code)
        assert client.daemon.log.last_seq == before


def test_clear_session_starts_afresh_and_refuses_unknown_lines(
    paths: ProjectPaths,
) -> None:
    with attached(paths) as client:
        assert client.ok("clear_session", {"line_id": "alpha"}) == {"line_id": "alpha"}
        assert client.entry("line", "session")["value"] == "new"
        unknown = client.ask("clear_session", {"line_id": "nobody"})
        assert (unknown.ok, unknown.error_code) == (False, ErrorCode.REJECTED)


def test_reload_applies_lines_and_limits_from_the_file(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        write_file(
            paths,
            '[limits]\nmax_cost_usd = 4.0\n\n[lines.beta]\nmodel = "claude-haiku-5-5"\n'
            'effort = "low"\n',
        )
        result = client.ok("reload_project")
        assert result["present"] is True and result["lines"] == ["beta"]
        assert "beta" in client.daemon.log.state.lines
        assert client.daemon.scheduler.limits_for("alpha").max_cost_usd == 4.0
        assert (
            client.entry("project", "limits.max_cost_usd")["source"] == "project file"
        )


def test_a_reload_that_needs_a_restart_or_is_invalid_changes_nothing(
    paths: ProjectPaths,
) -> None:
    project = loaded(paths, "[limits]\nmax_cost_usd = 4.0\n")
    with attached(paths, project=project) as client:
        before = client.daemon.log.last_seq
        write_file(paths, "[limits]\nmax_cost_usd = 5.0\n[sandbox]\nenabled = true\n")
        moved = client.ask("reload_project")
        assert not moved.ok and "restarts" in (moved.error_message or "")
        write_file(paths, "[limits\n")
        broken = client.ask("reload_project")
        assert not broken.ok and "dromon.toml" in (broken.error_message or "")
        write_file(paths, '[lines.alpha]\nmodel = "claude-opus-5-5"\n')
        mismatch = client.ask("reload_project")
        assert not mismatch.ok and "create a new line" in (mismatch.error_message or "")
        assert client.daemon.log.last_seq == before
        assert client.daemon.scheduler.limits_for("alpha").max_cost_usd == 4.0
        assert (
            client.ask("reload_project", {"x": 1}).error_code is ErrorCode.BAD_REQUEST
        )


def test_a_line_dropped_from_the_file_returns_to_the_project_policy(
    paths: ProjectPaths,
) -> None:
    project = loaded(
        paths,
        '[lines.alpha]\nmodel = "claude-haiku-5-5"\n'
        "[lines.alpha.limits]\nmax_cost_usd = 6.0\n",
    )
    with attached(paths, project=project) as client:
        from dromon.daemon import apply_project

        apply_project(client.daemon.scheduler, client.daemon.log, project)
        assert client.daemon.scheduler.limits_for("alpha").max_cost_usd == 6.0
        write_file(paths, "")
        client.ok("reload_project")
        assert client.daemon.scheduler.limits_for("alpha") == DEFAULT_LIMITS


def test_project_overrides_survive_a_restart(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        client.ok(
            "set_setting", {"scope": "project", "key": "wake.every_ticks", "value": 3}
        )
    with attached(paths) as client:
        assert client.daemon.scheduler.wake_every_for("alpha") == 3
        client.ok("set_setting", {"scope": "project", "key": "wake.every_ticks"})
        assert client.daemon.scheduler.wake_every_for("alpha") == 1


def test_stored_project_overrides_that_do_not_apply_are_skipped(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon.model import ProjectOverridden

    with attached(paths) as client:
        client.daemon.log.append(
            ProjectOverridden('{"daemon.tick_seconds":"99","colour":"red"}')
        )
    with attached(paths) as client:
        assert client.daemon.scheduler.limits_for("alpha") == DEFAULT_LIMITS
    reported = capsys.readouterr().err
    assert "colour" in reported and "tick_seconds" in reported


def test_a_reload_that_changes_the_peers_needs_a_restart(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        write_file(paths, '[peers.k]\nsocket = "/srv/k/run/peers.sock"\n')
        moved = client.ask("reload_project")
        assert not moved.ok and "federation" in (moved.error_message or "")
        assert "restarts" in (moved.error_message or "")


def test_a_reload_that_changes_git_ssh_needs_a_restart(paths: ProjectPaths) -> None:
    """The agent was resolved and handed to the engine factory at start, so neither
    the table nor the lines it reaches may change without a restart."""
    table = "[git_ssh]\nagent = 'inherit'\nknown_hosts = 'k'\npublic_key = 'p'\n"
    alpha = f"[lines.alpha]\nmodel = {HAIKU!r}\n"
    with attached(paths, project=loaded(paths, table + alpha)) as client:
        before = client.daemon.log.last_seq
        write_file(paths, table + alpha + "git_ssh = true\n")
        widened = client.ask("reload_project")
        assert not widened.ok
        assert "the lines given git_ssh" in (widened.error_message or "")
        write_file(paths, alpha)
        dropped = client.ask("reload_project")
        assert not dropped.ok and "git_ssh" in (dropped.error_message or "")
        assert "restarts" in (dropped.error_message or "")
        assert client.daemon.log.last_seq == before
        write_file(paths, table + alpha)
        client.ok("reload_project")


def test_an_unconfined_line_is_not_created_beside_peers(paths: ProjectPaths) -> None:
    project = loaded(paths, '[peers.k]\nsocket = "/srv/k/run/peers.sock"\n')
    pi = next(model for model, engine in ENGINE_OF.items() if engine == "pi")
    with attached(paths, project=project) as client:
        before = client.daemon.log.last_seq
        refused = client.ask(
            "create_line", {"line_id": "p", "model": pi, "strategy": "subscription"}
        )
        assert not refused.ok and "cannot confine" in (refused.error_message or "")
        assert client.daemon.log.last_seq == before
