"""Row 14 of the T3 Code review. The event log's size and growth in ``dromon status``
and ``line_status``, with an unknown size kept apart from zero. No compaction yet."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import LineCreated, OperatorRegistered
from dromon.configview import Overrides
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig
from dromon.scheduler import MIN_RATE_SECONDS, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Any:  # noqa: ANN401
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def test_the_log_s_size_counts_its_files_and_grows(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        log.append(OperatorRegistered("ana"))
    with EventLog(path) as log:
        assert log.opened_seq == 1
        assert log.opened_bytes is not None and log.opened_bytes > 0
        for number in range(200):
            log.append(OperatorRegistered(f"op{number}"))
        size = log.size_bytes()
        assert size is not None and size > log.opened_bytes
        expected = sum(
            candidate.stat().st_size
            for candidate in tmp_path.iterdir()
            if candidate.name.startswith("events.sqlite")
        )
        assert size == expected


def test_a_size_that_cannot_be_read_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:

        def refuse(self: Path, **_: object) -> object:
            raise PermissionError(f"no access to {self}")

        monkeypatch.setattr(Path, "stat", refuse)
        assert log.size_bytes() is None


def test_growth_is_counted_from_the_daemon_s_start(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        log.append(OperatorRegistered("ana"))
    clock = Clock()
    with EventLog(path) as log:
        scheduler = Scheduler(log, no_engine, clock=clock, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        early = scheduler.event_log_status()
        assert early["events"] == log.last_seq
        since = cast(dict[str, object], early["since_start"])
        assert since["events"] == log.last_seq - 1
        assert early["events_per_hour"] is None and early["bytes_per_hour"] is None
        assert early["compaction"] == "none"
        clock.now += MIN_RATE_SECONDS * 60
        later = scheduler.event_log_status()
        hours = MIN_RATE_SECONDS * 60 / 3600
        assert later["events_per_hour"] == round((log.last_seq - 1) / hours, 1)
        grown = cast(dict[str, object], later["since_start"])
        assert grown["seconds"] == MIN_RATE_SECONDS * 60
        assert grown["bytes"] == cast(int, later["bytes"]) - cast(int, log.opened_bytes)


def test_an_unknown_size_gives_no_growth_in_bytes(tmp_path: Path) -> None:
    clock = Clock()
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, clock=clock, hold_wakes=True)
        log.size_bytes = lambda: None  # type: ignore[method-assign]
        clock.now += MIN_RATE_SECONDS
        status = scheduler.event_log_status()
        assert status["bytes"] is None
        assert cast(dict[str, object], status["since_start"])["bytes"] is None
        assert status["bytes_per_hour"] is None
        assert status["events_per_hour"] == 0.0


def test_line_status_shows_the_log(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        shown = cast(dict[str, object], scheduler.line_status("alpha")["event_log"])
        assert set(shown) == set(scheduler.event_log_status())
        assert shown["events"] == log.last_seq


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


@contextmanager
def attached(paths: ProjectPaths) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        yield Daemon(
            paths,
            log,
            "2026-10-27T00:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )


def test_daemon_status_shows_the_log_s_size_and_growth(paths: ProjectPaths) -> None:
    with attached(paths) as daemon:
        daemon.log.append(LineCreated("alpha", HAIKU, "subscription"))
        log = cast(dict[str, object], daemon.status()["event_log"])
        assert log["last_seq"] == log["events"] == 1
        assert type(log["bytes"]) is int
        assert {"since_start", "events_per_hour", "compaction"} <= set(log)


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        (
            {
                "bytes": 3 * 1024 * 1024,
                "since_start": {"events": 12, "bytes": 2048},
                "events_per_hour": 40.0,
                "compaction": "none",
            },
            "event log 3.0 MiB, 12 events and 2.0 KiB since the daemon started,"
            " 40.0 events an hour, compaction none",
        ),
        (
            {
                "bytes": None,
                "since_start": {"events": 0, "bytes": None},
                "events_per_hour": None,
                "compaction": "none",
            },
            "event log unknown size, 0 events and unknown size since the daemon"
            " started, rate not yet known, compaction none",
        ),
        (
            {},
            "event log unknown size, unknown events and unknown size since the daemon"
            " started, rate not yet known, compaction unknown",
        ),
    ],
)
def test_dromon_status_prints_unknown_as_unknown(
    log: dict[str, object], expected: str
) -> None:
    assert cli._log_growth(log) == expected  # pyright: ignore[reportPrivateUsage]
