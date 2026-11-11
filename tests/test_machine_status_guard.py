"""Round 16 review, point 3. No line may write the machine monitor's status file,
since a status a line could write could lift or force the deferrals of runs and
turns. The file, and its directory when that is not around the line's working
directory, are denied to writes in an enabled sandbox and to the built-in file tools.
``docs/protocol/SANDBOX.md`` and ``docs/protocol/LINE_CONFIG.md`` are normative."""

import os
from pathlib import Path
from typing import cast

import pytest

from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine
from dromon.codex import CodexLine
from dromon.lineconfig import LineConfig
from dromon.sandbox import parse_sandbox
from dromon.scheduler import (
    LineSpec,
    Scheduler,
    engine_factory,
    machine_status_guards,
    machine_status_rules,
)

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"


def real(path: Path) -> str:
    return os.path.realpath(path)


def spec(model: str = HAIKU, config: LineConfig | None = None) -> LineSpec:
    return LineSpec("alpha", model, Strategy.SUBSCRIPTION, config or LineConfig())


def claude(root: Path, status: Path | None, sandboxed: bool = True) -> ClaudeCodeLine:
    project = parse_sandbox({"enabled": True}, operator=True) if sandboxed else None
    factory = engine_factory(
        str(root),
        project_sandbox=project,
        machine_status_file=lambda: status,
    )
    engine = factory(spec(config=LineConfig(builtin_tools=("Bash",))), [])
    assert isinstance(engine, ClaudeCodeLine)
    return engine


def denied(engine: ClaudeCodeLine) -> list[str]:
    filesystem = cast(dict[str, list[str]], (engine.sandbox or {})["filesystem"])
    return filesystem["denyWrite"]


def tool_rules(engine: ClaudeCodeLine) -> list[str]:
    permissions = cast(dict[str, list[str]], (engine.settings or {})["permissions"])
    return permissions["deny"]


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    outside = tmp_path / "state" / "monitor"
    root.mkdir()
    outside.mkdir(parents=True)
    return root, outside / "status"


def test_a_status_file_outside_the_workdir_is_denied_with_its_directory(
    layout: tuple[Path, Path],
) -> None:
    root, status = layout
    engine = claude(root, status)
    for path in (status, status.parent):
        assert real(path) in denied(engine)
    assert f"Edit(/{real(status)})" in tool_rules(engine)
    assert f"Edit(/{real(status.parent)}/**)" in tool_rules(engine)


def test_a_status_file_in_the_workdir_is_denied_alone(
    layout: tuple[Path, Path],
) -> None:
    root, _ = layout
    status = root / "machine-status"
    engine = claude(root, status)
    assert real(status) in denied(engine)
    assert real(root) not in denied(engine), "the line still writes its own directory"
    assert f"Edit(/{real(status)})" in tool_rules(engine)
    # The directory around it is not denied by this rule.
    assert f"Edit(/{real(root)}/**)" not in tool_rules(engine)


def test_a_status_file_in_a_subdirectory_of_the_workdir_denies_only_the_file(
    layout: tuple[Path, Path],
) -> None:
    root, _ = layout
    (root / "ops").mkdir()
    status = root / "ops" / "machine-status"
    engine = claude(root, status)
    assert real(status) in denied(engine) and real(root / "ops") not in denied(engine)


def test_a_status_file_that_is_a_link_denies_the_target_too(
    layout: tuple[Path, Path], tmp_path: Path
) -> None:
    root, status = layout
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = status.parent / "link"
    link.symlink_to(target / "status")
    engine = claude(root, link)
    assert real(target / "status") in denied(engine)
    assert real(target) in denied(engine)


def test_nothing_is_denied_when_no_file_is_configured(
    layout: tuple[Path, Path],
) -> None:
    root, status = layout
    assert real(status) not in denied(claude(root, None))
    assert machine_status_guards(None, root) == ()
    factory = engine_factory(str(root))
    engine = factory(spec(config=LineConfig(builtin_tools=("Bash",))), [])
    assert isinstance(engine, ClaudeCodeLine)
    assert str(status) not in str(engine.settings)


def test_a_line_whose_sandbox_is_off_still_has_its_file_tools_denied(
    layout: tuple[Path, Path],
) -> None:
    root, status = layout
    engine = claude(root, status, sandboxed=False)
    assert f"Edit(/{real(status)})" in tool_rules(engine)


def test_a_codex_line_is_denied_it_too(layout: tuple[Path, Path]) -> None:
    root, status = layout
    status.write_text("x")
    factory = engine_factory(str(root), machine_status_file=lambda: status)
    engine = factory(spec(LUNA), [])
    assert isinstance(engine, CodexLine)
    assert status in engine.write_protected and status.parent in engine.write_protected


def test_the_file_is_read_at_each_build_so_a_reload_is_followed(
    layout: tuple[Path, Path],
) -> None:
    root, first = layout
    second = first.parent / "other"
    current = [first]
    factory = engine_factory(str(root), machine_status_file=lambda: current[0])
    config = LineConfig(builtin_tools=("Bash",))
    one = factory(spec(config=config), [])
    current[0] = second
    two = factory(spec(config=config), [])
    assert isinstance(one, ClaudeCodeLine) and isinstance(two, ClaudeCodeLine)
    assert f"Edit(/{real(first)})" in tool_rules(one)
    assert f"Edit(/{real(second)})" in tool_rules(two)
    assert f"Edit(/{real(first)})" not in tool_rules(two)


def test_the_guards_and_rules_are_plain_data(layout: tuple[Path, Path]) -> None:
    root, status = layout
    guards = machine_status_guards(status, root)
    assert guards[0] == status and status.parent in guards
    assert machine_status_rules(guards) == sorted({f"Edit(/{path})" for path in guards})
    assert machine_status_rules(()) == []


def test_the_scheduler_names_the_file_it_reads(tmp_path: Path) -> None:
    from dromon.eventlog import EventLog

    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, lambda *_: None)  # type: ignore[arg-type,return-value]
        assert scheduler.machine_status_file is None
        scheduler.set_machine_status(tmp_path / "status")
        assert scheduler.machine_status_file == tmp_path / "status"
        scheduler.set_machine_status(None)
        assert scheduler.machine_status_file is None


def test_the_codex_profile_makes_an_existing_status_file_and_directory_read_only(
    layout: tuple[Path, Path],
) -> None:
    """Round 16 follow-up review, finding 1. The profile Dromon hands Codex lists each
    ``write_protected`` path that exists as ``read``, so the adapter honours it when it
    writes the profile. That Codex then enforces the entry is not checked here, since
    no test runs Codex. A path that does not exist when the profile is made is left out
    of it, so a status file the monitor has not yet written is not guarded."""
    import tomllib

    from dromon.codex import server_arguments

    root, status = layout
    entry = next(
        argument
        for argument in server_arguments(
            [],
            LineConfig(builtin_tools=("Bash", "Edit")),
            (),
            write_protected=machine_status_guards(status, root),
        )
        if argument.startswith("permissions.dromon-line=")
    )
    absent = tomllib.loads(f"profile = {entry.split('=', 1)[1]}")["profile"][
        "filesystem"
    ]
    assert real(status) not in absent, "the file does not exist yet"
    assert absent[real(status.parent)] == "read"
    status.write_text("x")
    entry = next(
        argument
        for argument in server_arguments(
            [],
            LineConfig(builtin_tools=("Bash", "Edit")),
            (),
            write_protected=machine_status_guards(status, root),
        )
        if argument.startswith("permissions.dromon-line=")
    )
    present = tomllib.loads(f"profile = {entry.split('=', 1)[1]}")["profile"][
        "filesystem"
    ]
    assert present[real(status)] == "read" and present[real(status.parent)] == "read"
