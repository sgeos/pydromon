"""The ``runs`` answer is bounded. **Round 19.** On 2026-11-18 ``dromon run status``
failed with "the line exceeds 65536", because the answer held each scope's latest ten
runs with their notes and output tails, and a project with enough lines passed the
protocol's line limit. The answer is now bounded by a count and by a byte budget
derived from the line limit, and it gives the queued runs. ``docs/architecture/RUNS.md``
is normative."""

import json
from collections.abc import Generator, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from dromon import cli
from dromon import scheduler as scheduler_module
from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    MAX_ADMIT_REASON_BYTES,
    MAX_QUEUED_RUNS,
    MAX_RUN_NOTE_BYTES,
    MAX_RUN_TAIL_BYTES,
    MAX_TASK_PATH_BYTES,
    EventRejected,
    OperatorAddress,
    Run,
    RunAdmitted,
    RunEnded,
    RunStarted,
    RunStatus,
    format_due,
)
from dromon.paths import project_paths
from dromon.projectconfig import ProjectConfig
from dromon.protocol import (
    MAX_LINE_BYTES,
    RUNS_BUDGET_BYTES,
    RUNS_LIMIT_DEFAULT,
    RUNS_LIMIT_MAX,
    RUNS_RESERVE_BYTES,
    ErrorCode,
    Operation,
    Response,
    encode_response,
)
from dromon.scheduler import MAX_QUEUED_WAIT_BYTES, MAX_RUNS_SHOWN, Scheduler
from test_queued_runs import Rig
from test_runs import AT, HAIKU, LATER, Clock, scheduler_on, started

LINES = 40
"""Lines with runs. Forty is more than the lines of a busy project, so the failure the
operator met reproduces with margin, and still a small number of events."""

TAIL = "".join(
    f"FAILED tests/test_line_{n}.py::test_{n} - AssertionError\n" for n in range(40)
)
"""A plausible failure tail, cut to the model's bound where it is longer."""


def failed_tail() -> str:
    return TAIL.encode("utf-8")[:MAX_RUN_TAIL_BYTES].decode("utf-8", "ignore")


def many_runs(log: EventLog, scheduler: Scheduler, lines: int = LINES) -> int:
    """``lines`` lines, each with ``MAX_RUNS_SHOWN + 2`` failed runs that carry a
    note and an output tail. Returns how many runs there are."""
    count = 0
    for line in range(lines):
        line_id = f"line-{line:03d}"
        scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        previous = None
        for number in range(MAX_RUNS_SHOWN + 2):
            run_id = f"r-{line:03d}-{number:02d}"
            log.append(started(run_id, line_id=line_id, previous=previous))
            log.append(
                RunEnded(
                    run_id,
                    RunStatus.EXITED,
                    1,
                    None,
                    None,
                    "n" * (MAX_RUN_NOTE_BYTES - 1),
                    LATER,
                    failed_tail(),
                )
            )
            previous = run_id
            count += 1
    return count


def answer(daemon: Daemon, args: dict[str, object]) -> Response:
    line = {"id": "r", "op": Operation.RUNS.value, "v": 1, "args": args}
    return daemon.respond((json.dumps(line) + "\n").encode())


@pytest.fixture
def project(tmp_path: Path) -> Generator[Path]:
    (tmp_path / "reports").mkdir()
    yield tmp_path


def daemon_for(project: Path, log: EventLog, scheduler: Scheduler) -> Daemon:
    paths = project_paths(project, project / "run")
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    return Daemon(
        paths,
        log,
        format_due(Clock().now),
        scheduler,
        project=ProjectConfig(),
        command_line=Overrides(),
        project_file_present=False,
    )


def test_the_answer_the_operator_met_did_not_fit_the_line(project: Path) -> None:
    """Confirmed. The answer before the change, every scope's latest ``MAX_RUNS_SHOWN``
    runs each with its note and tail, is over the line limit for these runs, so the
    daemon could not encode it, which is the operator's failure. Built here from the
    same view function, so it does not depend on the fix."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        many_runs(log, scheduler)
        state = log.state
        unbounded = 0
        for scope_runs in _by_line(state.runs.values()).values():
            for run in scope_runs[-MAX_RUNS_SHOWN:]:
                view = scheduler_module._run_view(  # pyright: ignore[reportPrivateUsage]
                    run, ("line", run.started.line_id), (), None, detail=True
                )
                unbounded += scheduler_module._written_size(view)  # pyright: ignore[reportPrivateUsage]
    assert unbounded > MAX_LINE_BYTES, unbounded


def _by_line(runs: Iterable[Run]) -> dict[str, list[Run]]:
    grouped: dict[str, list[Run]] = {}
    for run in runs:
        grouped.setdefault(run.started.line_id, []).append(run)
    return grouped


def test_the_runs_answer_fits_the_line_for_the_same_runs(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        total = many_runs(log, scheduler)
        daemon = daemon_for(project, log, scheduler)
        response = answer(daemon, {})
        assert response.ok, response.error_message
        line = encode_response(response)
        assert len(line) <= MAX_LINE_BYTES
        result = response.result
        assert result is not None
        shown = cast(list[object], result["runs"])
        assert 0 < len(shown) <= RUNS_LIMIT_DEFAULT
        assert result["total"] == total
        assert result["left_out"] == total - len(shown)
        assert int(str(result["left_out"])) > 0


def test_the_largest_limit_still_fits_the_line(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        many_runs(log, scheduler)
        daemon = daemon_for(project, log, scheduler)
        response = answer(daemon, {"limit": RUNS_LIMIT_MAX})
        assert response.ok, response.error_message
        assert len(encode_response(response)) <= MAX_LINE_BYTES
        assert response.result is not None
        assert len(response.result["runs"]) <= RUNS_LIMIT_MAX  # type: ignore[arg-type]


def test_a_list_says_where_the_note_and_tail_are_and_one_run_gives_both(
    project: Path,
) -> None:
    """**Review.** The list gave ``null``, which a client that predates the cut cannot
    tell from a run with no note. It now gives words that name the size and the request
    that gives the text, an empty text stays empty, and a run that has not ended has
    none, so it stays ``null``."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("zed", HAIKU, Strategy.SUBSCRIPTION)
        log.append(started("r-quiet", line_id="zed"))
        log.append(RunEnded("r-quiet", RunStatus.EXITED, 0, None, None, "", LATER))
        log.append(started("r-open", line_id="zed", previous="r-quiet"))
        many_runs(log, scheduler, lines=2)
        page = scheduler.runs_page(None, RUNS_LIMIT_MAX, RUNS_BUDGET_BYTES)
        listed = cast(list[dict[str, Any]], page["runs"])
        by_id = {str(view["run_id"]): view for view in listed}
        talking = [v for v in listed if str(v["run_id"]).startswith("r-0")]
        assert talking
        for view in talking:
            note = str(view["note"])
            assert note.startswith("[not in a list, ")
            assert f"dromon run status {view['run_id']} gives it]" in note
            assert f"{MAX_RUN_NOTE_BYTES - 1} bytes" in note
            assert str(view["output_tail"]).startswith("[not in a list, ")
        assert by_id["r-quiet"]["note"] == ""
        assert by_id["r-quiet"]["output_tail"] is None
        assert by_id["r-open"]["note"] is None
        assert by_id["r-open"]["output_tail"] is None
        (one,) = scheduler.runs_view("r-000-05")
        assert one["output_tail"] == failed_tail()
        assert one["note"] == "n" * (MAX_RUN_NOTE_BYTES - 1)


def test_a_run_that_blocks_its_scope_is_listed_before_newer_finished_runs(
    project: Path,
) -> None:
    """An open run in the oldest scope is what an operator looks for, so it is shown
    even when the limit would otherwise cut it."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("old", HAIKU, Strategy.SUBSCRIPTION)
        log.append(started("r-open", line_id="old"))
        many_runs(log, scheduler, lines=6)
        page = scheduler.runs_page(None, 3, RUNS_BUDGET_BYTES)
        shown: list[dict[str, Any]] = page["runs"]  # type: ignore[assignment]
        assert len(shown) == 3
        assert "r-open" in [view["run_id"] for view in shown]
        assert page["left_out"] == int(str(page["total"])) - 3


def test_the_log_refuses_a_start_behind_a_blocking_run(project: Path) -> None:
    """The per-scope window of ``MAX_RUNS_SHOWN`` cannot hide a blocking run only
    because a blocking run is always the newest of its scope, which this rule gives.
    The review of round 19 found no test pinned it, so a change that let a scope
    start behind an open or an uncleared lost run would break the window silently."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        log.append(started("r-open", line_id="alpha"))
        with pytest.raises(EventRejected, match="run r-open has not ended"):
            log.append(started("r-next", line_id="alpha", previous="r-open"))
        log.append(RunEnded("r-open", RunStatus.LOST, None, None, None, "", LATER))
        assert log.state.runs["r-open"].blocks
        with pytest.raises(EventRejected, match="run r-open is lost and not cleared"):
            log.append(started("r-next", line_id="alpha", previous="r-open"))


def test_a_blocking_run_is_never_hidden_by_the_window_or_the_limit(
    project: Path,
) -> None:
    """Built through the log, which refuses a start behind a blocking run, so the
    runs are ones a daemon can hold. In every scope the blocking run, open or lost,
    is the newest of the scope, whatever came before it, and the page shows every
    blocking run when the limit is their number, with newer runs of other scopes
    around."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        blocking: list[str] = []
        for number, status in enumerate((None, RunStatus.LOST)):
            line_id = f"stuck-{number}"
            scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
            previous = None
            for step in range(MAX_RUNS_SHOWN + 5):
                run_id = f"r-s{number}-{step:02d}"
                log.append(started(run_id, line_id=line_id, previous=previous))
                log.append(RunEnded(run_id, RunStatus.EXITED, 0, None, None, "", LATER))
                previous = run_id
            last = f"r-s{number}-last"
            log.append(started(last, line_id=line_id, previous=previous))
            if status is not None:
                log.append(RunEnded(last, status, None, None, None, "", LATER))
            blocking.append(last)
        many_runs(log, scheduler, lines=6)
        state = log.state
        for scope_runs in _by_line(state.runs.values()).values():
            for run in scope_runs[:-1]:
                assert not run.blocks, "a blocking run is the newest of its scope"
        page = scheduler.runs_page(None, len(blocking), RUNS_BUDGET_BYTES)
        shown: list[dict[str, Any]] = page["runs"]  # type: ignore[assignment]
        assert sorted(view["run_id"] for view in shown) == sorted(blocking)
        assert all(view["blocks"] for view in shown)


def test_the_page_is_grouped_by_scope_with_each_scopes_runs_newest_first(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        many_runs(log, scheduler, lines=3)
        page = scheduler.runs_page(None, 30, RUNS_BUDGET_BYTES)
        shown: list[dict[str, Any]] = page["runs"]  # type: ignore[assignment]
        scopes = [view["scope"] for view in shown]
        assert scopes == sorted(scopes)
        for scope in set(scopes):
            ids = [view["run_id"] for view in shown if view["scope"] == scope]
            assert ids == sorted(ids, reverse=True)


def test_a_budget_that_holds_two_runs_shows_two_and_counts_the_rest(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        total = many_runs(log, scheduler, lines=3)
        one = scheduler_module._written_size(  # pyright: ignore[reportPrivateUsage]
            scheduler.runs_view("r-000-05")[0]
        )
        sizes = [
            scheduler_module._written_size(view)  # pyright: ignore[reportPrivateUsage]
            for view in scheduler.runs_view(None)[:2]
        ]
        page = scheduler.runs_page(None, RUNS_LIMIT_MAX, sum(sizes))
        assert len(page["runs"]) == 2  # type: ignore[arg-type]
        assert page["left_out"] == total - 2
        none = scheduler.runs_page(None, RUNS_LIMIT_MAX, 10)
        assert none["runs"] == [] and none["left_out"] == total
        assert one > 0


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 0},
        {"limit": RUNS_LIMIT_MAX + 1},
        {"limit": True},
        {"limit": "5"},
        {"limit": 5, "run_id": "r-1"},
        {"limits": 5},
        {"run_id": 7},
    ],
)
def test_a_bad_limit_or_a_limit_with_a_run_is_refused_and_nothing_is_read(
    project: Path, args: dict[str, object]
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        daemon = daemon_for(project, log, scheduler)
        refused = answer(daemon, args)
        assert not refused.ok and refused.error_code is ErrorCode.BAD_REQUEST


def test_one_run_is_not_cut_by_the_list_bound(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        many_runs(log, scheduler, lines=2)
        daemon = daemon_for(project, log, scheduler)
        response = answer(daemon, {"run_id": "r-001-00"})
        assert response.ok and response.result is not None
        assert response.result["left_out"] == 0
        (view,) = response.result["runs"]  # type: ignore[misc]
        assert view["run_id"] == "r-001-00"


# The bound is derived, and checked.


def worst_started(run_id: str, commit_run: bool = True) -> RunStarted:
    """A run whose every text field is as long and as costly to write as the model
    allows. A control character is written as six bytes in JSON, the most any
    character takes, and a path must start with a slash. **Round 19, review.** A commit
    run gives a ``checkout`` path in place of the tree, the dirty count and the
    worktree's commit, so it is the larger shape, which this gives by default. A run in
    a worktree is ``commit_run=False``."""
    long_path = "/" + "\x01" * (MAX_TASK_PATH_BYTES - 1)
    base = started(run_id, line_id="l" * 64)
    common = replace(
        base,
        role_id="r" * 64,
        previous="p" * 64,
        command="c" * 64,
        workdir=long_path,
        output_file=long_path,
        admitted_by="o" * 64,
    )
    if commit_run:
        return replace(common, commit="c" * 40, checkout=long_path)
    return replace(
        common, workdir_commit="a" * 40, tree="b" * 64, dirty_paths=2**31 - 1
    )


def worst_ended(run_id: str) -> RunEnded:
    return RunEnded(
        run_id,
        RunStatus.EXITED,
        -(2**31) + 1,
        "c" * 64,
        10**9,
        "\x01" * MAX_RUN_NOTE_BYTES,
        LATER,
        '"\n' * (MAX_RUN_TAIL_BYTES // 2),
    )


def test_the_worst_run_the_model_allows_fits_the_budget_and_the_line(
    project: Path,
) -> None:
    """A list view of the worst run, of either shape, fits the budget, so the budget
    never leaves an answer with no run for want of room, and one run's whole view with
    the envelope and the override fits the line. **Round 19, review.** The worst shape
    is a commit run, whose ``checkout`` path adds up to 6 KiB. Of those a page holds
    one before the budget stops it, and ``left_out`` says so, which is the cost of the
    bound for runs that take the most the model allows, and not for ordinary runs."""
    many = tuple("r-" + str(n) * 60 for n in range(1, 10)) * 100
    for commit_run in (True, False):
        run_id = "r-" + "9" * 60
        run = Run(worst_started(run_id, commit_run), worst_ended(run_id))
        listed = scheduler_module._written_size(  # pyright: ignore[reportPrivateUsage]
            scheduler_module._run_view(  # pyright: ignore[reportPrivateUsage]
                run, ("line", "l" * 64), many, "r-" + "7" * 60, False
            )
        )
        whole = scheduler_module._written_size(  # pyright: ignore[reportPrivateUsage]
            scheduler_module._run_view(  # pyright: ignore[reportPrivateUsage]
                run, ("line", "l" * 64), many, "r-" + "7" * 60, True
            )
        )
        assert listed <= RUNS_BUDGET_BYTES, f"a list view is {listed} bytes"
        assert whole + RUNS_RESERVE_BYTES <= MAX_LINE_BYTES, f"a whole view is {whole}"


def test_a_run_that_corrected_many_names_the_newest_and_counts_the_rest(
    project: Path,
) -> None:
    """A passing run corrects every failed run of its command since the last pass,
    which is as many as the log holds. Confirmed before the cut, which listed them
    all."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        count = scheduler_module.MAX_CORRECTS_SHOWN + 5
        for number in range(count):
            before = None if number == 0 else f"r-f{number - 1:03d}"
            log.append(started(f"r-f{number:03d}", previous=before))
            log.append(
                RunEnded(f"r-f{number:03d}", RunStatus.EXITED, 1, None, None, "", LATER)
            )
        log.append(started("r-pass", previous=f"r-f{count - 1:03d}"))
        log.append(RunEnded("r-pass", RunStatus.EXITED, 0, None, None, "", LATER))
        (view,) = scheduler.runs_view("r-pass")
    assert len(view["corrects"]) == scheduler_module.MAX_CORRECTS_SHOWN  # type: ignore[arg-type]
    assert view["corrects"][-1] == f"r-f{count - 1:03d}"  # type: ignore[index]
    assert view["corrects_left_out"] == 5
    row = cli._run_row(view)  # pyright: ignore[reportPrivateUsage]
    assert row.endswith(", corrects " + ", ".join(view["corrects"]) + " and 5 older")  # type: ignore[arg-type]


def test_the_reserve_covers_the_worst_of_everything_but_the_runs() -> None:
    """The envelope, the override, the queued runs and the counts, each at the most
    the model and the queue allow, with the same costly characters, sum to under two
    thirds of the reserve, so the budget plus all of them is under the line limit."""
    envelope = len(
        json.dumps(
            {"id": "i" * 64, "ok": True, "v": 1, "result": {}},
            separators=(",", ":"),
        )
    )
    admission = len(
        json.dumps(
            {
                "operator": "o" * 64,
                "admitted_at": AT,
                "expires_at": AT,
                "reason": "\x01" * MAX_ADMIT_REASON_BYTES,
                "active": True,
                "standing_by": True,
                "expired": True,
                "critical": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    one_queued = len(
        json.dumps(
            {
                "queue_id": "q" * 64,
                "line_id": "l" * 64,
                "command": "c" * 64,
                "commit": "a" * 40,
                "queued_at": AT,
                "position": MAX_QUEUED_RUNS,
                "of": MAX_QUEUED_RUNS,
                "waits": scheduler_module._clip_text(  # pyright: ignore[reportPrivateUsage]
                    "\x01" * 4000, MAX_QUEUED_WAIT_BYTES
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    counts = len(json.dumps({"total": 10**9, "left_out": 10**9}))
    worst = envelope + admission + MAX_QUEUED_RUNS * (one_queued + 1) + counts
    assert worst * 3 < RUNS_RESERVE_BYTES * 2, (worst, RUNS_RESERVE_BYTES)
    assert RUNS_BUDGET_BYTES + RUNS_RESERVE_BYTES == MAX_LINE_BYTES


def test_a_full_worst_case_answer_encodes_within_the_line(project: Path) -> None:
    """The whole answer for runs that each take the most they may, an override and a
    full queue, built through the daemon and encoded as it is sent."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        for line in range(LINES):
            line_id = f"line-{line:03d}"
            scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
            run_id = f"r-w-{line:03d}"
            started_run = replace(
                worst_started(run_id),
                line_id=line_id,
                role_id=None,
                previous=None,
                admitted_by=None,
            )
            log.append(started_run)
            log.append(worst_ended(run_id))
        scheduler.ensure_operator("ana")
        log.append(
            RunAdmitted(
                "ana", AT, "2026-10-31T10:00:00Z", "\x01" * MAX_ADMIT_REASON_BYTES
            )
        )
        daemon = daemon_for(project, log, scheduler)
        response = answer(daemon, {"limit": RUNS_LIMIT_MAX})
        assert response.ok, response.error_message
        assert len(encode_response(response)) <= MAX_LINE_BYTES
        assert response.result is not None
        shown: list[Any] = response.result["runs"]  # type: ignore[assignment]
        assert shown, "an answer with no run for want of room"


# The queued runs.


@pytest.fixture
def rig(tmp_path: Path) -> Generator[Rig]:
    made = Rig(tmp_path.resolve())
    yield made
    made.log.close()


def test_a_queued_run_is_in_the_answer_with_its_place_and_what_holds_it(
    rig: Rig,
) -> None:
    """The operator could not see queued runs. Confirmed, since the answer had no field
    for them."""
    rig.set_level("elevated")
    queued = rig.queue("alpha")
    page = rig.scheduler.runs_page(None, RUNS_LIMIT_DEFAULT, RUNS_BUDGET_BYTES)
    waiting: list[dict[str, Any]] = page["queued"]  # type: ignore[assignment]
    assert [entry["queue_id"] for entry in waiting] == [queued.queue_id]
    (entry,) = waiting
    assert entry["line_id"] == "alpha" and entry["command"] == "gate"
    assert (entry["position"], entry["of"]) == (1, 1)
    assert isinstance(entry["waits"], str) and "machine status" in entry["waits"]
    assert len(entry["waits"].encode()) <= MAX_QUEUED_WAIT_BYTES


def test_nothing_queued_gives_an_empty_list(rig: Rig) -> None:
    page = rig.scheduler.runs_page(None, RUNS_LIMIT_DEFAULT, RUNS_BUDGET_BYTES)
    assert page["queued"] == []


def written(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("unit", ["\x01", '"', "\n", "é", "\u2028", "a"])
def test_a_queued_reason_over_the_bound_is_cut_by_its_written_length(
    unit: str,
) -> None:
    """Confirmed before the change, which cut by UTF-8 bytes. A control character is
    one byte there and six written, so a cut of 512 bytes of them wrote 3 KiB. The cut
    is now by the bytes written in JSON, whatever the character."""
    cut = scheduler_module._clip_text(unit * 2000, MAX_QUEUED_WAIT_BYTES)  # pyright: ignore[reportPrivateUsage]
    assert written(cut) <= MAX_QUEUED_WAIT_BYTES and cut.endswith("...")
    assert len(cut) > len("...")
    longer = scheduler_module._clip_text(unit * 2000 + "x", MAX_QUEUED_WAIT_BYTES)  # pyright: ignore[reportPrivateUsage]
    assert longer == cut, "the cut is of the start, so it does not depend on the end"
    one_more = cut[: -len("...")] + unit + "..."
    assert written(one_more) > MAX_QUEUED_WAIT_BYTES, "it keeps as much as fits"


def test_a_queued_reason_that_fits_is_not_cut_and_the_byte_cut_is_what_failed() -> None:
    same = scheduler_module._clip_text("short", MAX_QUEUED_WAIT_BYTES)  # pyright: ignore[reportPrivateUsage]
    assert same == "short"
    exact = "a" * (MAX_QUEUED_WAIT_BYTES - 2)
    assert scheduler_module._clip_text(exact, MAX_QUEUED_WAIT_BYTES) == exact  # pyright: ignore[reportPrivateUsage]
    assert written("\x01" * MAX_QUEUED_WAIT_BYTES) > 5 * MAX_QUEUED_WAIT_BYTES


# The client.


def client_for(
    project: Path, daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    def call(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        line = {"id": "r", "op": operation.value, "v": 1, "args": args or {}}
        return daemon.respond((json.dumps(line) + "\n").encode())

    monkeypatch.setattr(cli, "call", call)
    return ("--project", str(project), "--runtime-dir", str(project / "run"))


def test_the_client_lists_a_bounded_answer_and_says_what_it_left_out(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        total = many_runs(log, scheduler)
        base = client_for(project, daemon_for(project, log, scheduler), monkeypatch)
        assert cli.main([*base, "run", "status"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert cli.main([*base, "run", "status", "--limit", "3"]) == 0
        small = capsys.readouterr().out.splitlines()
    rows = [line for line in out if line.startswith("  r-")]
    assert 0 < len(rows) <= RUNS_LIMIT_DEFAULT
    assert out[-1].startswith(f"{total - len(rows)} more runs are not shown")
    assert "dromon run status RUN_ID shows one run whole" in out[-1]
    assert f"to at most {RUNS_LIMIT_MAX}" in out[-1]
    assert len([line for line in small if line.startswith("  r-")]) == 3
    assert small[-1].startswith(f"{total - 3} more runs are not shown")


def test_the_client_refuses_a_bad_limit_before_asking(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        base = client_for(project, daemon_for(project, log, scheduler), monkeypatch)

        def never(*_: object, **__: object) -> None:
            raise AssertionError("a request was made")

        monkeypatch.setattr(cli, "call", never)
        for bad in ("0", str(RUNS_LIMIT_MAX + 1), "-1"):
            assert cli.main([*base, "run", "status", "--limit", bad]) == 2
            assert "--limit must be from 1 to" in capsys.readouterr().err
        assert cli.main([*base, "run", "status", "r-1", "--limit", "3"]) == 2
        assert "--limit is for the list" in capsys.readouterr().err


def test_the_client_lists_queued_runs(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig.set_level("elevated")
    queued = rig.queue("alpha")
    rig.root.joinpath("run").mkdir(exist_ok=True)
    base = client_for(
        rig.root, daemon_for(rig.root, rig.log, rig.scheduler), monkeypatch
    )
    assert cli.main([*base, "run", "status"]) == 0
    out = capsys.readouterr().out
    assert "queued, waiting to start" in out
    assert queued.queue_id in out and "position 1 of 1" in out
    assert "it waits, since the machine status defers runs" in out


def test_the_default_and_the_maximum_are_within_the_protocol_bound() -> None:
    assert 1 <= RUNS_LIMIT_DEFAULT <= RUNS_LIMIT_MAX
    assert RUNS_RESERVE_BYTES <= MAX_LINE_BYTES // 2


# The other answers that grow with the log, which the audit of this round bounded.


def seed_questions(scheduler: Scheduler, count: int, text: str) -> list[str]:
    scheduler.ensure_operator("ana")
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    return [
        scheduler.ask("alpha", OperatorAddress("ana"), text, ("yes", "no"))
        for _ in range(count)
    ]


def test_many_open_questions_are_bounded_and_counted(project: Path) -> None:
    """Confirmed before the change by the arithmetic below, which is over the line.
    The answer held every open question and each text may be as long as the model
    allows, so with a few long ones it passed the line."""
    from dromon.daemon import ANSWER_BUDGET_BYTES

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        ids = seed_questions(scheduler, 12, "q" * 12_000)
        daemon = daemon_for(project, log, scheduler)
        line = {"id": "r", "op": "questions", "v": 1, "args": {"operator": "ana"}}
        response = daemon.respond((json.dumps(line) + "\n").encode())
        assert response.ok and response.result is not None
        assert len(ids) * 12_000 > MAX_LINE_BYTES, "the old answer was over the line"
        assert len(encode_response(response)) <= MAX_LINE_BYTES
        shown = cast(list[dict[str, object]], response.result["questions"])
        assert 0 < len(shown) < len(ids)
        assert [entry["question_id"] for entry in shown] == ids[: len(shown)]
        assert response.result["total"] == len(ids)
        left_out = len(ids) - len(shown)
        assert response.result["left_out"] == left_out
        # An older client prints the text and ignores the counts, so the last text
        # says how many were left out.
        assert str(shown[-1]["text"]).endswith(
            f"\n[{left_out} more open questions did not fit this answer, ask again]"
        )
        assert all("more open questions" not in str(e["text"]) for e in shown[:-1])
        assert sum(len(json.dumps(entry)) for entry in shown) <= ANSWER_BUDGET_BYTES


def test_one_question_longer_than_the_budget_is_cut_and_marked(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        (question,) = seed_questions(scheduler, 1, "w" * 200_000)
        daemon = daemon_for(project, log, scheduler)
        line = {"id": "r", "op": "questions", "v": 1, "args": {"operator": "ana"}}
        response = daemon.respond((json.dumps(line) + "\n").encode())
        assert response.ok and response.result is not None
        assert len(encode_response(response)) <= MAX_LINE_BYTES
        (entry,) = cast(list[dict[str, object]], response.result["questions"])
        assert entry["question_id"] == question and entry["text_truncated"] is True
        text = str(entry["text"])
        cut, _, mark = text.partition("\n")
        assert 0 < len(cut) < 200_000 and set(cut) == {"w"}
        assert mark == (
            "[cut by the daemon, 200000 characters in all, the whole is in the log]"
        )
        assert response.result["left_out"] == 0


def test_a_question_that_fits_is_not_marked(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        seed_questions(scheduler, 1, "Proceed?")
        daemon = daemon_for(project, log, scheduler)
        line = {"id": "r", "op": "questions", "v": 1, "args": {"operator": "ana"}}
        response = daemon.respond((json.dumps(line) + "\n").encode())
        assert response.result is not None
        (entry,) = cast(list[dict[str, object]], response.result["questions"])
        assert "text_truncated" not in entry and entry["text"] == "Proceed?"


def test_the_cut_of_a_body_fits_its_budget_for_any_text() -> None:
    from dromon import daemon as daemon_module

    ANSWER_BUDGET_BYTES = daemon_module.ANSWER_BUDGET_BYTES
    _cut_to_written = daemon_module._cut_to_written  # pyright: ignore[reportPrivateUsage]
    _written = daemon_module._written  # pyright: ignore[reportPrivateUsage]

    for text in ("x" * 90_000, '"' * 90_000, "\x01" * 90_000, "é" * 90_000, ""):
        cut = _cut_to_written(text, ANSWER_BUDGET_BYTES)
        assert _written(cut) <= ANSWER_BUDGET_BYTES
        assert text.startswith(cut)
        if _written(text) <= ANSWER_BUDGET_BYTES:
            assert cut == text
        else:
            assert _written(text[: len(cut) + 1]) > ANSWER_BUDGET_BYTES


def test_a_journal_entry_of_the_largest_size_is_cut_to_fit_one_line(
    project: Path,
) -> None:
    """Confirmed. A body of the model's largest size, 64 KiB, with its fields is over
    the line, so the entry could not be read through the operator's request."""
    from dromon.model import MAX_ROLE_NOTE_BYTES

    view: dict[str, object] = {
        "role_id": "core",
        "entry_id": "e-1",
        "chars": MAX_ROLE_NOTE_BYTES,
        "body": "b" * MAX_ROLE_NOTE_BYTES,
    }
    from dromon import daemon as daemon_module

    _bounded_entry = daemon_module._bounded_entry  # pyright: ignore[reportPrivateUsage]

    assert len(json.dumps(view)) > MAX_LINE_BYTES - 200
    cut = _bounded_entry(view)
    assert cut["body_truncated"] is True and cut["chars"] == MAX_ROLE_NOTE_BYTES
    # An older client ignores body_truncated, so the body says it was cut.
    assert str(cut["body"]).endswith(
        f"\n[cut by the daemon, {MAX_ROLE_NOTE_BYTES} characters in all,"
        " the whole is in the log]"
    )
    assert set(str(cut["body"]).partition("\n")[0]) == {"b"}
    assert len(encode_response(Response("r", ok=True, result=cut))) <= MAX_LINE_BYTES
    small: dict[str, object] = {"body": "short", "chars": 5}
    assert _bounded_entry(small) == small
    assert _bounded_entry({"chars": 0}) == {"chars": 0}


@pytest.mark.parametrize("unit", ["w", '"', "\x01", "é", "\n"])
def test_a_cut_text_with_its_mark_fits_for_any_character(unit: str) -> None:
    """The mark is part of the text, so it counts against the budget. For the
    characters that cost the most as written, a cut body and a cut question both
    leave the answer within its budget, and the mark is the last thing in the text."""
    from dromon import daemon as daemon_module

    written = daemon_module._written  # pyright: ignore[reportPrivateUsage]
    body = unit * 90_000
    entry = daemon_module._bounded_entry(  # pyright: ignore[reportPrivateUsage]
        {"body": body, "chars": len(body)}
    )
    assert written(entry["body"]) <= daemon_module.ANSWER_BUDGET_BYTES
    assert str(entry["body"]).endswith("the whole is in the log]")
    answer_cut = daemon_module._bounded_questions(  # pyright: ignore[reportPrivateUsage]
        [{"question_id": "m-1", "text": body, "choices": []}]
    )
    (question,) = cast(list[dict[str, object]], answer_cut["questions"])
    assert written(answer_cut) <= daemon_module.ANSWER_BUDGET_BYTES + 200
    assert str(question["text"]).endswith("the whole is in the log]")
    many = daemon_module._bounded_questions(  # pyright: ignore[reportPrivateUsage]
        [
            {"question_id": f"m-{n}", "text": unit * 9_000, "choices": []}
            for n in range(30)
        ]
    )
    shown = cast(list[dict[str, object]], many["questions"])
    assert 0 < len(shown) < 30 and many["left_out"] == 30 - len(shown)
    assert written(many) <= daemon_module.ANSWER_BUDGET_BYTES + 200
    assert str(shown[-1]["text"]).endswith("did not fit this answer, ask again]")


def test_an_older_client_that_prints_only_the_text_still_shows_every_cut(
    project: Path,
) -> None:
    """**Review.** A client written before round 19 prints ``text`` and ``body`` and
    ignores any key it does not know, so ``text_truncated``, ``body_truncated`` and
    ``left_out`` do not reach it. Built here by reading only the old keys of the
    answers, as it would. The cut is visible in the text it prints. The list of runs
    has no text to carry it, which ``docs/protocol/CLIENT.md`` records as a residual."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        seed_questions(scheduler, 12, "q" * 12_000)
        daemon = daemon_for(project, log, scheduler)
        line = {"id": "r", "op": "questions", "v": 1, "args": {"operator": "ana"}}
        response = daemon.respond((json.dumps(line) + "\n").encode())
        assert response.result is not None
        old_keys = {"question_id", "line", "operator", "text", "choices"}
        printed = [
            {key: value for key, value in entry.items() if key in old_keys}
            for entry in cast(list[dict[str, object]], response.result["questions"])
        ]
        assert "did not fit this answer" in str(printed[-1]["text"])


def test_retired_lines_and_roles_are_the_newest_few_with_their_totals(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They grow with the log, since a retired identifier is never used again. The view
    holds the newest ``MAX_RETIRED_SHOWN`` and the answer gives the total. The state is
    filled directly, since the view reads only the state."""
    from dromon.model import LineRetired, RoleRetired

    monkeypatch.setattr(scheduler_module, "MAX_RETIRED_SHOWN", 3)
    ana = OperatorAddress("ana")
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        state = log.state
        monkeypatch.setattr(type(log), "state", property(lambda _self: state))
        for number in range(7):
            instant = f"2026-11-1{number}T09:00:00Z"
            state.retired_lines[f"gone-{number}"] = LineRetired(
                f"gone-{number}", ana, instant
            )
            state.retired_roles[f"role-{number}"] = RoleRetired(
                f"role-{number}", ana, instant
            )
        assert [v["line_id"] for v in scheduler.retired_lines_view()] == [
            "gone-4",
            "gone-5",
            "gone-6",
        ]
        assert [v["role_id"] for v in scheduler.retired_roles_view()] == [
            "role-4",
            "role-5",
            "role-6",
        ]
        daemon = daemon_for(project, log, scheduler)
        status = daemon.status()
        assert status["retired_lines_total"] == 7
        assert len(cast(list[object], status["retired_lines"])) == 3
        request: dict[str, object] = {"id": "r", "op": "roles", "v": 1, "args": {}}
        roles = daemon.respond((json.dumps(request) + "\n").encode())
        assert roles.result is not None and roles.result["retired_total"] == 7


def test_the_shown_retired_ones_are_all_of_them_when_there_are_few(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dromon.model import LineRetired

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        state = log.state
        monkeypatch.setattr(type(log), "state", property(lambda _self: state))
        state.retired_lines["gone"] = LineRetired("gone", OperatorAddress("ana"), AT)
        assert [v["line_id"] for v in scheduler.retired_lines_view()] == ["gone"]
        assert daemon_for(project, log, scheduler).status()["retired_lines_total"] == 1


def test_a_line_view_holds_its_open_questions_within_what_the_messages_leave(
    project: Path,
) -> None:
    """Confirmed by arithmetic. Fifty questions of 5,000 characters are over the line
    with the messages' own budget, which the view already used."""
    from dromon.daemon import VIEW_QUESTIONS_BUDGET_BYTES

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        ids = seed_questions(scheduler, 50, "q" * 5_000)
        daemon = daemon_for(project, log, scheduler)
        request: dict[str, object] = {
            "id": "r",
            "op": "line_view",
            "v": 1,
            "args": {"line_id": "alpha"},
        }
        response = daemon.respond((json.dumps(request) + "\n").encode())
        assert response.ok and response.result is not None
        assert len(ids) * 5_000 > MAX_LINE_BYTES
        assert len(encode_response(response)) <= MAX_LINE_BYTES
        shown = cast(list[object], response.result["open_questions"])
        assert 0 < len(shown) < 50
        assert response.result["open_questions_left_out"] == 50 - len(shown)
        assert VIEW_QUESTIONS_BUDGET_BYTES + 40_000 + 8 * 1024 == MAX_LINE_BYTES
