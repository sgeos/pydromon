"""Task of 2026-11-05, item 4. ``dromon ci-log`` copies a GitHub Actions run's failed
job log into a line's scratch, through ``gh`` as the operator, after asking. ``gh`` is
stood in for, since no test may reach the network or the operator's credentials."""

import json
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cilog, cli, land
from dromon.disclosure import load_terms
from dromon.driver import Strategy
from dromon.eventlog import EventLog, read_entries
from dromon.model import (
    HARNESS,
    DisclosureChecked,
    EventRejected,
    LineAddress,
    OperatorAddress,
)
from dromon.protocol import Operation, Response
from dromon.scheduler import MAX_SCRATCH_BYTES, LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
SUMMARY = {
    "name": "CI",
    "displayTitle": "fix: a thing",
    "headBranch": "feat/x",
    "headSha": "a" * 40,
    "conclusion": "failure",
    "status": "completed",
    "url": "https://github.com/o/r/actions/runs/123",
    "jobs": [
        {"name": "test (macos, 3.15)", "databaseId": 77, "conclusion": "failure"},
        {"name": "test (ubuntu, 3.12)", "databaseId": 78, "conclusion": "success"},
        "not a job",
    ],
}
LOG = "test\tstep\tcollecting\ntest\tstep\tE   Timeout >300s\x1b[0m\n"


# The parts that need neither gh nor the daemon.


@pytest.mark.parametrize("value", ["", "0", "-1", "12a", "1" * 21, "1 2"])
def test_an_identifier_is_a_positive_whole_number(value: str) -> None:
    with pytest.raises(cilog.CiLogRefused, match="positive whole number"):
        cilog.check_id("run", value)


def test_the_gh_commands_name_the_run_and_the_job() -> None:
    assert cilog.check_id("run", "123") == "123"
    assert cilog.summary_argv("123")[:4] == ["gh", "run", "view", "123"]
    assert cilog.log_argv("123", None) == ["gh", "run", "view", "123", "--log-failed"]
    assert cilog.log_argv("123", "77")[-2:] == ["--job", "77"]


def test_a_summary_names_the_failed_jobs_in_printable_text() -> None:
    raw = dict(SUMMARY, name="CI\x1b[2J")
    summary = cilog.parse_summary(json.dumps(raw))
    assert summary.name == "CI[2J"
    assert summary.failed_jobs == ("test (macos, 3.15) (77)",)
    lines = cilog.describe("123", summary)
    assert lines[0] == "run 123: CI[2J, fix: a thing"
    assert lines[2] == "failed jobs: test (macos, 3.15) (77)"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not json", "not JSON"),
        ("[1]", "not an object"),
        (json.dumps({"name": "x" * cilog.MAX_SUMMARY_BYTES}), "larger"),
    ],
)
def test_a_summary_that_is_not_one_is_refused(raw: str, reason: str) -> None:
    with pytest.raises(cilog.CiLogRefused, match=reason):
        cilog.parse_summary(raw)


def test_a_tail_keeps_whole_last_lines_within_its_bound() -> None:
    log = "".join(f"line {n:04d}\n" for n in range(1000))
    kept, total = cilog.tail(log, 100)
    assert total == len(log.encode())
    assert len(kept.encode()) <= 100
    assert kept.endswith("line 0999\n")
    assert all(line.startswith("line ") for line in kept.splitlines())
    assert cilog.tail("short\n") == ("short\n", 6)


def test_a_tail_drops_control_characters_and_cuts_one_long_line() -> None:
    kept, _ = cilog.tail("a\x1b[31mb\x07\tc\n")
    assert kept == "a[31mb\tc\n"
    kept, _ = cilog.tail("x" * 500, 100)
    assert kept == "x" * 100


def test_the_document_says_first_that_it_is_untrusted() -> None:
    summary = cilog.parse_summary(json.dumps(SUMMARY))
    text = cilog.document("123", "77", summary, "log\n", 900, "ana")
    assert text.startswith("Untrusted data, the failed steps' log of GitHub")
    assert "run 123, job 77" in text and "operator ana" in text
    assert "the last 4 of 900 bytes" in text
    assert text.endswith("\nlog\n")
    assert cilog.scratch_path("123", None) == "ci/run-123.log"
    assert cilog.scratch_path("123", "77") == "ci/run-123-job-77.log"


# The daemon's side, a placement in the line's scratch.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(
            log,
            no_engine,
            hold_wakes=True,
            wall_clock=lambda: datetime(2026, 11, 5, 3, tzinfo=UTC),
            root=tmp_path,
        )
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        yield found


def scratch(scheduler: Scheduler, root: Path) -> Path:
    """The line's scratch, as ``scratch_parts`` gives it."""
    return root.joinpath(*scheduler.scratch_parts("alpha"))


def notes(scheduler: Scheduler) -> list[str]:
    return [
        message.body
        for message in scheduler._log.state.messages.values()  # pyright: ignore[reportPrivateUsage]
        if message.sender == HARNESS and message.recipient == LineAddress("alpha")
    ]


def test_an_operator_places_a_file_in_a_lines_scratch(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    written = scheduler.place_scratch(
        OperatorAddress("ana"), "alpha", "ci/run-1.log", "the log\n"
    )
    target = scratch(scheduler, tmp_path) / "ci" / "run-1.log"
    assert written.relative == str(target.relative_to(tmp_path))
    assert target.read_text() == "the log\n"
    (note,) = notes(scheduler)
    assert note.startswith(f"Operator ana placed {written.relative}, 8 bytes")
    assert "untrusted data" in note


def test_a_placement_follows_no_link_a_line_planted(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = scratch(scheduler, tmp_path) / "ci"
    planted.parent.mkdir(parents=True)
    planted.symlink_to(outside)
    with pytest.raises(EventRejected):
        scheduler.place_scratch(OperatorAddress("ana"), "alpha", "ci/x.log", "x")
    assert list(outside.iterdir()) == []
    planted.unlink()
    planted.mkdir()
    (planted / "x.log").symlink_to(outside / "target")
    with pytest.raises(EventRejected, match="symbolic link"):
        scheduler.place_scratch(OperatorAddress("ana"), "alpha", "ci/x.log", "x")
    assert not (outside / "target").exists()
    assert notes(scheduler) == []


def test_a_placement_is_not_checked_for_disclosure_and_spends_nothing(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    (tmp_path / "terms").write_text("codename\n")
    scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    scheduler.place_scratch(OperatorAddress("ana"), "alpha", "ci/a.log", "codename\n")
    checked = [
        entry.event
        for entry in read_entries(tmp_path / "events.sqlite")
        if isinstance(entry.event, DisclosureChecked)
    ]
    assert checked == []


def test_a_placed_file_is_never_answered_as_clean_from_memory(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    """Round 15 review, C1. ``check_disclosure`` answers a file whose bytes already
    passed from its memory, and ``write_scratch``'s screened text is remembered as
    passed. A placement is written unscreened, so it must never be remembered, or a
    placed log holding a listed term would be answered clean. Both changes are
    present here, as the memory answering a written file shows."""
    (tmp_path / "terms").write_text("codename\n")
    scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    written = scheduler.write_scratch_file("alpha", "w.md", "screened\n")
    answer = scheduler.check_disclosure("alpha", None, written.relative)
    assert answer.cached and answer.matched == 0
    placed = scheduler.place_scratch(
        OperatorAddress("ana"), "alpha", "ci/a.log", "the codename\n"
    )
    answer = scheduler.check_disclosure("alpha", None, placed.relative)
    assert not answer.cached and answer.matched == 1
    clean = scheduler.place_scratch(
        OperatorAddress("ana"), "alpha", "ci/b.log", "clean\n"
    )
    first = scheduler.check_disclosure("alpha", None, clean.relative)
    assert not first.cached and first.matched == 0
    # Once the daemon itself has matched the placed bytes, they passed, and only
    # then is the file answered from memory.
    assert scheduler.check_disclosure("alpha", None, clean.relative).cached
    recorded = [
        entry.event
        for entry in read_entries(tmp_path / "events.sqlite")
        if isinstance(entry.event, DisclosureChecked)
    ]
    assert [event.matched_lines for event in recorded] == [1, 0]


@pytest.mark.parametrize(
    ("line_id", "path", "size", "reason"),
    [
        ("omega", "ci/a.log", 1, "no line omega"),
        ("alpha", "../a.log", 1, "may not contain"),
        ("alpha", "/etc/a.log", 1, "relative path"),
        ("alpha", "ci/a.log", MAX_SCRATCH_BYTES + 1, "over"),
    ],
)
def test_a_placement_is_refused_as_write_scratch_refuses(
    scheduler: Scheduler, line_id: str, path: str, size: int, reason: str
) -> None:
    with pytest.raises(EventRejected, match=reason):
        scheduler.place_scratch(OperatorAddress("ana"), line_id, path, "x" * size)


# The command, end to end, with gh stood in for.


class Gh:
    """Stands in for ``land.run_command`` on ``gh``, recording each command."""

    def __init__(self, summary_code: int = 0, log_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.summary_code = summary_code
        self.log_code = log_code

    def __call__(
        self, argv: Sequence[str], cwd: Path, stdin: str | None = None, **_: object
    ) -> land.Completed:
        self.calls.append(list(argv))
        if "--json" in argv:
            return land.Completed(self.summary_code, json.dumps(SUMMARY), "no run")
        return land.Completed(self.log_code, LOG, "no log")


def daemon_call(
    scheduler: Scheduler,
    tmp_path: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Route the client's requests to a daemon around ``scheduler``, and return the
    base arguments for ``cli.main``.

    ``runtime`` comes from ``short_runtime``, since the socket path is checked against
    the Unix limit even though no socket is opened, and pytest's own temporary paths
    outside a sandbox exceed it."""
    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.paths import project_paths
    from dromon.projectconfig import ProjectConfig

    paths = project_paths(tmp_path, runtime)
    daemon = Daemon(
        paths,
        scheduler._log,  # pyright: ignore[reportPrivateUsage]
        "2026-11-05T03:00:00Z",
        scheduler,
        project=ProjectConfig(),
        command_line=Overrides(),
        project_file_present=False,
    )

    def call(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        line = {"id": "r", "op": operation.value, "v": 1, "args": args or {}}
        return daemon.respond((json.dumps(line) + "\n").encode())

    monkeypatch.setattr(cli, "call", call)
    return ["--project", str(tmp_path), "--runtime-dir", str(runtime)]


def test_ci_log_shows_the_run_asks_and_copies_on_a_yes(
    scheduler: Scheduler,
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = daemon_call(scheduler, tmp_path, short_runtime, monkeypatch)
    gh = Gh()
    asked: list[str] = []
    answers = [False, True]

    def ask(question: str) -> bool:
        asked.append(question)
        return answers[len(asked) - 1]

    monkeypatch.setattr(land, "run_command", gh)
    monkeypatch.setattr(land, "ask_terminal", ask)
    assert cli.main([*base, "ci-log", "alpha", "123", "--operator", "ana"]) == 1
    assert "not copied" in capsys.readouterr().err
    assert len(gh.calls) == 1 and "--json" in gh.calls[0]
    assert not (scratch(scheduler, tmp_path) / "ci").exists()
    assert cli.main([*base, "ci-log", "alpha", "123", "--operator", "ana"]) == 0
    out = capsys.readouterr().out
    assert "failed jobs: test (macos, 3.15) (77)" in out
    assert "copied" in out and "line alpha was told" in out
    assert asked[-1] == (
        "Copy its failed steps' log into line alpha's scratch as ci/run-123.log?"
    )
    text = (scratch(scheduler, tmp_path) / "ci" / "run-123.log").read_text()
    assert text.startswith("Untrusted data")
    assert text.endswith("test\tstep\tE   Timeout >300s[0m\n")
    assert gh.calls[-1] == cilog.log_argv("123", None)


def test_ci_log_with_yes_asks_nothing_and_names_the_job(
    scheduler: Scheduler,
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = daemon_call(scheduler, tmp_path, short_runtime, monkeypatch)
    gh = Gh()
    monkeypatch.setattr(land, "run_command", gh)

    def never(question: str) -> bool:
        raise AssertionError(f"asked {question}")

    monkeypatch.setattr(land, "ask_terminal", never)
    args = [*base, "ci-log", "alpha", "123", "--job", "77", "--yes"]
    assert cli.main(args) == 0
    assert (scratch(scheduler, tmp_path) / "ci" / "run-123-job-77.log").is_file()
    assert gh.calls[-1] == cilog.log_argv("123", "77")


@pytest.mark.parametrize(
    ("gh", "args", "code", "reason"),
    [
        (Gh(summary_code=1), ["alpha", "123", "--yes"], 1, "could not show run"),
        (Gh(log_code=1), ["alpha", "123", "--yes"], 1, "could not give the log"),
        (Gh(), ["alpha", "12x", "--yes"], 2, "positive whole number"),
        (Gh(), ["alpha", "123", "--job", "-3", "--yes"], 2, "positive whole number"),
        (Gh(), ["omega", "123", "--yes"], 1, "no line omega"),
    ],
)
def test_ci_log_copies_nothing_on_a_failure(
    scheduler: Scheduler,
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gh: Gh,
    args: list[str],
    code: int,
    reason: str,
) -> None:
    base = daemon_call(scheduler, tmp_path, short_runtime, monkeypatch)
    monkeypatch.setattr(land, "run_command", gh)
    assert cli.main([*base, "ci-log", *args]) == code
    assert reason in capsys.readouterr().err
    assert not (scratch(scheduler, tmp_path) / "ci").exists()
