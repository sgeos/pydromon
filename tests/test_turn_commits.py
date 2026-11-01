"""Each turn names the commits its state rests on. **Round 14 task of 2026-11-01.**
A line acted on a standing brief written at an earlier commit, and on a daemon whose
loaded code its checkout had left. Each turn's preamble now names the brief's commit
beside the worktree's ``HEAD``, and the daemon's loaded commit beside its checkout's,
with a plain warning when a pair differs."""

from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import BriefSet
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
BRIEFED = "a" * 40
MOVED = "b" * 40
LOADED = "c" * 40
CHECKED_OUT = "d" * 40
OLDER = "e" * 40
AT = "2026-11-01T09:00:00Z"


def checkout(root: Path, commit: str) -> Path:
    """A directory whose ``.git`` holds a detached ``HEAD`` at ``commit``, which is
    all the daemon reads of a checkout."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(f"{commit}\n")
    return root


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


def fixed(commit: str | None) -> Callable[[str], str | None]:
    def head(line_id: str) -> str | None:
        del line_id
        return commit

    return head


class Turns:
    def __init__(self, tmp_path: Path, loaded: str | None, checked_out: str) -> None:
        self.log = EventLog(tmp_path / "events.sqlite")
        self.scheduler = Scheduler(
            self.log,
            no_engine,
            tick_seconds=0,
            daemon_commit=loaded,
            daemon_checkout=checkout(tmp_path / "daemon", checked_out),
        )
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.head: str | None = BRIEFED

    def brief(self, commit: str, daemon: str = LOADED) -> None:
        self.log.append(
            BriefSet("alpha", "the task", "", (), "the next step", commit, daemon, AT)
        )

    def notice(self) -> str | None:
        return self.scheduler._commits_notice("alpha")  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Turns]:
    target = Turns(tmp_path, LOADED, LOADED)

    def head(line_id: str) -> str | None:
        del line_id
        return target.head

    monkeypatch.setattr(target.scheduler, "_workdir_commit", head)
    yield target
    target.log.close()


def test_matching_commits_are_named_without_a_warning(turns: Turns) -> None:
    turns.brief(BRIEFED)
    notice = turns.notice()
    assert notice == (
        f"Your standing brief names worktree commit {BRIEFED[:12]}, and your"
        f" worktree is at {BRIEFED[:12]}. The daemon loaded commit {LOADED[:12]},"
        f" and its checkout is at {LOADED[:12]}."
    )


def test_a_brief_from_an_earlier_commit_is_warned_of(turns: Turns) -> None:
    turns.brief(BRIEFED)
    turns.head = MOVED
    notice = turns.notice()
    assert notice is not None
    assert "WARNING: they differ, so the brief describes an earlier state." in notice
    assert f"git log --oneline {BRIEFED[:12]}..{MOVED[:12]}" in notice


def test_a_daemon_whose_checkout_moved_is_warned_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = Turns(tmp_path, LOADED, CHECKED_OUT)
    monkeypatch.setattr(turns.scheduler, "_workdir_commit", fixed(BRIEFED))
    try:
        notice = turns.notice()
        assert notice == (
            f"The daemon loaded commit {LOADED[:12]}, and its checkout is at"
            f" {CHECKED_OUT[:12]}. WARNING: they differ, so the running daemon may"
            " lack code its checkout holds until an operator restarts it."
        )
    finally:
        turns.log.close()


def test_no_brief_or_no_readable_head_names_only_what_is_known(turns: Turns) -> None:
    notice = turns.notice()
    assert notice is not None and "standing brief" not in notice
    turns.brief(BRIEFED)
    turns.head = None
    notice = turns.notice()
    assert notice is not None and "standing brief" not in notice


def test_a_brief_with_no_recorded_commit_is_not_compared(turns: Turns) -> None:
    turns.brief("")
    notice = turns.notice()
    assert notice is not None and "standing brief" not in notice


def test_nothing_known_gives_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = Turns(tmp_path, None, LOADED)
    monkeypatch.setattr(turns.scheduler, "_workdir_commit", fixed(None))
    try:
        assert turns.notice() is None
    finally:
        turns.log.close()


def paths_holding(value: object, wanted: str, path: str = "") -> list[str]:
    """Every key path in a ``line_status`` answer whose value is ``wanted``."""
    found: list[str] = []
    pending: list[tuple[str, object]] = [(path, value)]
    # Bounded by the size of the answer, since each pass consumes one node.
    while pending:
        where, node = pending.pop()
        if node == wanted:
            found.append(where)
        elif isinstance(node, dict):
            items = cast(dict[str, object], node).items()
            pending.extend((f"{where}.{key}", child) for key, child in items)
        elif isinstance(node, list):
            children = cast(list[object], node)
            pending.extend((f"{where}[{index}]", c) for index, c in enumerate(children))
    return sorted(found)


@pytest.mark.parametrize("detail", [True, False])
def test_a_brief_set_under_an_older_daemon_names_it_only_as_its_stamp(
    turns: Turns, detail: bool
) -> None:
    """**Follow-up of 2026-11-01.** A line reported an older daemon commit from
    ``line_status``. The brief's stamp was named ``daemon_commit``, as the running
    commit is, so it could be taken for it. Every field that names the older commit
    now says it was taken when the brief was set."""
    turns.brief(BRIEFED, daemon=OLDER)
    status = turns.scheduler.line_status("alpha", detail=detail, lists=False)
    assert paths_holding(status, OLDER) == [".brief.daemon_commit_when_set"]
    commits = cast(dict[str, object], status["commits"])
    assert commits["daemon"] == LOADED
    assert f"under the daemon at commit {LOADED}" in str(commits["cite"])
