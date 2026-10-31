"""The project's guards: ``require_sandbox`` for Claude Code lines, and the disclosure
list that keeps listed terms out of what lines and agents send."""

import asyncio
import json
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import daemon, disclosure, exits
from dromon.configview import Overrides
from dromon.daemon import Daemon, check_lines_sandboxed, check_sandbox_required
from dromon.disclosure import (
    MAX_TERMS_FILE_BYTES,
    DisclosureError,
    discloses,
    load_terms,
)
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig
from dromon.agents import AgentPolicy
from dromon.model import (
    WITHHELD_WAKE_NOTE,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    LineAddress,
    OperatorAddress,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import (
    ConfigFileError,
    ProjectConfig,
    config_path,
    load,
    parse,
)
from dromon.protocol import ErrorCode, Operation, Request, Response
from dromon.sandbox import SandboxConfig, SandboxError, parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
TERM = "Hidden Codename"
"""A term the tests list. Every refusal is checked not to contain it."""


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


# The project file.


def test_the_daemon_table_takes_both_guards() -> None:
    config = parse(
        "version = 1\n[daemon]\nrequire_sandbox = true\n"
        "disclosure_terms = 'secret/terms.txt'\n"
    )
    assert config.require_sandbox is True
    assert config.disclosure_terms == "secret/terms.txt"
    default = parse("version = 1\n")
    assert (default.require_sandbox, default.disclosure_terms) == (False, None)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("require_sandbox = 'yes'", "daemon.require_sandbox: must be a boolean"),
        ("require_sandbox = 1", "daemon.require_sandbox: must be a boolean"),
        ("disclosure_terms = ''", "daemon.disclosure_terms: must be a path"),
        ("disclosure_terms = 3", "daemon.disclosure_terms: must be a path"),
    ],
)
def test_bad_guard_settings_are_refused(text: str, message: str) -> None:
    with pytest.raises(ConfigFileError, match=message):
        parse(f"version = 1\n[daemon]\n{text}\n")


# The disclosure list.


def write_terms(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_terms_are_read_without_blanks_or_comments_and_case_folded(
    tmp_path: Path,
) -> None:
    file = write_terms(
        tmp_path / "terms.txt",
        f"# a comment\n\n   \n  # an indented comment\n{TERM}\nSTRASSE\nstrasse\r\n",
    )
    assert load_terms(file) == ("hidden codename", "strasse")


def test_terms_are_stripped_before_matching(tmp_path: Path) -> None:
    terms = load_terms(write_terms(tmp_path / "terms.txt", "  Private Name  \n"))
    assert terms == ("private name",)
    assert discloses(terms, ["PRIVATE NAME"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"about the {TERM.upper()} work", True),
        ("about the hidden codenames", True),
        ("die Straße", True),
        ("hidden  codename", False),
        ("nothing listed", False),
    ],
)
def test_terms_match_as_substrings_without_regard_to_case(
    tmp_path: Path, text: str, expected: bool
) -> None:
    terms = load_terms(write_terms(tmp_path / "t", f"{TERM}\nstrasse\n"))
    assert discloses(terms, [text]) is expected
    assert discloses(terms, ["clean", text]) is expected
    assert discloses((), [text]) is False


def test_an_unreadable_list_is_named_and_its_terms_never_shown(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    with pytest.raises(DisclosureError, match="missing.txt cannot be read"):
        load_terms(missing)
    with pytest.raises(DisclosureError, match="cannot be read"):
        load_terms(tmp_path)
    binary = tmp_path / "binary.txt"
    binary.write_bytes(TERM.encode() + b"\n\xff\xfe\n")
    with pytest.raises(DisclosureError, match="not UTF-8") as raised:
        load_terms(binary)
    assert TERM.casefold() not in str(raised.value).casefold()
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * (MAX_TERMS_FILE_BYTES + 1))
    with pytest.raises(DisclosureError, match="larger than"):
        load_terms(large)


# The tools.


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True)
        found.ensure_operator("ana")
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        log.append(AgentRegistered("probe", AgentAccess.MESSAGE, ""))
        found.set_disclosure_terms(load_terms(write_terms(tmp_path / "t", TERM)))
        yield found


async def invoke(tool: SdkMcpTool[Any], args: dict[str, object]) -> dict[str, Any]:
    return await tool.handler(args)


def call(tool: SdkMcpTool[Any], args: dict[str, object]) -> dict[str, Any]:
    return asyncio.run(invoke(tool, args))


def last_seq(scheduler: Scheduler) -> int:
    return scheduler._log.last_seq  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("tool_index", "args", "field"),
    [
        (0, {"to": "beta", "body": f"the {TERM.lower()} plan"}, "body"),
        (0, {"to": "@ana", "body": f"about {TERM}"}, "body"),
        (1, {"question": f"may I mention {TERM}?"}, "question"),
        (
            1,
            {"question": "which name?", "choices": ["the old one", TERM.upper()]},
            None,
        ),
        (2, {"seconds": 60, "note": f"recheck {TERM}"}, "note"),
    ],
)
def test_a_line_cannot_send_a_listed_term(
    scheduler: Scheduler, tool_index: int, args: dict[str, object], field: str | None
) -> None:
    tool = scheduler.own_tools("alpha")[tool_index]
    before = last_seq(scheduler)
    result = call(tool, args)
    text = cast(str, result["content"][0]["text"])
    assert result["is_error"] is True
    assert text == (
        disclosure.REFUSAL
        if field is None
        else f"the {field} field contains a term from the project's disclosure list"
    )
    assert TERM.casefold() not in text.casefold()
    # The refusal is recorded as one disclosure check, which holds no text, at the
    # operator's decision of 2026-10-28, so that it spends the line's limit.
    assert last_seq(scheduler) == before + 1
    state = scheduler._log.state  # pyright: ignore[reportPrivateUsage]
    assert len(state.disclosure_checks["alpha"]) == 1


def test_clean_text_still_passes_every_tool(scheduler: Scheduler) -> None:
    send, ask, wake = scheduler.own_tools("alpha")[:3]
    cases: list[tuple[SdkMcpTool[Any], dict[str, object]]] = [
        (send, {"to": "beta", "body": "the plan"}),
        (ask, {"question": "may I proceed?", "choices": ["yes"], "to": "@ana"}),
        (wake, {"seconds": 60, "note": "recheck"}),
    ]
    for tool, args in cases:
        assert call(tool, args).get("is_error") is not True


def test_a_refusal_names_the_field_only_for_a_call_with_one(
    scheduler: Scheduler,
) -> None:
    """Round 4 F1 permits a field name only for one offered text field."""
    ask = scheduler.own_tools("alpha")[1]
    several = call(
        ask,
        {"question": f"{TERM} question", "choices": [f"choice {TERM.upper()}"]},
    )
    assert several["is_error"] is True
    assert several["content"][0]["text"] == disclosure.REFUSAL
    one_matching_of_two = call(
        ask, {"question": "which name?", "choices": [f"choice {TERM}"]}
    )
    assert one_matching_of_two["content"][0]["text"] == disclosure.REFUSAL
    alone = call(ask, {"question": f"{TERM} question"})
    alone_text = cast(str, alone["content"][0]["text"])
    assert alone_text == (
        "the question field contains a term from the project's disclosure list"
    )
    assert TERM.casefold() not in alone_text.casefold()


def test_the_refusal_helper_names_one_field_of_one_offered() -> None:
    assert disclosure.refusal(("note",), 1) == (
        "the note field contains a term from the project's disclosure list"
    )
    assert disclosure.refusal(("note",), 2) == disclosure.REFUSAL
    assert disclosure.refusal(("a", "b"), 2) == disclosure.REFUSAL
    with pytest.raises(ValueError, match="at least one field"):
        disclosure.refusal((), 1)


def test_an_external_agent_cannot_send_a_listed_term(scheduler: Scheduler) -> None:
    tool = scheduler.send_tool(AgentAddress("probe"))
    before = last_seq(scheduler)
    result = call(tool, {"to": "beta", "body": f"{TERM} details"})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == (
        "the body field contains a term from the project's disclosure list"
    )
    assert last_seq(scheduler) == before
    assert call(tool, {"to": "beta", "body": "details"}).get("is_error") is not True


def test_operator_messages_are_not_checked(scheduler: Scheduler) -> None:
    message_id = scheduler.send(OperatorAddress("ana"), AgentAddress("probe"), TERM)
    assert message_id


# The sandbox requirement.

ENABLED = parse_sandbox({"enabled": True}, operator=False)
DISABLED = parse_sandbox({"enabled": False}, operator=True)


@pytest.mark.parametrize(
    ("project", "line", "model", "refused"),
    [
        (None, None, HAIKU, True),
        (ENABLED, None, HAIKU, False),
        (None, ENABLED, HAIKU, False),
        (ENABLED, DISABLED, HAIKU, True),
        (None, None, "gpt-6-luna", False),
        (None, None, "grok-4.7", False),
    ],
)
def test_a_required_sandbox_is_checked_on_the_merged_document(
    project: SandboxConfig | None,
    line: SandboxConfig | None,
    model: str,
    refused: bool,
) -> None:
    required = ProjectConfig(require_sandbox=True, sandbox=project)
    config = LineConfig(sandbox=line)
    if refused:
        with pytest.raises(SandboxError, match="line alpha would run on Claude Code"):
            check_sandbox_required(required, "alpha", model, config)
    else:
        check_sandbox_required(required, "alpha", model, config)
    check_sandbox_required(ProjectConfig(sandbox=project), "alpha", model, config)


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def write_file(paths: ProjectPaths, text: str) -> None:
    config_path(paths.state_dir).write_text("version = 1\n" + text, encoding="utf-8")


class Client:
    def __init__(self, target: Daemon) -> None:
        self.daemon = target

    def ask(self, op: str, args: dict[str, object]) -> Response:
        line = {"id": "r", "op": op, "v": 1, "args": args}
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def refused(self, op: str, args: dict[str, object]) -> str:
        before = self.daemon.log.last_seq
        response = self.ask(op, args)
        assert not response.ok and response.error_code is ErrorCode.REJECTED
        assert self.daemon.log.last_seq == before
        return response.error_message or ""

    def project_entry(self, key: str) -> dict[str, object]:
        response = self.ask("settings", {})
        assert response.ok and response.result is not None
        entries = cast(list[dict[str, object]], response.result["project"])
        return next(e for e in entries if e["key"] == key)


@contextmanager
def attached(paths: ProjectPaths, project: ProjectConfig) -> Generator[Client]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        yield Client(
            Daemon(
                paths,
                log,
                "2026-10-26T00:00:00Z",
                scheduler,
                project=project,
                command_line=Overrides(),
                project_file_present=config_path(paths.state_dir).exists(),
            )
        )


def create(line_id: str, model: str = HAIKU, **config: object) -> dict[str, object]:
    args: dict[str, object] = {
        "line_id": line_id,
        "model": model,
        "strategy": "subscription",
    }
    if config:
        args["config"] = config
    return args


def test_create_and_configure_refuse_an_unsandboxed_claude_line(
    paths: ProjectPaths,
) -> None:
    with attached(paths, ProjectConfig(require_sandbox=True)) as client:
        message = client.refused("create_line", create("alpha"))
        assert "line alpha" in message and "require_sandbox" in message
        assert client.ask("create_line", create("alpha", sandbox={"enabled": True})).ok
        assert client.ask("create_line", create("luna", model="gpt-6-luna")).ok
        message = client.refused(
            "configure_line",
            {"line_id": "alpha", "config": {"builtin_tools": ["Read"]}},
        )
        assert "line alpha" in message
        assert client.ask(
            "configure_line",
            {"line_id": "alpha", "config": {"sandbox": {"enabled": True}}},
        ).ok


def test_a_project_sandbox_satisfies_the_requirement(paths: ProjectPaths) -> None:
    project = ProjectConfig(require_sandbox=True, sandbox=ENABLED)
    with attached(paths, project) as client:
        assert client.ask("create_line", create("alpha")).ok


def test_settings_show_the_guards_by_path_only(paths: ProjectPaths) -> None:
    with attached(paths, ProjectConfig()) as client:
        assert client.project_entry("daemon.require_sandbox")["value"] == "off"
        assert client.project_entry("daemon.disclosure_terms")["value"] == "none"
    project = ProjectConfig(require_sandbox=True, disclosure_terms="secret/terms.txt")
    with attached(paths, project) as client:
        entry = client.project_entry("daemon.require_sandbox")
        assert (entry["kind"], entry["value"]) == ("info", "on")
        terms = client.project_entry("daemon.disclosure_terms")
        assert (terms["kind"], terms["value"]) == ("info", "secret/terms.txt")


def test_a_reload_checks_every_line_against_a_new_requirement(
    paths: ProjectPaths,
) -> None:
    with attached(paths, ProjectConfig()) as client:
        assert client.ask("create_line", create("alpha")).ok
        write_file(paths, "[daemon]\nrequire_sandbox = true\n")
        message = client.refused("reload_project", {})
        assert "line alpha" in message
        assert client.ask(
            "configure_line",
            {"line_id": "alpha", "config": {"sandbox": {"enabled": True}}},
        ).ok
        write_file(
            paths,
            "[daemon]\nrequire_sandbox = true\n"
            "[lines.beta]\nmodel = 'claude-haiku-5-5'\n",
        )
        assert "line beta" in client.refused("reload_project", {})
        write_file(paths, "[daemon]\nrequire_sandbox = true\n")
        assert client.ask("reload_project", {}).ok
        assert client.daemon.project.require_sandbox is True


def test_check_lines_sandboxed_covers_lines_outside_the_file(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("client", HAIKU, Strategy.SUBSCRIPTION)
        config = parse("version = 1\n[daemon]\nrequire_sandbox = true\n")
        with pytest.raises(SandboxError, match="line client"):
            check_lines_sandboxed(config, log, scheduler)
        scheduler.configure_line(
            "client",
            LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True)),
        )
        check_lines_sandboxed(config, log, scheduler)
        scheduler.configure_line("client", DEFAULT_CONFIG)
        check_lines_sandboxed(parse("version = 1\n"), log, scheduler)


def test_a_reload_reads_the_disclosure_list_again(paths: ProjectPaths) -> None:
    terms = paths.root / "terms.txt"
    write_terms(terms, "first\n")
    write_file(paths, "[daemon]\ndisclosure_terms = 'terms.txt'\n")
    project = load(config_path(paths.state_dir))
    with attached(paths, project) as client:
        scheduler = client.daemon.scheduler
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        send = scheduler.own_tools("alpha")[0]
        assert call(send, {"to": "beta", "body": "second"}).get("is_error") is not True
        write_terms(terms, "second\n")
        assert client.ask("reload_project", {}).ok
        assert call(send, {"to": "beta", "body": "second"})["is_error"] is True
        terms.unlink()
        message = client.refused("reload_project", {})
        assert "terms.txt cannot be read" in message
        assert call(send, {"to": "beta", "body": "second"})["is_error"] is True


# Starting the daemon.


def start(paths: ProjectPaths, short_runtime: Path) -> int:
    argv = ["--project", str(paths.root), "--runtime-dir", str(short_runtime)]
    return daemon.main(argv)


def test_dromond_stops_on_an_unreadable_disclosure_list(
    paths: ProjectPaths, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(paths, "[daemon]\ndisclosure_terms = 'secret/terms.txt'\n")
    assert start(paths, short_runtime) == exits.FAILURE
    err = capsys.readouterr().err
    assert "terms.txt cannot be read" in err
    (paths.root / "secret").mkdir()
    (paths.root / "secret" / "terms.txt").write_bytes(TERM.encode() + b"\n\xff\n")
    assert start(paths, short_runtime) == exits.FAILURE
    err = capsys.readouterr().err
    assert "not UTF-8" in err and TERM.casefold() not in err.casefold()


def test_dromond_stops_on_an_unsandboxed_line_when_one_is_required(
    paths: ProjectPaths, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing_workers = {
        thread for thread in threading.enumerate() if thread.name == "dromon-processes"
    }
    write_file(
        paths,
        "[daemon]\nrequire_sandbox = true\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n",
    )
    assert start(paths, short_runtime) == exits.FAILURE
    assert "line alpha would run on Claude Code" in capsys.readouterr().err
    with EventLog(paths.event_log) as log:
        assert "alpha" not in log.state.lines
    assert {
        thread
        for thread in threading.enumerate()
        if thread.name == "dromon-processes" and thread.is_alive()
    } <= existing_workers


# Anonymous agents name themselves, so their names are held to the list.


def agent_call(
    target: Daemon, agent: str, tool: str, token: str | None = None, **arguments: object
) -> Response:
    args: dict[str, object] = {"agent": agent, "tool": tool, "arguments": arguments}
    if token is not None:
        args["token"] = token
    line = json.dumps({"id": "r", "op": "agent_call", "v": 1, "args": args}) + "\n"
    return asyncio.run(target.respond_agent(line.encode()))


@contextmanager
def agent_daemon(paths: ProjectPaths) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        policy = AgentPolicy(True, AgentAccess.MESSAGE, AgentAccess.MANAGE)
        yield Daemon(paths, log, "2026-10-26T00:00:00Z", scheduler, agents=policy)


def list_terms(target: Daemon, tmp_path: Path, text: str) -> None:
    file = write_terms(tmp_path / "agent-terms.txt", text)
    target.scheduler.set_disclosure_terms(load_terms(file))


def test_an_anonymous_agent_cannot_name_itself_with_a_listed_term(
    tmp_path: Path, paths: ProjectPaths
) -> None:
    with agent_daemon(paths) as target:
        list_terms(target, tmp_path, "codename\n")
        before = target.log.last_seq
        for name in ("codename", "the-codename-bot", "codename.2"):
            response = agent_call(target, name, "send_message", to="alpha", body="hi")
            assert (response.ok, response.error_code) == (False, ErrorCode.REJECTED)
            assert response.error_message == (
                "the name field contains a term from the project's disclosure list"
            )
            assert name not in target.log.state.agents
        assert target.log.last_seq == before
        clean = agent_call(target, "helper", "send_message", to="alpha", body="hi")
        assert clean.ok and "helper" in target.log.state.agents


def test_a_listed_term_added_later_refuses_an_anonymous_agent_already_known(
    tmp_path: Path, paths: ProjectPaths
) -> None:
    with agent_daemon(paths) as target:
        assert agent_call(target, "codename-bot", "list_mail").ok
        assert "codename-bot" in target.log.state.agents
        list_terms(target, tmp_path, "codename\n")
        response = agent_call(target, "codename-bot", "list_mail")
        assert response.error_message == (
            "the name field contains a term from the project's disclosure list"
        )


def test_an_agent_the_operator_registered_is_exempt(
    tmp_path: Path, paths: ProjectPaths
) -> None:
    with agent_daemon(paths) as target:
        list_terms(target, tmp_path, "codename\n")
        added = target.execute(
            Request(
                "a", Operation.AGENT_ADD, {"agent": "codename-bot", "access": "message"}
            )
        )
        assert added.ok and added.result is not None
        token = str(added.result["token"])
        assert agent_call(target, "codename-bot", "list_mail", token=token).ok


# A wake note is checked again when it fires.


def test_a_note_listed_after_scheduling_fires_withheld(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    now = [datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)]
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log, no_engine, hold_wakes=True, wall_clock=lambda: now[0]
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        clean_id, _ = scheduler.schedule_wake("alpha", 60, "recheck the codename build")
        scheduler.set_disclosure_terms(
            load_terms(write_terms(tmp_path / "t", "Codename\n"))
        )
        now[0] += timedelta(seconds=60)
        assert scheduler.fire_due_wakes() == [clean_id]
        fired = log.state.messages[clean_id]
        assert fired.body == WITHHELD_WAKE_NOTE
        assert (fired.sender, fired.recipient) == (
            LineAddress("alpha"),
            LineAddress("alpha"),
        )
        assert not any("codename" in m.body for m in log.state.messages.values())
        assert "alpha" not in log.state.scheduled
        other_id, _ = scheduler.schedule_wake("alpha", 60, "a clean note")
        now[0] += timedelta(seconds=60)
        scheduler.fire_due_wakes()
        assert log.state.messages[other_id].body == "a clean note"


def test_a_reload_with_a_new_list_withholds_a_pending_note(
    paths: ProjectPaths,
) -> None:
    from datetime import UTC, datetime, timedelta

    now = [datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)]
    terms = write_terms(paths.root / "terms.txt", "unrelated\n")
    write_file(paths, "[daemon]\ndisclosure_terms = 'terms.txt'\n")
    project = load(config_path(paths.state_dir))
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(
            log, no_engine, hold_wakes=True, wall_clock=lambda: now[0]
        )
        scheduler.set_disclosure_terms(load_terms(terms))
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        client = Client(
            Daemon(
                paths,
                log,
                "2026-10-26T00:00:00Z",
                scheduler,
                project=project,
                command_line=Overrides(),
                project_file_present=True,
            )
        )
        message_id, _ = scheduler.schedule_wake("alpha", 60, "the Codename result")
        write_terms(terms, "unrelated\ncodename\n")
        assert client.ask("reload_project", {}).ok
        now[0] += timedelta(seconds=60)
        scheduler.fire_due_wakes()
        assert log.state.messages[message_id].body == WITHHELD_WAKE_NOTE


def test_the_model_accepts_only_the_note_or_the_fixed_withheld_text() -> None:
    from dromon.model import (
        EventRejected,
        LineCreated,
        MessageKind,
        MessageSent,
        State,
        WakeScheduled,
    )

    alpha = LineAddress("alpha")
    state = State()
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "secret note"))
    with pytest.raises(EventRejected, match="reserved by line alpha's scheduled wake"):
        state.check(MessageSent("s1", alpha, alpha, MessageKind.NOTE, "redacted"))
    state.apply(MessageSent("s1", alpha, alpha, MessageKind.NOTE, WITHHELD_WAKE_NOTE))
    assert "alpha" not in state.scheduled


def test_instruction_paths_are_checked_where_a_configuration_arrives(
    paths: ProjectPaths,
) -> None:
    """Review of 2026-10-27, I0-1 and I0-2. A client's configuration and a reloaded
    project file are refused, with nothing recorded, for a relative path that leaves
    the project root or a file that does not exist."""
    (paths.root / "RULES.md").write_text("Rules.\n")
    with attached(paths, ProjectConfig()) as client:
        escaping = client.refused(
            "create_line", create("alpha", instructions=["../outside.md"])
        )
        assert "outside the project root" in escaping
        assert client.ask("create_line", create("alpha", instructions=["RULES.md"])).ok
        missing = client.refused(
            "configure_line",
            {"line_id": "alpha", "config": {"instructions": ["GONE.md"]}},
        )
        assert "GONE.md do not name a regular file" in missing
        write_file(
            paths,
            "[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
            "instructions = ['../outside.md']\n",
        )
        reloaded = client.refused("reload_project", {})
        assert "lines.alpha.instructions" in reloaded
        assert "for line alpha" in reloaded


def test_file_acceptance_preserves_counts_and_hides_locations(
    tmp_path: Path,
) -> None:
    from hashlib import sha256

    text = f"accepted {TERM}"
    (tmp_path / "a.txt").write_text(text + "\n" + TERM + "\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text(text, encoding="utf-8")
    with EventLog(tmp_path / "events.sqlite") as log:
        target = Scheduler(log, no_engine, root=tmp_path, hold_wakes=True)
        target.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        target.set_disclosure_terms((TERM.casefold(),))
        target.set_disclosure_accepted({"a.txt": (sha256(text.encode()).hexdigest(),)})
        assert target.check_disclosure("alpha", None, "a.txt")[0] == 1
        assert target.check_disclosure("alpha", None, "b.txt")[0] == 1
        assert target.check_disclosure("alpha", text, None)[0] == 1
        result = call(
            target.check_disclosure_tool("alpha"), {"paths": ["a.txt", "b.txt"]}
        )
        answer = result["content"][0]["text"]
        assert answer.startswith("fail, 2 lines hold")
        assert "a.txt" not in answer and "b.txt" not in answer
        assert "Locations" not in answer
        assert ":1" not in answer and ":2" not in answer
        single = call(target.check_disclosure_tool("alpha"), {"path": "a.txt"})
        single_answer = single["content"][0]["text"]
        assert single_answer.startswith("fail, 1 line holds")
        assert "a.txt" not in single_answer and ":2" not in single_answer
        assert TERM not in answer
        target.set_disclosure_accepted({})
        assert target.check_disclosure("alpha", None, "a.txt")[0] == 2
