"""``dromon land``, against real git in a temporary repository whose remote is a
bare repository beside it, so no test reaches a real remote. The gate and ``gh`` are
replaced by a recording runner, and every other command runs for real."""

import os
import stat
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from dromon import cli, exits, land
from dromon.land import Completed, Lander, Request

TERM = "zqxjvorbit"
"""A made-up word for the tests' own disclosure list."""

PR_URL = "https://example.invalid/sgeos/demo/pull/7"


def git(directory: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(directory), *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout.strip()


def write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git with no user or system configuration, a fixed identity and no hooks, for
    the tests' own commands and for the runner's alike."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    """A clone on the branch ``feat/thing``, at the same commit as ``main``."""
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    write(work, "README.md", "readme\n")
    write(work, "docs/process/HANDOFF.md", "one\ntwo\n")
    write(work, "src/a.py", "a = 1\nb = 2\nc = 3\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "seed")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-q", "-c", "feat/thing")
    return work


@pytest.fixture
def terms(tmp_path: Path) -> Path:
    path = tmp_path / "terms.txt"
    path.write_text(f"# the tests' list\n{TERM}\n")
    return path


class Recording:
    """Runs git for real, captured, and answers for the gate and ``gh``."""

    def __init__(self, gate_code: int = 0, gh_code: int = 0) -> None:
        self.gate_code = gate_code
        self.gh_code = gh_code
        self.calls: list[tuple[str, ...]] = []
        self.gh_input: list[str | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | None = None,
        *,
        stream: bool = False,
        timeout: float = land.GIT_SECONDS,
    ) -> Completed:
        del stream
        self.calls.append(tuple(argv))
        if tuple(argv) == land.GATE:
            return Completed(self.gate_code)
        if argv[0] == "gh":
            self.gh_input.append(stdin)
            return Completed(self.gh_code, PR_URL + "\n", "gh: refused\n")
        return land.run_command(argv, cwd, stdin, timeout=timeout)


class Said:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []
        self.asked: list[str] = []
        self.answers: list[bool] = []

    def ask(self, question: str) -> bool:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else False

    @property
    def text(self) -> str:
        return "\n".join(self.out + self.err)


def lander(
    repo: Path,
    said: Said,
    runner: Recording | None = None,
    **given: object,
) -> Lander:
    values: dict[str, object] = {
        "worktree": repo,
        "message": "feat: a thing\n\nWhy it matters.\n",
        "title": "feat: a thing",
        "body": "Summary.\n",
    }
    values.update(given)
    request = Request(**values)  # pyright: ignore[reportArgumentType]
    return Lander(
        request,
        run=runner or Recording(),
        ask=said.ask,
        say=said.out.append,
        warn=said.err.append,
    )


def staged(repo: Path) -> str:
    return git(repo, "diff", "--cached", "--name-only")


# The dry run.


def test_a_dry_run_lists_every_path_and_changes_nothing(
    repo: Path, terms: Path
) -> None:
    write(repo, "src/a.py", "a = 1\nb = 20\nc = 3\n")
    write(repo, "src/new.py", "x = 1\n")
    (repo / "README.md").unlink()
    head = git(repo, "rev-parse", "HEAD")
    said = Said()
    code = lander(repo, said, terms_files=(terms,), dry_run=True).land()
    assert code == exits.OK, said.text
    out = "\n".join(said.out)
    assert " M src/a.py" in out
    assert "?? src/new.py" in out
    assert " D README.md" in out
    assert "3 paths to stage" in out
    assert "dry run, so nothing was staged or committed" in out
    assert said.asked == []
    assert staged(repo) == ""
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") != ""


# Refusals.


@pytest.mark.parametrize(
    "path",
    [
        "docs/process/HANDOFF.md",
        "docs/roadmap/V0_2_0.md",
        "secret/token.txt",
        ".dromon/dromon.toml",
        "DOCS/ROADMAP/upper.md",
    ],
)
def test_a_protected_path_refuses_the_whole_land(
    repo: Path, terms: Path, path: str
) -> None:
    write(repo, path, "changed\n")
    write(repo, "src/ok.py", "fine = True\n")
    said = Said()
    code = lander(repo, said, terms_files=(terms,), yes=True).land()
    assert code == exits.FAILURE
    err = "\n".join(said.err)
    assert "docs/process, docs/roadmap, secret or .dromon" in err
    assert path.casefold() in err.casefold()
    assert staged(repo) == ""
    assert git(repo, "log", "--oneline").count("\n") == 0


def test_main_is_refused(repo: Path, terms: Path) -> None:
    git(repo, "switch", "-q", "main")
    write(repo, "src/a.py", "changed\n")
    said = Said()
    assert lander(repo, said, terms_files=(terms,)).land() == exits.FAILURE
    assert "only a feature branch lands" in "\n".join(said.err)


def test_a_detached_head_is_refused(repo: Path, terms: Path) -> None:
    git(repo, "switch", "-q", "--detach")
    write(repo, "src/a.py", "changed\n")
    said = Said()
    assert lander(repo, said, terms_files=(terms,)).land() == exits.FAILURE
    assert "HEAD is detached" in "\n".join(said.err)


def test_nothing_to_land_is_refused(repo: Path, terms: Path) -> None:
    said = Said()
    assert lander(repo, said, terms_files=(terms,)).land() == exits.FAILURE
    assert "nothing to land" in "\n".join(said.err)


def test_a_subdirectory_is_refused(repo: Path, terms: Path) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    code = lander(repo, said, worktree=repo / "src", terms_files=(terms,)).land()
    assert code == exits.FAILURE
    assert "is not the top of a git work tree" in "\n".join(said.err)


@pytest.mark.parametrize(
    ("given", "text"),
    [
        ({"message": "  \n"}, "commit message is empty"),
        ({"title": "two\nlines"}, "one non-empty line"),
        ({"body": ""}, "body is empty"),
        (
            {"message": "feat: x\n\nCo-Authored-By: Someone <a@example.invalid>\n"},
            "Co-Authored-By",
        ),
        ({"body": "text\nco-authored-by: Someone\n"}, "Co-Authored-By"),
    ],
)
def test_bad_texts_are_refused_before_anything_runs(
    repo: Path, terms: Path, given: dict[str, object], text: str
) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    runner = Recording()
    code = lander(repo, said, runner, terms_files=(terms,), **given).land()
    assert code == exits.FAILURE
    assert text in "\n".join(said.err)
    assert runner.calls == []


def test_no_disclosure_list_refuses_unless_the_operator_says_so(
    repo: Path, tmp_path: Path
) -> None:
    write(repo, "src/a.py", "changed\n")
    missing = tmp_path / "absent.txt"
    said = Said()
    code = lander(repo, said, terms_files=(missing,), dry_run=True).land()
    assert code == exits.FAILURE
    assert "no disclosure list was found" in "\n".join(said.err)
    said = Said()
    code = lander(
        repo, said, terms_files=(missing,), dry_run=True, without_list=True
    ).land()
    assert code == exits.OK
    assert "not screened" in "\n".join(said.out)


# The disclosure screen.


def test_a_listed_term_is_reported_by_place_never_by_text(
    repo: Path, terms: Path
) -> None:
    write(repo, "src/a.py", f"a = 1\nb = 2\nc = '{TERM.upper()}'\n")
    write(repo, "src/new.py", f"first = 1\nsecond = '{TERM}'\n")
    said = Said()
    message = f"feat: a thing\n\nmentions {TERM}\n"
    code = lander(
        repo, said, terms_files=(terms,), dry_run=True, message=message
    ).land()
    assert code == exits.FAILURE
    err = said.err
    assert "src/a.py:3" in err
    assert "src/new.py:2" in err
    assert "the commit message, line 3" in err
    assert all(TERM not in line.casefold() for line in said.out + said.err)
    assert staged(repo) == ""


def test_an_accepted_match_passes(repo: Path, terms: Path, tmp_path: Path) -> None:
    line = f"second = '{TERM}'"
    write(repo, "src/new.py", f"first = 1\n{line}\n")
    accepted = tmp_path / "accepted.txt"
    accepted.write_text(f"# accepted\nsrc/new.py\t2\t{line}\n")
    said = Said()
    code = lander(
        repo, said, terms_files=(terms,), accepted_file=accepted, dry_run=True
    ).land()
    assert code == exits.OK, said.text


def test_a_term_in_a_path_name_is_counted_not_named(repo: Path, terms: Path) -> None:
    write(repo, f"src/{TERM}.py", "x = 1\n")
    said = Said()
    code = lander(repo, said, terms_files=(terms,), dry_run=True).land()
    assert code == exits.FAILURE
    assert "the names of 1 staged paths" in said.err
    assert all(TERM not in line for line in said.err)


def test_a_term_found_only_after_staging_is_refused_and_left_staged(
    repo: Path, terms: Path
) -> None:
    """The screen after staging reads the index, so a file that gains a term after
    the dry run's screen and before the commit is still caught."""
    write(repo, "src/a.py", "changed\n")
    said = Said()

    def change_then_agree(question: str) -> bool:
        write(repo, "src/a.py", f"changed {TERM}\n")
        said.asked.append(question)
        return True

    target = lander(repo, said, terms_files=(terms,))
    target.ask = change_then_agree
    assert target.land() == exits.FAILURE
    assert "src/a.py:1" in said.err
    assert "The paths stay staged" in said.err[-1]
    assert staged(repo) == "src/a.py"
    assert git(repo, "log", "--oneline").count("\n") == 0


# The whole command.


def test_land_commits_gates_pushes_and_opens_a_pull_request(
    repo: Path, remote: Path, terms: Path
) -> None:
    write(repo, "src/a.py", "a = 1\nb = 20\nc = 3\n")
    write(repo, "src/new.py", "x = 1\n")
    runner = Recording()
    said = Said()
    code = lander(repo, said, runner, terms_files=(terms,), yes=True).land()
    assert code == exits.OK, said.text
    commit = git(repo, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "refs/heads/feat/thing") == commit
    assert git(repo, "log", "-1", "--format=%B") == "feat: a thing\n\nWhy it matters."
    assert git(repo, "status", "--porcelain") == ""
    assert f"dromon land: opened {PR_URL}" in said.out
    gh = [call for call in runner.calls if call[0] == "gh"]
    assert gh == [
        (
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "feat/thing",
            "--title",
            "feat: a thing",
            "--body-file",
            "-",
        )
    ]
    assert runner.gh_input == ["Summary.\n"]
    flat = [word for call in runner.calls for word in call]
    assert not {"--force", "-f", "--force-with-lease", "merge"} & set(flat)
    assert not any(word.startswith("+") for word in flat)
    push = next(call for call in runner.calls if call[:2] == ("git", "push"))
    assert push[-1] == f"{commit}:refs/heads/feat/thing"
    order = [call[0] if call[0] != "git" else call[1] for call in runner.calls]
    gate_at = runner.calls.index(land.GATE)
    assert order.index("commit") < gate_at < order.index("push")


def test_each_outward_step_asks_and_a_no_stops_there(
    repo: Path, remote: Path, terms: Path
) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    code = lander(repo, said, terms_files=(terms,)).land()
    assert code == exits.FAILURE
    assert len(said.asked) == 1 and said.asked[0].startswith("Stage 1 paths")
    assert staged(repo) == ""
    said = Said()
    said.answers = [True, False]
    runner = Recording()
    code = lander(repo, said, runner, terms_files=(terms,)).land()
    assert code == exits.FAILURE
    assert said.asked[1].startswith("Push with: git push --no-verify")
    assert "local only" in "\n".join(said.out)
    assert git(remote, "branch", "--list", "feat/thing") == ""
    assert all(call[0] != "gh" for call in runner.calls)


def test_a_declined_pull_request_leaves_the_branch_pushed(
    repo: Path, remote: Path, terms: Path
) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    said.answers = [True, True, False]
    runner = Recording()
    assert lander(repo, said, runner, terms_files=(terms,)).land() == exits.FAILURE
    assert len(said.asked) == 3
    assert git(remote, "branch", "--list", "feat/thing").strip() == "feat/thing"
    assert all(call[0] != "gh" for call in runner.calls)


def test_a_failing_gate_pushes_nothing(repo: Path, remote: Path, terms: Path) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    runner = Recording(gate_code=1)
    code = lander(repo, said, runner, terms_files=(terms,), yes=True).land()
    assert code == exits.FAILURE
    assert "the gate failed with status 1" in "\n".join(said.err)
    assert all(call[:2] != ("git", "push") for call in runner.calls)
    assert git(remote, "branch", "--list", "feat/thing") == ""


def test_a_failing_pull_request_says_the_branch_is_pushed(
    repo: Path, terms: Path
) -> None:
    write(repo, "src/a.py", "changed\n")
    said = Said()
    runner = Recording(gh_code=1)
    code = lander(repo, said, runner, terms_files=(terms,), yes=True).land()
    assert code == exits.FAILURE
    assert "gh pr create failed with status 1: gh: refused" in "\n".join(said.err)


def test_a_trailer_a_hook_adds_is_caught_before_the_push(
    repo: Path, remote: Path, terms: Path, tmp_path: Path
) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "commit-msg"
    hook.write_text(
        '#!/bin/sh\nprintf "\\nCo-Authored-By: Bot <bot@example.invalid>\\n" >> "$1"\n'
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    git(repo, "config", "core.hooksPath", str(hooks))
    write(repo, "src/a.py", "changed\n")
    said = Said()
    runner = Recording()
    code = lander(repo, said, runner, terms_files=(terms,), yes=True).land()
    assert code == exits.FAILURE
    assert "carries a Co-Authored-By trailer that a hook" in "\n".join(said.err)
    assert land.GATE not in runner.calls
    assert git(remote, "branch", "--list", "feat/thing") == ""


# Merges a line resolved.


@pytest.fixture
def merging(repo: Path) -> Iterator[Path]:
    """``feat/thing`` part way through a merge of ``main`` that conflicts in
    ``src/a.py``, with ``main`` also changing ``docs/process/HANDOFF.md``."""
    write(repo, "src/a.py", "a = 1\nb = 'feature'\nc = 3\n")
    git(repo, "commit", "-q", "-am", "feature")
    git(repo, "switch", "-q", "main")
    write(repo, "src/a.py", "a = 1\nb = 'main'\nc = 3\n")
    write(repo, "docs/process/HANDOFF.md", "one\ntwo\nthree\n")
    git(repo, "commit", "-q", "-am", "main moves on")
    git(repo, "switch", "-q", "feat/thing")
    git(repo, "merge", "main", check=False)
    yield repo


def test_an_unresolved_merge_is_refused(merging: Path, terms: Path) -> None:
    said = Said()
    assert lander(merging, said, terms_files=(terms,)).land() == exits.FAILURE
    assert "unmerged paths" in "\n".join(said.err)
    assert "src/a.py" in "\n".join(said.err)


def test_a_resolved_merge_lands_and_carries_the_base_s_protected_change(
    merging: Path, remote: Path, terms: Path
) -> None:
    write(merging, "src/a.py", "a = 1\nb = 'both'\nc = 3\n")
    git(merging, "add", "src/a.py")
    said = Said()
    code = lander(merging, said, terms_files=(terms,), yes=True).land()
    assert code == exits.OK, said.text
    out = "\n".join(said.out)
    assert "concludes a merge of" in out
    assert "docs/process/HANDOFF.md  carried by the merge" in out
    parents = git(merging, "log", "-1", "--format=%P").split()
    assert len(parents) == 2
    assert git(remote, "rev-parse", "refs/heads/feat/thing") == git(
        merging, "rev-parse", "HEAD"
    )


def test_a_merge_that_changes_a_protected_path_itself_is_refused(
    merging: Path, terms: Path
) -> None:
    write(merging, "src/a.py", "a = 1\nb = 'both'\nc = 3\n")
    write(merging, "docs/process/HANDOFF.md", "the line's own words\n")
    git(merging, "add", "-A")
    said = Said()
    assert lander(merging, said, terms_files=(terms,)).land() == exits.FAILURE
    assert "docs/process/HANDOFF.md" in "\n".join(said.err)


# The command line.


def test_the_command_line_runs_a_dry_run_with_the_project_s_list(
    repo: Path,
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    write(project, land.TERMS_FILE, f"{TERM}\n")
    write(repo, "src/a.py", "changed\n")
    code = cli.main(
        [
            "--project",
            str(project),
            "--runtime-dir",
            str(short_runtime),
            "land",
            str(repo),
            "--message",
            "feat: x",
            "--body",
            "Body.",
            "--dry-run",
        ]
    )
    assert code == exits.OK
    out = capsys.readouterr().out
    assert "titled 'feat: x'" in out
    assert "the disclosure screen found no listed term" in out


def test_the_command_line_reads_texts_from_files_and_refuses_a_missing_one(
    repo: Path,
    tmp_path: Path,
    terms: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = tmp_path / "message.txt"
    message.write_text("fix: from a file\n\nDetail.\n")
    write(repo, "src/a.py", "changed\n")
    common = [
        "--project",
        str(tmp_path),
        "--runtime-dir",
        str(short_runtime),
        "land",
        str(repo),
        "--terms",
        str(terms),
        "--dry-run",
    ]
    code = cli.main(
        [*common, "--message-file", str(message), "--body-file", str(message)]
    )
    assert code == exits.OK
    assert "titled 'fix: from a file'" in capsys.readouterr().out
    code = cli.main(
        [*common, "--message", "x", "--body-file", str(tmp_path / "absent")]
    )
    assert code == exits.USAGE
    assert "cannot be read" in capsys.readouterr().err


# The parts.


def test_status_entries_are_read_and_a_strange_one_refused() -> None:
    found = land.parse_status(" M a b.py\0?? c\0UU d\0")
    assert [(c.code, c.path) for c in found] == [
        (" M", "a b.py"),
        ("??", "c"),
        ("UU", "d"),
    ]
    assert [c.unmerged for c in found] == [False, False, True]
    with pytest.raises(land.LandError):
        land.parse_status("garbage\0")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("b/plain.py", "b/plain.py"),
        ('"b/tab\\there"', "b/tab\there"),
        ('"b/quote\\"d"', 'b/quote"d'),
        ('"b/caf\\303\\251"', "b/café"),
        ('"b/odd\\q"', "b/odd\\q"),
    ],
)
def test_quoted_diff_paths_are_unquoted(given: str, expected: str) -> None:
    assert land.unquote(given) == expected


def test_diff_lines_are_numbered_in_the_new_file() -> None:
    diff = "\n".join(
        [
            "diff --git a/x b/x",
            "index 1..2 100644",
            "--- a/x",
            "+++ b/x",
            "@@ -2,0 +3,3 @@",
            "+fine",
            f"+has {TERM}",
            f"+++ {TERM} as an added line that looks like a header",
            "@@ -9 +12 @@",
            f"-{TERM} removed",
            f"+{TERM} again",
            "\\ No newline at end of file",
            "diff --git a/gone b/gone",
            "--- a/gone",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            f"-{TERM}",
        ]
    )
    assert land.diff_matches((TERM,), frozenset(), diff) == ["x:4", "x:5", "x:12"]
    accepted = frozenset({("x", 4, f"has {TERM}")})
    assert land.diff_matches((TERM,), accepted, diff) == ["x:5", "x:12"]


@pytest.mark.parametrize(
    "line",
    ["no tabs at all", "path\tzero\ttext", "path\t0\ttext", "\t3\ttext", "p\t3\t"],
)
def test_a_malformed_accepted_entry_names_its_line(tmp_path: Path, line: str) -> None:
    path = tmp_path / "accepted.txt"
    path.write_text(f"# header\n{line}\n")
    with pytest.raises(land.LandError, match=r"accepted\.txt:2"):
        land.load_accepted(path)


def test_the_protected_prefixes_ignore_case_and_need_a_directory() -> None:
    assert land.is_protected("Secret/x")
    assert land.is_protected(".DROMON/run/lock")
    assert not land.is_protected("secretive.py")
    assert not land.is_protected("docs/processes.md")


def test_the_real_runner_reports_a_missing_command_and_a_timeout(
    tmp_path: Path,
) -> None:
    missing = land.run_command(["/nonexistent/command"], tmp_path)
    assert missing.code == 127
    slow = land.run_command(["/bin/sleep", "5"], tmp_path, timeout=0.05)
    assert slow.code == 124
