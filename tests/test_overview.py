"""Round 16, item 1. One read-only view of a line's task, checkpoint, leases, cost
carried and ceilings, as a line tool and an operator request, built from what
``line_status``, ``role_status`` and ``list_leases`` answer, bounded, and recording
nothing."""

import asyncio
import json
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    BriefSet,
    EventRejected,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    SeatBound,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig
from dromon.protocol import ErrorCode, Operation, Request
from dromon.scheduler import (
    LINE_STATUS_TOOL_NAME,
    LIST_LEASES_TOOL_NAME,
    MAX_OVERVIEW_BYTES,
    MAX_OVERVIEW_LEASES,
    OVERVIEW_SOURCES,
    OVERVIEW_TOOL_NAME,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
AT = "2026-10-26T09:00:00Z"
COMMIT = "a" * 40
ANA = OperatorAddress("ana")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def log(tmp_path: Path) -> Generator[EventLog]:
    with EventLog(tmp_path / "events.sqlite") as found:
        yield found


@pytest.fixture
def scheduler(log: EventLog) -> Scheduler:
    found = Scheduler(log, no_engine, hold_wakes=True, wall_clock=Clock())
    for line_id in ("alpha", "beta"):
        found.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
    found.set_leases([f"lease{n:02}" for n in range(20)], 600)
    return found


def tool_text(scheduler: Scheduler, name: str, args: dict[str, object]) -> str:
    tools = {found.name: found for found in scheduler.own_tools("alpha")}

    async def invoke() -> dict[str, Any]:
        return await tools[name].handler(args)

    result = asyncio.run(invoke())
    assert result.get("is_error") is not True, result
    return cast(str, result["content"][0]["text"])


def size(view: dict[str, object]) -> int:
    return len(json.dumps(view, indent=2, sort_keys=True).encode())


def test_the_overview_joins_what_the_three_tools_answer(scheduler: Scheduler) -> None:
    """Each part equals what its source tool gives for the same state, so the view
    adds nothing of its own. Both sides come from the scheduler, not from
    constants."""
    scheduler.acquire_lease("alpha", "lease03", seconds=120)
    scheduler.acquire_lease("alpha", "lease01")
    view = json.loads(tool_text(scheduler, OVERVIEW_TOOL_NAME, {}))
    status = json.loads(tool_text(scheduler, LINE_STATUS_TOOL_NAME, {"detail": True}))
    listed = json.loads(tool_text(scheduler, LIST_LEASES_TOOL_NAME, {}))
    assert view["line_id"] == "alpha"
    assert view["condition"] == status["condition"]
    assert view["hold_cause"] is None and status["held_by"] is None
    assert view["task"] is None and status["brief"] is None
    mine = [
        {key: lease[key] for key in ("name", "expires", "seconds")}
        for lease in listed["leases"]
        if lease["holder"] == "alpha"
    ]
    assert view["leases"] == mine
    assert mine == [
        {"name": "lease01", "expires": "2026-10-26T09:10:00Z", "seconds": 600},
        {"name": "lease03", "expires": "2026-10-26T09:02:00Z", "seconds": 120},
    ]
    assert view["leases_omitted"] == 0
    cost = status["cost_usd"]
    assert view["cost"] == {
        "since_release_usd": cost["estimate"],
        "basis": cost["basis"],
        "carried": cost["whole"],
    }
    ceilings = view["ceilings"]
    assert ceilings["autonomous_turns"] == {
        "used": status["autonomous_turns"]["used"],
        "allowed": status["autonomous_turns"]["allowed"],
    }
    assert ceilings["cost_usd"] == {
        "limit": cost["limit"],
        "enforced": cost["limit_enforced"],
    }
    assert ceilings["provider_budget"] == status["provider_budget"]
    assert ceilings["lease_seconds"] == listed["limit_seconds"] == 600
    checkpoint = view["checkpoint"]
    assert checkpoint["workdir_commit"] == status["commits"]["workdir"]
    assert checkpoint["daemon_commit"] == status["commits"]["daemon"]
    assert checkpoint["daemon_stale"] == status["commits"]["stale"]
    assert checkpoint["role"] is None
    assert view["sources"] == OVERVIEW_SOURCES


def test_the_task_is_the_lines_own_brief_cut_and_labelled(
    scheduler: Scheduler, log: EventLog
) -> None:
    log.append(BriefSet("alpha", "t" * 900, "keep", ("a.txt",), "next", "", "", AT))
    task = scheduler.overview("alpha")["task"]
    assert isinstance(task, dict)
    view = cast(dict[str, Any], task)
    assert view["set_at"] == AT
    assert "untrusted" in str(view["trust"]).lower()
    data = view["untrusted_data"]
    assert data == {"task": "t" * 400, "next_action": "next", "artifact_count": 1}


def test_the_cut_is_in_bytes_as_the_sources_say(
    scheduler: Scheduler, log: EventLog
) -> None:
    """Review of 16de365. The sources text once said characters where the cut is in
    bytes, so a brief in a wide script kept fewer characters than it said."""
    wide = "漢" * 300
    log.append(BriefSet("alpha", wide, "keep", (), wide, "", "", AT))
    task = cast(dict[str, Any], scheduler.overview("alpha")["task"])
    data = task["untrusted_data"]
    for field in ("task", "next_action"):
        assert len(data[field].encode("utf-8")) <= 400 < len(wide.encode("utf-8"))
        assert data[field] == "漢" * (400 // 3)
    assert "400 bytes" in OVERVIEW_SOURCES and "characters" not in OVERVIEW_SOURCES


def test_a_seat_sees_its_role_owner_and_newest_entry_without_its_body(
    scheduler: Scheduler, log: EventLog
) -> None:
    for event in (
        OperatorRegistered("ana"),
        RoleDeclared("core", "."),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
        RoleNoted("core", "e1", "alpha", RoleNoteKind.NOTE, "older", COMMIT, "", AT),
        RoleNoted(
            "core", "e2", "alpha", RoleNoteKind.HANDOFF, "the body", COMMIT, "", AT
        ),
    ):
        log.append(event)
    role = scheduler.overview("beta")["checkpoint"]
    assert isinstance(role, dict)
    seen = cast(dict[str, Any], role)["role"]
    assert seen["role_id"] == "core"
    assert (seen["owner"], seen["you_own"]) == ("alpha", False)
    entry = seen["newest_entry"]
    assert (entry["entry_id"], entry["kind"], entry["workdir_commit"]) == (
        "e2",
        "handoff",
        COMMIT,
    )
    assert "the body" not in json.dumps(seen)
    owner = cast(dict[str, Any], scheduler.overview("alpha")["checkpoint"])["role"]
    assert owner["you_own"] is True


def test_the_overview_records_nothing(scheduler: Scheduler, log: EventLog) -> None:
    scheduler.acquire_lease("alpha", "lease00")
    log.append(BriefSet("alpha", "task", "", (), "", "", "", AT))
    before = log.last_seq
    scheduler.overview("alpha")
    tool_text(scheduler, OVERVIEW_TOOL_NAME, {})
    assert log.last_seq == before


def test_an_unknown_line_or_an_argument_is_refused(scheduler: Scheduler) -> None:
    with pytest.raises(EventRejected, match="no line gamma"):
        scheduler.overview("gamma")
    tools = {found.name: found for found in scheduler.own_tools("alpha")}

    async def invoke() -> dict[str, Any]:
        return await tools[OVERVIEW_TOOL_NAME].handler({"line_id": "beta"})

    result = asyncio.run(invoke())
    assert result.get("is_error") is True
    assert "line_id" in cast(str, result["content"][0]["text"])


def test_the_largest_overview_stays_within_its_bound(
    scheduler: Scheduler, log: EventLog
) -> None:
    """Every lease declared is held and the brief's text is as long as it may be,
    in characters that escape to twelve bytes of JSON each, the most any takes."""
    for n in range(20):
        scheduler.acquire_lease("alpha", f"lease{n:02}", seconds=60 + n)
    wide = "\U0001f600" * 1024
    log.append(BriefSet("alpha", wide, wide, ("x",) * 32, wide, COMMIT, COMMIT, AT))
    view = scheduler.overview("alpha")
    assert size(view) <= MAX_OVERVIEW_BYTES
    assert len(cast(list[object], view["leases"])) == MAX_OVERVIEW_LEASES
    assert view["leases_omitted"] == 20 - MAX_OVERVIEW_LEASES
    task = cast(dict[str, Any], view["task"])
    assert task["untrusted_data"]["task"]


def test_the_guard_drops_the_brief_then_the_leases_to_keep_the_bound(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A part whose bound a later change raises is cut by the guard. The bound is
    lowered here so that the real view passes it."""
    from dromon import scheduler as module

    for n in range(3):
        scheduler.acquire_lease("alpha", f"lease{n:02}")
    module_view = scheduler._overview("alpha")  # pyright: ignore[reportPrivateUsage]
    module_view["task"] = {"untrusted_data": {"task": "x" * 4000}, "set_at": AT}
    monkeypatch.setattr(module, "MAX_OVERVIEW_BYTES", size(module_view) - 1)
    bounded = module._bounded_overview(dict(module_view))  # pyright: ignore[reportPrivateUsage]
    assert cast(dict[str, Any], bounded["task"])["untrusted_data"] is None
    assert len(cast(list[object], bounded["leases"])) == 3
    # Just below the view without the brief, so only removing the leases fits.
    monkeypatch.setattr(module, "MAX_OVERVIEW_BYTES", size(bounded) - 1)
    bounded = module._bounded_overview(dict(module_view))  # pyright: ignore[reportPrivateUsage]
    assert (bounded["leases"], bounded["leases_omitted"]) == ([], 3)
    monkeypatch.setattr(module, "MAX_OVERVIEW_BYTES", 10)
    with pytest.raises(AssertionError, match="passed its bound"):
        module._bounded_overview(dict(module_view))  # pyright: ignore[reportPrivateUsage]


# The operator's request and command.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def test_the_operator_request_gives_the_same_view(paths: ProjectPaths) -> None:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True, wall_clock=Clock())
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        target = Daemon(
            paths,
            log,
            AT,
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=True,
        )
        answered = target.execute(
            Request("r", Operation.OVERVIEW, {"line_id": "alpha"})
        )
        assert answered.ok, answered.error_message
        assert answered.result == scheduler.overview("alpha")
        unknown = target.execute(Request("r", Operation.OVERVIEW, {"line_id": "zeta"}))
        assert not unknown.ok and unknown.error_code is ErrorCode.REJECTED
        assert "no line zeta" in str(unknown.error_message)
        missing = target.execute(Request("r", Operation.OVERVIEW, {}))
        assert not missing.ok and missing.error_code is ErrorCode.BAD_REQUEST


def overview_command(tmp_path: Path, runtime: Path, line_id: str) -> int:
    return cli.main(
        [
            "--project",
            str(tmp_path),
            "--runtime-dir",
            str(runtime),
            "overview",
            line_id,
        ]
    )


def test_the_command_prints_the_view_as_json(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[tuple[Operation, dict[str, object]]] = []
    view: dict[str, object] = {"line_id": "alpha", "leases": []}

    def answer(
        paths: object, command: str, operation: Operation, args: dict[str, object]
    ) -> dict[str, object]:
        asked.append((operation, args))
        return view

    monkeypatch.setattr(cli, "_call", answer)
    status = overview_command(tmp_path, short_runtime, "alpha")
    assert status == exits.OK
    assert asked == [(Operation.OVERVIEW, {"line_id": "alpha"})]
    assert json.loads(capsys.readouterr().out) == view


def test_the_command_passes_on_a_refusal(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        return exits.FAILURE

    monkeypatch.setattr(cli, "_call", refuse)
    assert overview_command(tmp_path, short_runtime, "zeta") == exits.FAILURE
