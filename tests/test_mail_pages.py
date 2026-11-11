"""A page of the operator's mail says what lies outside it. **Round 16, at the
operator's request, after an operator who read the tail of a page of 20, the oldest,
missed the newest reports.** The notes of ``dromon mail`` follow from counts the
daemon gives, which these tests derive from the mailbox itself."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon.cli import _page_notes  # pyright: ignore[reportPrivateUsage]
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.mailbox import listing, unread_outside
from dromon.model import LineAddress, MessageRead, OperatorAddress
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import Response
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    return project_paths(tmp_path, short_runtime)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def seed(paths: ProjectPaths, count: int, read_first: int = 0) -> list[str]:
    """``count`` notes to @ana, alpha's and beta's alternately, the oldest
    ``read_first`` of which she has read."""
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        ids = [
            scheduler.send(ALPHA if n % 2 == 0 else BETA, ANA, f"note {n}")
            for n in range(count)
        ]
        scheduler.collect(ANA)
        for message_id in ids[:read_first]:
            log.append(MessageRead(message_id))
        return ids


def ask(paths: ProjectPaths, args: dict[str, object]) -> dict[str, object]:
    with EventLog(paths.event_log) as log:
        daemon = Daemon(
            paths,
            log,
            "2026-10-25T00:00:00Z",
            Scheduler(log, no_engine, hold_wakes=True),
        )
        line = (
            json.dumps({"id": "r", "op": "mail", "v": 1, "args": args}) + "\n"
        ).encode()
        response: Response = daemon.respond(line)
    assert response.ok and response.result is not None, response
    return response.result


def test_the_daemon_counts_the_unread_mail_before_and_after_a_page(
    paths: ProjectPaths,
) -> None:
    seed(paths, 10, read_first=2)
    middle = ask(paths, {"operator": "ana", "offset": 4, "limit": 3})
    # Matches 0 and 1 were read, so before the page 2 and 3 are unread. After it,
    # the matches 7, 8 and 9 are all unread.
    assert (middle["unread_before"], middle["unread_after"]) == (2, 3)
    assert middle["total"] == 10
    first = ask(paths, {"operator": "ana", "limit": 4})
    assert (first["unread_before"], first["unread_after"]) == (0, 6)
    last = ask(paths, {"operator": "ana", "offset": 6, "limit": 20})
    assert (last["unread_before"], last["unread_after"]) == (4, 0)


def test_the_counts_follow_the_mailbox_not_a_constant(paths: ProjectPaths) -> None:
    """Derived on both sides, for every page of a mailbox with some mail read."""
    seed(paths, 9, read_first=3)
    with EventLog(paths.event_log) as log:
        state = log.state
        matches, _ = listing(state, ANA, limit=50)
        for offset in range(0, 10):
            for limit in (1, 2, 5, 50):
                before, after = unread_outside(state, ANA, offset=offset, limit=limit)
                page = {
                    m for m, _ in listing(state, ANA, offset=offset, limit=limit)[0]
                }
                outside = [(m, msg) for m, msg in matches if m not in page]
                unread_ids = {m for m, msg in matches if not msg.read}
                position = {m: i for i, (m, _) in enumerate(matches)}
                assert before == sum(
                    1 for m, _ in outside if m in unread_ids and position[m] < offset
                )
                assert after == sum(
                    1 for m, _ in outside if m in unread_ids and position[m] >= offset
                )
                assert before + after + sum(1 for m in page if m in unread_ids) == len(
                    unread_ids
                )


def test_the_counts_follow_the_filters(paths: ProjectPaths) -> None:
    ids = seed(paths, 10, read_first=1)
    only = ask(paths, {"operator": "ana", "sender": "alpha", "limit": 2})
    # Alpha sent the notes 0, 2, 4, 6 and 8, and note 0 was read. The page holds the
    # first two, so three of its notes follow it, all unread.
    assert only["total"] == 5
    assert (only["unread_before"], only["unread_after"]) == (0, 3)
    unread = ask(paths, {"operator": "ana", "unread_only": True, "limit": 3})
    assert unread["total"] == 9
    assert (unread["unread_before"], unread["unread_after"]) == (0, 6)
    del ids


def test_a_bad_page_is_refused_by_the_count_as_by_the_listing(
    paths: ProjectPaths,
) -> None:
    seed(paths, 2)
    with EventLog(paths.event_log) as log:
        with pytest.raises(ValueError, match="offset must be at least 0"):
            unread_outside(log.state, ANA, offset=-1)
        with pytest.raises(ValueError, match="limit in"):
            unread_outside(log.state, ANA, limit=0)


# The client's notes.


def test_a_page_that_holds_everything_says_nothing() -> None:
    assert _page_notes({"total": 3, "unread_after": 0, "unread_before": 0}, 0, 3) == []


def test_a_first_page_says_where_it_ends_and_how_much_unread_waits() -> None:
    notes = _page_notes({"total": 30, "unread_after": 4, "unread_before": 0}, 0, 20)
    assert notes == [
        "showing matches 1 to 20 of 30, oldest first",
        "4 unread after this page, the newest of them among them. --offset 20"
        " shows them",
    ]


def test_a_later_page_names_the_unread_before_it() -> None:
    notes = _page_notes({"total": 30, "unread_after": 0, "unread_before": 2}, 20, 10)
    assert notes == [
        "showing matches 21 to 30 of 30, oldest first",
        "2 unread before this page. --offset 0 shows them",
    ]


def test_a_page_past_the_end_says_it_is_empty() -> None:
    notes = _page_notes({"total": 5, "unread_after": 0, "unread_before": 1}, 50, 0)
    assert notes[0] == "this page is empty, the 5 matches are all before --offset"
    assert notes[1] == "1 unread before this page. --offset 0 shows them"
    assert _page_notes({"total": 0}, 0, 0) == []


def test_an_older_daemon_still_gets_the_position_note() -> None:
    """A daemon that sends no unread counts, so the rollout needs no pairing."""
    assert _page_notes({"total": 30}, 0, 20) == [
        "showing matches 1 to 20 of 30, oldest first"
    ]


@pytest.mark.parametrize("bad", [None, "30", 3.5, True])
def test_a_total_that_is_not_an_integer_gives_no_notes(bad: object) -> None:
    result = cast(dict[str, object], {"total": bad, "unread_after": 2})
    assert _page_notes(result, 0, 2) == []


def test_dromon_mail_prints_the_notes_after_the_page(
    paths: ProjectPaths,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = seed(paths, 25, read_first=0)

    def answer(
        _paths: ProjectPaths, _name: str, _operation: object, args: dict[str, object]
    ) -> dict[str, object]:
        return ask(paths, args)

    monkeypatch.setattr(cli, "_call", answer)
    assert (
        cli.main(
            [
                "--project",
                str(paths.root),
                "--runtime-dir",
                str(short_runtime),
                "mail",
                "--operator",
                "ana",
                "--limit",
                "10",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count(" from alpha") + out.count(" from beta") == 10
    assert ids[0] in out and ids[9] in out and ids[10] not in out
    assert "showing matches 1 to 10 of 25, oldest first" in out
    assert "15 unread after this page, the newest of them among them." in out
    assert "--offset 10 shows them" in out
