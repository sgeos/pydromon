"""The attached client moving between the projects of the fleet index, round 13,
item 4, without restarting."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from dromon import fleet
from dromon.protocol import Operation, Response
from dromon.ui import link
from dromon.ui.commands import (
    Invalid,
    ShowProjects,
    SwitchProject,
    parse_input,
    suggest,
)
from dromon.ui.data import LineSummary
from dromon.ui.keys import Char, Named
from dromon.ui.link import FleetProjects
from dromon.ui.model import (
    Effect,
    FetchProjects,
    FetchView,
    Model,
    OpenProject,
    Pressed,
    ProjectOpened,
    ProjectsLoaded,
)
from dromon.ui.render import frame, text_of
from dromon.ui.runtime import Daemon, Runtime, Unreachable
from dromon.ui.update import _choices, initial, update  # pyright: ignore[reportPrivateUsage]

STARTED = "Sat Oct 31 00:00:00 2026"
NO_PROCESS: dict[int, str] = {}
"""A process table that lists no process."""


def line(line_id: str) -> dict[str, object]:
    return {
        "line_id": line_id,
        "engine": "claude-haiku-5-5",
        "strategy": "subscription",
        "state": "idle",
        "turns": 0,
        "pending": 0,
        "cost_estimate_usd": "0.0000",
        "held_by": None,
    }


class LinesDaemon:
    """A daemon with ``lines``, which records each operation it is asked."""

    def __init__(self, *lines: str) -> None:
        self.lines = lines
        self.asked: list[Operation] = []

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        del args
        self.asked.append(operation)
        if operation is Operation.STATUS:
            return Response("r", True, result={"lines": [line(n) for n in self.lines]})
        return Response("r", True, result={})


class Projects:
    def __init__(self, daemons: dict[str, Daemon]) -> None:
        self.daemons = daemons

    def names(self) -> tuple[tuple[str, str], ...]:
        return tuple((name, "live") for name in sorted(self.daemons))

    def open(self, project: str) -> Daemon:
        found = self.daemons.get(project)
        if found is None:
            raise Unreachable(f"no daemon in the fleet index is named {project}")
        return found


def typed(client: Runtime, text: str) -> None:
    """Type ``text`` into the client's input and press Enter."""
    for char in text:
        client.press(Char(char))
    client.press(Named.ENTER)


def summary(line_id: str) -> LineSummary:
    return LineSummary(line_id, "m", "subscription", "idle", 0, 0, "0", None)


# Commands and the state machine.


def test_project_commands_are_parsed() -> None:
    assert parse_input("/project tide") == SwitchProject("tide", None)
    assert parse_input("/project tide core") == SwitchProject("tide", "core")
    assert isinstance(parse_input("/project"), Invalid)
    assert isinstance(parse_input("/project a b c"), Invalid)
    assert parse_input("/projects") == ShowProjects()
    assert isinstance(parse_input("/projects extra"), Invalid)


def entered(model: Model, text: str) -> tuple[Model, tuple[Effect, ...]]:
    """``model`` once ``text`` is typed and Enter pressed, and the effects of Enter."""
    for char in text:
        model, _ = update(model, Pressed(Char(char)))
    return update(model, Pressed(Named.ENTER))


def test_a_switch_asks_the_runtime_and_keeps_the_client_until_it_answers() -> None:
    model = initial("core", "ana", 1.0, "harbor")
    moved, effects = update(model, ProjectsLoaded((("tide", "live"),)))
    assert moved.projects == (("tide", "live"),)
    assert "tide live" in moved.notice
    asked, effects = entered(moved, "/project tide")
    assert effects == (OpenProject("tide", None, "core"),)
    assert asked.project == "harbor" and asked.line_id == "core"
    opened, effects = update(asked, ProjectOpened("tide", "alpha", (summary("alpha"),)))
    assert (opened.project, opened.line_id) == ("tide", "alpha")
    assert opened.view is None and opened.projects == (("tide", "live"),)
    assert FetchView("alpha") in effects
    _, effects = entered(opened, "/projects")
    assert effects == (FetchProjects(),)


def test_project_names_complete_and_the_bar_names_the_project() -> None:
    model = replace(
        initial("core", "ana", 1.0, "harbor"),
        projects=(("tide", "live"), ("harbor", "live")),
        width=60,
        height=16,
    )
    assert [s.label for s in suggest("/project t", _choices(model))] == ["tide"]
    rows = [text_of(row) for row in frame(model).rows]
    assert any("harbor:core | connecting" in row for row in rows)
    assert frame(model).title == "dromon harbor:core"


# The runtime.


def test_the_runtime_moves_to_another_daemon_and_keeps_the_line_name() -> None:
    harbor = LinesDaemon("core", "alpha")
    tide = LinesDaemon("beta", "core")
    client = Runtime(harbor, "core", "ana", 1.0, projects=Projects({"tide": tide}))
    client.resize(60, 16)
    typed(client, "/project tide")
    assert (client.model.project, client.model.line_id) == ("tide", "core")
    before = len(harbor.asked)
    client.tick(100.0)
    assert len(harbor.asked) == before
    assert Operation.LINE_VIEW in tide.asked


def test_the_runtime_takes_the_first_line_when_the_name_is_absent() -> None:
    tide = LinesDaemon("beta", "gamma")
    client = Runtime(
        LinesDaemon("core"), "core", "ana", 1.0, projects=Projects({"tide": tide})
    )
    typed(client, "/project tide")
    assert client.model.line_id == "beta"


@pytest.mark.parametrize(
    ("project", "line_id", "reason"),
    [
        ("nowhere", None, "no daemon in the fleet index is named nowhere"),
        ("tide", "missing", "tide has no line missing"),
        ("empty", None, "empty has no lines"),
    ],
)
def test_a_project_that_cannot_be_opened_leaves_the_client_where_it_was(
    project: str, line_id: str | None, reason: str
) -> None:
    harbor = LinesDaemon("core")
    projects = Projects({"tide": LinesDaemon("beta"), "empty": LinesDaemon()})
    client = Runtime(harbor, "core", "ana", 1.0, projects=projects, project="harbor")
    typed(client, f"/project {project} {line_id or ''}".strip())
    assert (client.model.project, client.model.line_id) == ("harbor", "core")
    assert reason in client.model.notice and client.model.alarm
    client.tick(100.0)
    assert Operation.LINE_VIEW in harbor.asked


def test_a_client_without_the_index_says_so() -> None:
    client = Runtime(LinesDaemon("core"), "core", "ana", 1.0)
    typed(client, "/project tide")
    assert "no fleet index" in client.model.notice
    typed(client, "/projects")
    assert "no fleet index" in client.model.notice


# The port over the index.


@pytest.fixture
def indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[fleet.IndexEntry, list[Path]]:
    root = tmp_path / "tide"
    root.mkdir()
    entry = fleet.IndexEntry(
        "tide", root, root / ".dromon" / "run" / "dromond.sock", 102, STARTED, None
    )
    fleet.write_index_entry(fleet.configured_index(os.environ), entry)
    sockets: list[Path] = []

    def answer(
        socket_path: Path,
        operation: Operation,
        timeout_seconds: float = 2.0,
        args: dict[str, object] | None = None,
    ) -> Response:
        del timeout_seconds, args
        sockets.append(socket_path)
        assert operation is Operation.STATUS
        return Response(
            "r", True, result={"pid": 102, "project": str(root), "lines": []}
        )

    monkeypatch.setattr(link, "call", answer)
    return entry, sockets


def test_the_port_opens_a_live_project_through_its_own_socket(
    indexed: tuple[fleet.IndexEntry, list[Path]],
) -> None:
    entry, sockets = indexed
    port = FleetProjects(fleet.configured_index(os.environ), {102: STARTED}.get)
    assert port.names() == (("tide", "live"),)
    port.open("tide")
    assert sockets == [entry.socket]


def test_the_port_refuses_a_stale_entry_and_a_daemon_that_is_not_the_one_named(
    indexed: tuple[fleet.IndexEntry, list[Path]],
) -> None:
    entry, sockets = indexed
    stale = FleetProjects(fleet.configured_index(os.environ), NO_PROCESS.get)
    with pytest.raises(Unreachable, match="is stale"):
        stale.open("tide")
    assert sockets == []
    other = replace(entry, pid=103)
    fleet.write_index_entry(fleet.configured_index(os.environ), other)
    moved = FleetProjects(fleet.configured_index(os.environ), {103: STARTED}.get)
    with pytest.raises(Unreachable, match="not the one the index names"):
        moved.open("tide")
