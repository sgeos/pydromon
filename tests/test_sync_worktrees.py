"""``dromon sync-worktrees``, round 14, item 3. The daemon's ``roles`` answer and
``uv`` are both replaced, so no test reaches the network or a daemon."""

import argparse
from collections.abc import Sequence
from pathlib import Path

import pytest

from dromon import cli, exits, land
from dromon.land import Completed
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import ErrorCode, Operation, Response


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    return project_paths(tmp_path, short_runtime)


def worktree(
    paths: ProjectPaths, name: str, *, lock: bool = True, project: bool = True
) -> str:
    relative = f"tmp/worktrees/{name}"
    (paths.root / relative).mkdir(parents=True)
    if lock:
        (paths.root / relative / "uv.lock").write_text("version = 1\n")
    if project:
        (paths.root / relative / "pyproject.toml").write_text("[project]\n")
    return relative


def answering(roles: list[object]) -> object:
    def call(
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        **kwargs: object,
    ) -> Response:
        del socket, args, kwargs
        assert operation is Operation.ROLES
        return Response("r", ok=True, result={"roles": roles})

    return call


class Uv:
    """Stands in for both ``uv`` and the git calls that compare build inputs. Each
    worktree named in ``diffs`` differs from the base by that diff text, one named
    in ``untracked`` holds those untracked files, and one in ``git_failing`` has no
    base to compare with."""

    def __init__(
        self,
        failing: set[str],
        diffs: dict[str, str] | None = None,
        untracked: dict[str, str] | None = None,
        git_failing: set[str] | None = None,
    ) -> None:
        self.failing = failing
        self.diffs = diffs or {}
        self.untracked = untracked or {}
        self.git_failing = git_failing or set()
        self.ran: list[tuple[tuple[str, ...], Path]] = []
        self.compared: list[tuple[tuple[str, ...], Path]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | None = None,
        *,
        stream: bool = False,
        timeout: float = land.GIT_SECONDS,
    ) -> Completed:
        del stdin, stream, timeout
        if argv[0] == "git":
            self.compared.append((tuple(argv), cwd))
            if cwd.name in self.git_failing:
                return Completed(128, "", "fatal: bad revision 'origin/main'\n")
            if "diff" in argv:
                return Completed(0, self.diffs.get(cwd.name, ""))
            return Completed(0, self.untracked.get(cwd.name, ""))
        self.ran.append((tuple(argv), cwd))
        if cwd.name in self.failing:
            return Completed(1, "", "error: the lock file needs updating\n")
        return Completed(0, "Resolved\n")


def no_questions(question: str) -> bool:
    raise AssertionError(f"asked {question!r} with nothing to ask about")


def test_every_role_s_worktree_is_synced_and_reported(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = worktree(paths, "core")
    review = worktree(paths, "review")
    bare = worktree(paths, "docs", lock=False)
    roles: list[object] = [
        {"role_id": "review", "workdir": review},
        {"role_id": "core", "workdir": core},
        {"role_id": "core-two", "workdir": core},
        {"role_id": "docs", "workdir": bare},
        {"role_id": "away", "workdir": "../elsewhere"},
        "not a role",
        {"role_id": "broken"},
    ]
    monkeypatch.setattr(cli, "call", answering(roles))
    uv = Uv(failing={"review"})
    monkeypatch.setattr(land, "run_command", uv)
    monkeypatch.setattr(land, "ask_terminal", no_questions)
    code = cli._sync_worktrees(paths, argparse.Namespace())  # pyright: ignore[reportPrivateUsage]
    assert code == exits.FAILURE
    assert [cwd.name for _, cwd in uv.ran] == ["core", "review"]
    assert all(argv == ("uv", "sync", "--locked") for argv, _ in uv.ran)
    captured = capsys.readouterr()
    assert f"core {core}: build inputs match origin/main" in captured.out
    assert f"core {core}: synced" in captured.out
    assert f"core-two {core}: already handled for another role" in captured.out
    assert f"docs {bare}: skipped, no uv.lock" in captured.out
    assert "away ../elsewhere: skipped, outside the project" in captured.err
    assert "uv sync --locked failed with status 1: error: the lock file" in captured.err


def test_all_synced_is_a_success(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = worktree(paths, "core")
    monkeypatch.setattr(cli, "call", answering([{"role_id": "core", "workdir": core}]))
    monkeypatch.setattr(land, "run_command", Uv(failing=set()))
    monkeypatch.setattr(land, "ask_terminal", no_questions)
    assert cli._sync_worktrees(paths, argparse.Namespace()) == exits.OK  # pyright: ignore[reportPrivateUsage]


LOCK_DIFF = (
    "diff --git a/uv.lock b/uv.lock\n"
    '+source = { git = "https://example.invalid/evil" }\n'
)


def test_a_worktree_whose_build_inputs_differ_is_shown_and_asked_about(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = worktree(paths, "core")
    engines = worktree(paths, "engines")
    review = worktree(paths, "review")
    roles: list[object] = [
        {"role_id": "core", "workdir": core},
        {"role_id": "engines", "workdir": engines},
        {"role_id": "review", "workdir": review},
    ]
    monkeypatch.setattr(cli, "call", answering(roles))
    uv = Uv(
        failing=set(),
        diffs={"core": LOCK_DIFF, "engines": LOCK_DIFF},
        untracked={"review": "backend.py\x00"},
    )
    monkeypatch.setattr(land, "run_command", uv)
    asked: list[str] = []

    def ask(question: str) -> bool:
        asked.append(question)
        return "engines" in question

    monkeypatch.setattr(land, "ask_terminal", ask)
    code = cli._sync_worktrees(paths, argparse.Namespace(accept_changes=False))  # pyright: ignore[reportPrivateUsage]
    assert code == exits.FAILURE
    assert [cwd.name for _, cwd in uv.ran] == ["engines"]
    assert len(asked) == 3
    captured = capsys.readouterr()
    assert f"core {core}: build inputs differ from origin/main" in captured.out
    assert '    +source = { git = "https://example.invalid/evil" }' in captured.out
    assert "    untracked: backend.py" in captured.out
    assert f"core {core}: not synced, its build inputs were not accepted" in (
        captured.err
    )
    assert f"engines {engines}: synced" in captured.out
    diffs = [argv for argv, _ in uv.compared if "diff" in argv]
    assert all(
        {"--no-ext-diff", "--no-textconv", "core.fsmonitor=false"} <= set(argv)
        for argv in diffs
    )
    assert all(argv[-2:] == ("pyproject.toml", "uv.lock") for argv in diffs)


def test_accept_changes_syncs_after_showing_without_asking(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = worktree(paths, "core")
    monkeypatch.setattr(cli, "call", answering([{"role_id": "core", "workdir": core}]))
    uv = Uv(failing=set(), diffs={"core": LOCK_DIFF})
    monkeypatch.setattr(land, "run_command", uv)
    monkeypatch.setattr(land, "ask_terminal", no_questions)
    parsed = cli.build_parser().parse_args(["sync-worktrees", "--accept-changes"])
    assert cli._sync_worktrees(paths, parsed) == exits.OK  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert "build inputs differ from origin/main" in out
    assert "evil" in out
    assert "accepted with --accept-changes" in out
    assert [cwd.name for _, cwd in uv.ran] == ["core"]


def test_no_base_or_no_project_file_syncs_nothing(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = worktree(paths, "core")
    loose = worktree(paths, "loose", project=False)
    roles: list[object] = [
        {"role_id": "core", "workdir": core},
        {"role_id": "loose", "workdir": loose},
    ]
    monkeypatch.setattr(cli, "call", answering(roles))
    uv = Uv(failing=set(), git_failing={"core"})
    monkeypatch.setattr(land, "run_command", uv)
    monkeypatch.setattr(land, "ask_terminal", no_questions)
    parsed = cli.build_parser().parse_args(["sync-worktrees", "--accept-changes"])
    assert cli._sync_worktrees(paths, parsed) == exits.FAILURE  # pyright: ignore[reportPrivateUsage]
    assert uv.ran == []
    captured = capsys.readouterr()
    assert "core" in captured.err
    assert "not synced, git diff against origin/main failed with status 128" in (
        captured.err
    )
    assert f"loose {loose}: skipped, no pyproject.toml" in captured.out


def test_the_subcommand_is_offered() -> None:
    parsed = cli.build_parser().parse_args(["sync-worktrees"])
    assert parsed.command == "sync-worktrees"


# dromon renew, round 14, item 4, which shares this file's fakes.


def test_renew_sends_clear_session_and_says_what_is_kept(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[Operation, dict[str, object]]] = []

    def call(
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        **kwargs: object,
    ) -> Response:
        del socket, kwargs
        sent.append((operation, dict(args or {})))
        return Response("r", ok=True, result={"line_id": "core"})

    monkeypatch.setattr(cli, "call", call)
    parsed = cli.build_parser().parse_args(["renew", "core"])
    assert cli._renew(paths, parsed) == exits.OK  # pyright: ignore[reportPrivateUsage]
    assert sent == [(Operation.CLEAR_SESSION, {"line_id": "core"})]
    assert "fresh engine session, with its mail, brief and files kept" in (
        capsys.readouterr().out
    )


def test_renew_of_a_line_in_a_turn_is_refused(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def call(
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        **kwargs: object,
    ) -> Response:
        del socket, operation, args, kwargs
        return Response(
            "r",
            ok=False,
            error_code=ErrorCode.REJECTED,
            error_message="line core is in a turn. Interrupt it first",
        )

    monkeypatch.setattr(cli, "call", call)
    parsed = cli.build_parser().parse_args(["renew", "core"])
    assert cli._renew(paths, parsed) == exits.FAILURE  # pyright: ignore[reportPrivateUsage]
    assert "in a turn" in capsys.readouterr().err
