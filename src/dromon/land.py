"""``dromon land``: take a line's finished worktree to a pull request.

**Asked for by the operator on 2026-10-31.** One operator command shows what will
happen, stages a line's work, including a merge the line resolved, screens it, commits
it with the operator's message, runs the full gate, pushes the branch and opens a pull
request with the operator's body. ``docs/protocol/CLIENT.md`` gives the command.

The steps, in order, each stopping the command when it fails.

1. Inspect. The directory must be the top of a git work tree on a branch other than
   the base, with no rebase, cherry-pick or revert in progress and no unmerged path.
   Every changed, deleted and untracked path is listed, numbered, as the paths to
   stage.
2. Refuse protected paths. A path under ``docs/process``, ``docs/roadmap``,
   ``secret`` or ``.dromon`` refuses the whole command, compared without regard to
   case, since macOS file systems ignore it. During a merge of a commit in the
   history of the remote base, a tracked path whose content is the incoming
   commit's own is not refused, because it is the base's change, which the merge
   carries and the line did not write. A merge of any other commit carries nothing.
3. Screen. Every commit the push carries, from where the branch left the remote
   base, by its message and its own diff, the net diff since then, the branch name,
   the diff of what would be committed, and the commit message, pull request title
   and body, are checked against the disclosure list, which must exist and hold a
   term unless ``--without-disclosure-list`` is given. A match is reported by file
   and line, or by text and line, never by its content or the term. The gate's
   accepted matches, by path, line and exact text, are honoured for the diffs.
4. With ``--dry-run`` the command stops here, having changed nothing. Otherwise it
   asks, then stages exactly the listed paths and checks that the index holds those
   and no others, then screens the staged diff again, since that is what is
   committed.
5. Commit with the operator's message, which may not carry a ``Co-Authored-By``
   trailer, and check that the commit made carries none and that its stored message
   holds no listed term, since a hook or template could add either.
6. Run the full gate, ``scripts/check.sh`` with no arguments, in the worktree.
   The gate and the hooks the commit runs are the branch's own code, run as the
   operator outside any sandbox, and the gate reaches all of it, the tests,
   ``conftest.py``, ``pyproject.toml`` and ``uv.lock`` included. The plan says so,
   and when the branch changes a path under ``scripts/``, which holds the gate and
   the hooks, the command asks before staging even with ``--yes``.
7. Ask, then push the branch to the remote, never forcing, with ``--no-verify``,
   because the pre-push hook would run the same gate on the same commit again. The
   commit is checked to be the one gated, and the refspec names that commit.
8. Ask, then open the pull request with ``gh pr create``. Nothing here merges.

``--yes`` answers every question. A question that cannot be asked, as with no
terminal, is answered no.

**Assumptions and limits.**

- The operator runs this outside any agent sandbox, since it writes the git directory
  and reaches the network. A line cannot run it, since lines may not commit.
- The disclosure screen matches fixed strings after Unicode case folding, as
  ``dromon.disclosure`` does. ``scripts/check.sh`` matches with ``git grep -i``,
  which folds ASCII only, so the two can differ for other scripts, in the direction
  of this screen finding more. A staged path whose name holds a term is reported
  without its name, by count. Removed lines are not screened, since a line a commit
  removes was added by an earlier commit, which is screened if the push carries it.
- The screen of a dry run reads the working tree, and untracked files through
  ``git diff --no-index``. The screen after staging reads the index, which is what
  the commit holds, so a file changed between the two is screened again.
- During a merge the diff is taken against ``HEAD``, so it holds the base's changes
  too, which are already public. They are screened all the same.
- A failure after the commit leaves the commit in place, local only, and says so, so
  the operator can amend it or reset it. A failure after the push leaves the branch
  pushed and no pull request.
- Every command is run with an argument vector, never a shell. Paths are passed to
  git with ``--literal-pathspecs`` so a name with glob characters names only itself.

Porting note. The command is a fixed sequence of external processes, each judged by
its exit status and its output, so it maps onto a state machine over a process runner.
The runner is injected, which is how the tests avoid a real remote.
"""

import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from dromon import exits
from dromon.disclosure import DisclosureError, load_terms

PROTECTED_PREFIXES: Final = ("docs/process/", "docs/roadmap/", "secret/", ".dromon/")
"""Paths no line's work may change, which the operator's own session keeps. Compared
after case folding."""

DEFAULT_BASE: Final = "main"
DEFAULT_REMOTE: Final = "origin"

LOCK_FILE: Final = "uv.lock"
"""The lock file whose change makes every other worktree's environment stale."""

GATE: Final = ("./scripts/check.sh",)
"""The full gate, as continuous integration and the pre-push hook run it."""

TERMS_FILE: Final = "secret/disclosure-terms.txt"
ACCEPTED_FILE: Final = "secret/disclosure-accepted.txt"
"""The gate's list and its accepted matches, under the project root, which a worktree
does not hold, since ``secret/`` is ignored."""

GIT_SECONDS: Final = 120.0
GATE_SECONDS: Final = 3600.0
NETWORK_SECONDS: Final = 600.0

MAX_UNTRACKED_FILES: Final = 10_000
"""The most untracked files a dry run screens one by one, so a tree full of build
output refuses rather than runs for hours."""

TRAILER: Final = re.compile(
    r"^[ \t]*co-authored-by[ \t]*:", re.IGNORECASE | re.MULTILINE
)
"""A ``Co-Authored-By`` trailer, which no commit in any repository may carry, by the
operator's instruction of 2026-10-25."""

_HUNK: Final = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_DIFF: Final = (
    "-c",
    "core.quotePath=false",
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--text",
    "--unified=0",
    "--no-renames",
)
"""How every screened diff is read, as added lines with no context."""

_SHOW: Final = ("-c", "core.quotePath=false", "show", "--format=", *_DIFF[3:])
"""How one commit's own diff is read, in the form ``_DIFF`` gives."""

MAX_COMMITS: Final = 1000
"""The most commits land screens on one branch, so the screen is bounded."""


class LandError(Exception):
    """The command cannot go on, with the reason."""


@dataclass(frozen=True, slots=True)
class Completed:
    """What one external command did. ``stdout`` and ``stderr`` are empty when the
    command's output went straight to the terminal."""

    code: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    """Runs one command in ``cwd`` and says what it did. With ``stream``, its output
    goes to the terminal rather than being captured. Never raises for a nonzero
    status, which ``code`` gives, and gives code 127 for a command that cannot be
    run."""

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | None = None,
        *,
        stream: bool = False,
        timeout: float = GIT_SECONDS,
    ) -> Completed: ...


def run_command(
    argv: Sequence[str],
    cwd: Path,
    stdin: str | None = None,
    *,
    stream: bool = False,
    timeout: float = GIT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> Completed:
    """The real runner. A timeout or a command that cannot start is reported as a
    failed command, so every step judges one ``Completed``. ``env``, when given,
    is the command's whole environment, which ``dromon sync-worktrees`` sets."""
    try:
        done = subprocess.run(  # noqa: S603
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            input=stdin,
            capture_output=not stream,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"{argv[0]} did not finish within {timeout:g}s")
    except OSError as error:
        return Completed(127, "", f"{argv[0]} cannot be run: {error}")
    return Completed(done.returncode, done.stdout or "", done.stderr or "")


def ask_terminal(question: str) -> bool:
    """Ask the operator on the terminal. Anything but ``y`` or ``yes`` is no, and so
    is a question that cannot be read, as with no terminal."""
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().casefold() in {"y", "yes"}


def to_stderr(text: str) -> None:
    print(text, file=sys.stderr)


@dataclass(frozen=True, slots=True)
class Change:
    """One path the plan stages, with its two-letter ``git status`` code."""

    path: str
    code: str

    @property
    def untracked(self) -> bool:
        return self.code == "??"

    @property
    def unmerged(self) -> bool:
        return "U" in self.code or self.code in {"AA", "DD"}


@dataclass(frozen=True, slots=True)
class Plan:
    """What ``land`` found in the worktree, and why it would refuse, if it would."""

    worktree: Path
    branch: str
    head: str
    merging: str | None
    changes: tuple[Change, ...]
    refusals: tuple[str, ...]
    carried: frozenset[str] = frozenset()
    """Protected paths a merge carries unchanged from the incoming commit."""
    fork: str | None = None
    """Where the branch left the remote base, from which every commit the push
    carries is screened, or ``None`` when the remote base is not known."""
    gate_changed: tuple[str, ...] = ()
    """Paths under ``scripts/`` the branch changes against ``fork``, which hold the
    gate and the hooks that land runs as the operator."""
    lock_changed: bool = False
    """Whether the branch changes ``uv.lock``, so that every other worktree's
    environment goes stale once it merges. Round 14, item 3."""


@dataclass(frozen=True, slots=True)
class Request:
    """What the operator asked for."""

    worktree: Path
    message: str
    title: str
    body: str
    base: str = DEFAULT_BASE
    remote: str = DEFAULT_REMOTE
    dry_run: bool = False
    yes: bool = False
    terms_files: tuple[Path, ...] = ()
    accepted_file: Path | None = None
    without_list: bool = False
    gate: tuple[str, ...] = GATE


@dataclass(slots=True)
class Lander:
    """One run of ``dromon land``, with its runner, questions and output injected."""

    request: Request
    run: Runner = run_command
    ask: Callable[[str], bool] = ask_terminal
    say: Callable[[str], None] = print
    warn: Callable[[str], None] = to_stderr
    terms: tuple[str, ...] = field(default=(), init=False)
    accepted: frozenset[tuple[str, int, str]] = field(
        default=frozenset[tuple[str, int, str]](), init=False
    )

    # Running git.

    def git(self, *args: str, stdin: str | None = None) -> str:
        """Run git in the worktree and give its output, or raise ``LandError``."""
        done = self.run(["git", *args], self.request.worktree, stdin)
        if done.code != 0:
            reason = (done.stderr or done.stdout).strip().splitlines()
            name = next((a for a in args if not a.startswith("-") and "=" not in a), "")
            raise LandError(
                f"git {name} failed with status {done.code}:"
                f" {reason[-1] if reason else 'no output'}"
            )
        return done.stdout

    def _ref(self, name: str) -> str | None:
        done = self.run(
            ["git", "rev-parse", "-q", "--verify", name], self.request.worktree
        )
        found = done.stdout.strip()
        return found if done.code == 0 and found else None

    def _carried(self, merging: str, path: str) -> bool:
        """Whether the working tree holds ``path`` as the incoming commit has it."""
        code = self.run(
            ["git", "--literal-pathspecs", "diff", "--quiet", merging, "--", path],
            self.request.worktree,
        ).code
        if code not in {0, 1}:
            raise LandError(f"git diff could not compare {path} with {merging}")
        return code == 0

    # The steps.

    def inspect(self) -> Plan:
        """Read the worktree and decide what would be staged and what refuses."""
        worktree = self.request.worktree
        top = self.git("rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() != worktree.resolve():
            raise LandError(f"{worktree} is not the top of a git work tree, {top} is")
        refusals: list[str] = []
        named = self.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], worktree
        )
        branch = named.stdout.strip() if named.code == 0 else ""
        bases = {self.request.base.casefold(), "main", "master"}
        if not branch:
            refusals.append("HEAD is detached, so there is no branch to push")
        elif branch.casefold() in bases:
            refusals.append(
                f"the worktree is on {branch}, and only a feature branch lands"
            )
        head = self.git("rev-parse", "HEAD").strip()
        merging = self._ref("MERGE_HEAD")
        for name, what in (
            ("REBASE_HEAD", "a rebase"),
            ("CHERRY_PICK_HEAD", "a cherry-pick"),
            ("REVERT_HEAD", "a revert"),
        ):
            if self._ref(name) is not None:
                refusals.append(
                    f"{what} is in progress, and land concludes a merge only"
                )
        changes = parse_status(
            self.git(
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            )
        )
        unmerged = [c.path for c in changes if c.unmerged]
        if unmerged:
            refusals.append(
                "the merge leaves unmerged paths, which the line must resolve: "
                + ", ".join(unmerged)
            )
        remote_base = f"refs/remotes/{self.request.remote}/{self.request.base}"
        fork = self._fork(remote_base)
        if fork is None:
            refusals.append(
                f"the remote base {remote_base} is not known or shares no history"
                " with HEAD, so land cannot tell which commits the push carries."
                " Fetch it first"
            )
        # Audit's finding L2. Only a merge of the published base may carry a
        # protected path, so a merge of any other branch cannot.
        published = merging is not None and self._ancestor(merging, remote_base)
        carried: set[str] = set()
        protected: list[str] = []
        for change in changes:
            if not is_protected(change.path) or change.unmerged:
                continue
            if (
                merging is not None
                and published
                and not change.untracked
                and self._carried(merging, change.path)
            ):
                carried.add(change.path)
                continue
            protected.append(change.path)
        if protected:
            refusals.append(
                "paths under docs/process, docs/roadmap, secret or .dromon are"
                " changed, which no line's work may change: " + ", ".join(protected)
            )
        if not changes and merging is None:
            refusals.append("nothing is changed, so there is nothing to land")
        return Plan(
            worktree,
            branch,
            head,
            merging,
            tuple(changes),
            tuple(refusals),
            frozenset(carried),
            fork,
            () if fork is None else self._gate_changes(fork, changes),
            fork is not None and LOCK_FILE in self._changed(fork, changes),
        )

    def _fork(self, remote_base: str) -> str | None:
        done = self.run(
            ["git", "merge-base", "HEAD", remote_base], self.request.worktree
        )
        found = done.stdout.strip()
        return found if done.code == 0 and found else None

    def _ancestor(self, commit: str, of: str) -> bool:
        """Whether ``commit`` is in the history of ``of``. Any failure, such as an
        unknown ref, is no."""
        done = self.run(
            ["git", "merge-base", "--is-ancestor", commit, of], self.request.worktree
        )
        return done.code == 0

    def _changed(self, fork: str, changes: Sequence[Change]) -> frozenset[str]:
        """The paths that differ from ``fork`` in the commits or the working tree,
        or that are new."""
        listed = self.git("diff", "--name-only", "-z", "--no-renames", fork)
        names = {name for name in listed.split("\0") if name}
        names |= {change.path for change in changes}
        return frozenset(names)

    def _gate_changes(self, fork: str, changes: Sequence[Change]) -> tuple[str, ...]:
        """The paths under ``scripts/`` that differ from ``fork``. Audit's finding
        L5."""
        names = self._changed(fork, changes)
        return tuple(sorted(n for n in names if n.casefold().startswith("scripts/")))

    def load_lists(self) -> None:
        """Read the disclosure lists. Unless the operator gave
        ``--without-disclosure-list``, every list named must be a file and together
        they must hold a term, so a mistyped path or an emptied list refuses rather
        than screening nothing. **Decided by the operator on 2026-10-31, after
        audit's finding L1.** Errors name files only."""
        lists = self.request.terms_files
        present = [path for path in lists if path.is_file()]
        missing = [str(path) for path in lists if not path.is_file()]
        unscreened = (
            ", so nothing could be screened. Give --terms naming a list with terms,"
            " or --without-disclosure-list to land unscreened"
        )
        if not self.request.without_list and (missing or not lists):
            named = ", ".join(missing) or "no path"
            raise LandError(f"no disclosure list was found at {named}{unscreened}")
        found: set[str] = set()
        try:
            for path in present:
                found.update(load_terms(path))
        except DisclosureError as error:
            raise LandError(str(error)) from error
        if not found and not self.request.without_list:
            named = ", ".join(str(path) for path in present)
            raise LandError(f"the disclosure list {named} holds no terms{unscreened}")
        self.terms = tuple(sorted(found))
        accepted = self.request.accepted_file
        if accepted is not None and accepted.is_file():
            self.accepted = load_accepted(accepted)

    def screen_worktree(self, plan: Plan) -> list[str]:
        """The screen of a dry run, over the working tree against ``HEAD``."""
        untracked = [c.path for c in plan.changes if c.untracked]
        if len(untracked) > MAX_UNTRACKED_FILES:
            raise LandError(
                f"{len(untracked)} untracked files, more than the"
                f" {MAX_UNTRACKED_FILES} a dry run screens"
            )
        parts = [self.git(*_DIFF, "HEAD")]
        for path in untracked:
            done = self.run(
                ["git", *_DIFF, "--no-index", "--", "/dev/null", path],
                self.request.worktree,
            )
            if done.code not in {0, 1}:
                raise LandError(f"git diff could not read the untracked file {path}")
            parts.append(done.stdout)
        return self.screen("\n".join(parts))

    def screen_history(self, plan: Plan) -> list[str]:
        """Where the commits the push carries hold a listed term, each commit from
        the fork to ``HEAD`` by its message and its own diff, the net diff from the
        fork, which holds what a merge resolved, and the branch name, which the push
        publishes. **Decided by the operator on 2026-10-31, after audit's finding
        L3.** A merge commit's own diff is not read, since what it brings from the
        base is published already and its resolution is in the net diff. Line
        numbers in a commit's diff are that commit's, which the accepted matches,
        written for the current tree, may not name."""
        if not self.terms or plan.fork is None:
            return []
        found: list[str] = []
        if _holds(self.terms, plan.branch):
            found.append("the branch name")
        listed = self.git("rev-list", "--reverse", "--parents", f"{plan.fork}..HEAD")
        commits = [line.split() for line in listed.splitlines() if line.strip()]
        if len(commits) > MAX_COMMITS:
            raise LandError(
                f"the branch carries {len(commits)} commits, more than the"
                f" {MAX_COMMITS} land screens"
            )
        for ids in commits:
            short = ids[0][:12]
            message = self.git("log", "-1", "--format=%B", ids[0])
            found += [
                f"commit {short}, {where}"
                for where in text_matches(self.terms, {"message": message})
            ]
            if len(ids) > 2:
                continue
            diff = self.git(*_SHOW, ids[0])
            found += [
                f"commit {short}, {where}"
                for where in diff_matches(self.terms, self.accepted, diff)
            ]
        net = self.git(*_DIFF, plan.fork, "HEAD")
        found += [
            f"the branch since {plan.fork[:12]}, {where}"
            for where in diff_matches(self.terms, self.accepted, net)
        ]
        return found

    def screen(self, diff: str) -> list[str]:
        """Where the diff and the operator's texts hold a listed term."""
        if not self.terms:
            return []
        found = diff_matches(self.terms, self.accepted, diff)
        found += text_matches(
            self.terms,
            {
                "commit message": self.request.message,
                "pull request title": self.request.title,
                "pull request body": self.request.body,
            },
        )
        return found

    def stage(self, plan: Plan) -> None:
        """Stage exactly the plan's paths, then check that the index holds those."""
        paths = [c.path for c in plan.changes]
        if paths:
            self.git(
                "--literal-pathspecs",
                "add",
                "-A",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                stdin="\0".join(paths) + "\0",
            )
        listed = self.git(
            "diff", "--cached", "--name-only", "-z", "--no-renames", "HEAD"
        )
        staged = {name for name in listed.split("\0") if name}
        planned = set(paths)
        if staged != planned:
            extra = ", ".join(sorted(staged - planned)) or "none"
            missing = ", ".join(sorted(planned - staged)) or "none"
            raise LandError(
                f"the index does not hold exactly the planned paths. Extra: {extra}."
                f" Missing: {missing}. Nothing was committed, and git reset"
                " unstages them"
            )

    def commit(self) -> str:
        """Commit the index with the operator's message, and give the commit."""
        self.git(
            "commit", "--cleanup=whitespace", "--file=-", stdin=self.request.message
        )
        made = self.git("log", "-1", "--format=%B")
        commit = self.git("rev-parse", "HEAD").strip()
        if TRAILER.search(made):
            raise LandError(
                f"commit {commit} carries a Co-Authored-By trailer that a hook or"
                " template added. It was not pushed. Amend it or reset it"
            )
        # A hook may also have changed the message the screen read.
        stored = text_matches(self.terms, {"stored commit message": made})
        if stored:
            raise LandError(
                f"{', '.join(stored)} of commit {commit} holds a term from the"
                " disclosure list, which a hook or template added. It was not"
                " pushed. Amend it or reset it"
            )
        return commit

    # The whole command.

    def land(self) -> int:
        """Run the command and give its exit status."""
        request = self.request
        try:
            check_texts(request)
            plan = self.inspect()
            self.say(describe(plan, request))
            if plan.refusals:
                for refusal in plan.refusals:
                    self.warn(f"dromon land: refused, {refusal}")
                return exits.FAILURE
            self.load_lists()
            found = self.screen_history(plan) + self.screen_worktree(plan)
            if found:
                self._report(found, staged=False)
                return exits.FAILURE
            self.say(self._screened())
            if request.dry_run:
                self.say("dromon land: dry run, so nothing was staged or committed")
                return exits.OK
            question = (
                f"Stage {len(plan.changes)} paths and commit them on {plan.branch}?"
            )
            if not self._agree(question):
                self.say("dromon land: declined, so nothing was staged")
                return exits.FAILURE
            # Audit's finding L5. The commit runs the branch's hooks and the gate is
            # the branch's own script, so a change to either is asked about even
            # with --yes.
            if plan.gate_changed and not self.ask(
                f"The branch changes {', '.join(plan.gate_changed)}, which land runs"
                " as you outside any sandbox. Run them?"
            ):
                self.say("dromon land: declined, so nothing was staged")
                return exits.FAILURE
            self.stage(plan)
            found = self.screen(self.git(*_DIFF, "--cached", "HEAD"))
            if found:
                self._report(found, staged=True)
                return exits.FAILURE
            commit = self.commit()
            self.say(f"dromon land: committed {commit} on {plan.branch}")
            return self._after_commit(plan, commit)
        except LandError as error:
            self.warn(f"dromon land: {error}")
            return exits.FAILURE

    def _after_commit(self, plan: Plan, commit: str) -> int:
        """Gate, push and open the pull request, asking before each outward step."""
        request = self.request
        local = f" Commit {commit} stays on {plan.branch}, local only."
        self.say(f"dromon land: running the gate, {' '.join(request.gate)}")
        gate = self.run(
            request.gate, request.worktree, stream=True, timeout=GATE_SECONDS
        )
        if gate.code != 0:
            self.warn(f"dromon land: the gate failed with status {gate.code}.{local}")
            return exits.FAILURE
        if self.git("rev-parse", "HEAD").strip() != commit:
            raise LandError(
                f"HEAD moved during the gate, so nothing was pushed.{local}"
            )
        push = [
            "git",
            "push",
            "--no-verify",
            "--set-upstream",
            request.remote,
            f"{commit}:refs/heads/{plan.branch}",
        ]
        if not self._agree(f"Push with: {' '.join(push)}?"):
            self.say(f"dromon land: declined, so nothing was pushed.{local}")
            return exits.FAILURE
        pushed = self.run(push, request.worktree, stream=True, timeout=NETWORK_SECONDS)
        if pushed.code != 0:
            self.warn(f"dromon land: the push failed with status {pushed.code}.{local}")
            return exits.FAILURE
        create = [
            "gh",
            "pr",
            "create",
            "--base",
            request.base,
            "--head",
            plan.branch,
            "--title",
            request.title,
            "--body-file",
            "-",
        ]
        question = (
            f"Open a pull request from {plan.branch} into {request.base},"
            f" titled {request.title!r}?"
        )
        if not self._agree(question):
            self.say("dromon land: declined, so the branch is pushed with no request")
            return exits.FAILURE
        opened = self.run(
            create, request.worktree, request.body, timeout=NETWORK_SECONDS
        )
        if opened.code != 0:
            reason = (opened.stderr or opened.stdout).strip().splitlines()
            self.warn(
                f"dromon land: gh pr create failed with status {opened.code}:"
                f" {reason[-1] if reason else 'no output'}. The branch is pushed"
            )
            return exits.FAILURE
        self.say(f"dromon land: opened {opened.stdout.strip()}")
        return exits.OK

    def _agree(self, question: str) -> bool:
        return self.request.yes or self.ask(question)

    def _screened(self) -> str:
        if not self.terms:
            return (
                "dromon land: not screened, since --without-disclosure-list was given"
                " and no list with terms was found"
            )
        return "dromon land: the disclosure screen found no listed term"

    def _report(self, found: Sequence[str], *, staged: bool) -> None:
        for where in found:
            self.warn(where)
        after = (
            " The paths stay staged, and git reset unstages them."
            if staged
            else " Nothing was staged."
        )
        self.warn(
            f"dromon land: refused, {len(found)} places hold a term from the"
            f" disclosure list, listed above by place only.{after}"
        )


def check_texts(request: Request) -> None:
    """Refuse an empty message, title or body, or texts carrying a trailer."""
    if not request.message.strip():
        raise LandError("the commit message is empty")
    if not request.title.strip() or "\n" in request.title:
        raise LandError("the pull request title must be one non-empty line")
    if not request.body.strip():
        raise LandError("the pull request body is empty")
    for name, text in (
        ("commit message", request.message),
        ("pull request body", request.body),
    ):
        if TRAILER.search(text):
            raise LandError(
                f"the {name} carries a Co-Authored-By trailer, which no commit or"
                " pull request may carry"
            )


def is_protected(path: str) -> bool:
    """Whether ``path`` lies under a protected directory, without regard to case."""
    folded = path.casefold()
    return any(folded.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def parse_status(output: str) -> list[Change]:
    """The entries of ``git status --porcelain=v1 -z --no-renames``. Raises
    ``LandError`` for an entry it cannot read."""
    changes: list[Change] = []
    for entry in output.split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise LandError("git status gave an entry this command cannot read")
        changes.append(Change(entry[3:], entry[:2]))
    return changes


def describe(plan: Plan, request: Request) -> str:
    """The plan, as the command shows it before it does anything."""
    lines = [
        f"dromon land: worktree {plan.worktree}",
        f"  branch {plan.branch or '(detached)'} at {plan.head}",
    ]
    if plan.merging is not None:
        lines.append(f"  concludes a merge of {plan.merging}")
    lines.append(f"  {len(plan.changes)} paths to stage")
    for number, change in enumerate(plan.changes, start=1):
        note = "  carried by the merge" if change.path in plan.carried else ""
        lines.append(f"  {number:>4} {change.code} {change.path}{note}")
    message = request.message.strip()
    first = message.splitlines()[0] if message else ""
    if plan.fork is not None:
        lines.append(f"  the push carries the commits since {plan.fork}")
    lines.append(
        "  land runs the branch's whole gate and its git hooks as you, outside any"
        " sandbox, which is all of the branch's code they reach: scripts/check.sh,"
        " the tests, conftest.py, pyproject.toml and uv.lock"
    )
    if plan.lock_changed:
        lines.append(
            "  warning: the branch changes uv.lock, so once it merges every role's"
            " worktree needs dromon sync-worktrees, since lines cannot sync without"
            " the network"
        )
    if plan.gate_changed:
        lines.append(
            f"  the branch changes {len(plan.gate_changed)} paths under scripts/,"
            " so land asks before running them, even with --yes:"
        )
        lines += [f"       {path}" for path in plan.gate_changed]
    lines += [
        f"  commit message {first!r}",
        f"  then the gate {' '.join(request.gate)}",
        f"  then push to {request.remote}, never forced",
        f"  then a pull request into {request.base}, titled {request.title!r}",
    ]
    return "\n".join(lines)


def load_accepted(path: Path) -> frozenset[tuple[str, int, str]]:
    """The gate's accepted matches, each a path, line number and exact line text
    separated by tabs, as ``scripts/check.sh`` reads them. Errors name the file and
    line only."""
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as error:
        raise LandError(f"the accepted matches {path} cannot be read") from error
    found: set[tuple[str, int, str]] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0] or not parts[2]:
            raise LandError(f"{path}:{number} is not a path, line and text by tabs")
        if not re.fullmatch(r"[1-9][0-9]*", parts[1]):
            raise LandError(f"{path}:{number} has no positive line number")
        found.add((parts[0], int(parts[1]), parts[2]))
    return frozenset(found)


_ESCAPES: Final = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


def unquote(name: str) -> str:
    """A path as git wrote it in a diff header, with C-style quoting undone. Git
    quotes a name with a control character, a double quote or a backslash, and,
    with ``core.quotePath`` on, any byte above 127, as octal."""
    if not (len(name) >= 2 and name.startswith('"') and name.endswith('"')):
        return name
    body = name[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        octal = body[index + 1 : index + 4]
        if char == "\\" and following in _ESCAPES:
            out.append(_ESCAPES[following])
            index += 2
        elif char == "\\" and re.fullmatch(r"[0-7]{3}", octal):
            out.append(int(octal, 8) & 0xFF)
            index += 4
        else:
            out += char.encode("utf-8", "surrogateescape")
            index += 1
    return out.decode("utf-8", "surrogateescape")


def diff_matches(
    terms: Iterable[str],
    accepted: frozenset[tuple[str, int, str]],
    diff: str,
) -> list[str]:
    """Where the added lines of a unified diff with no context hold a term, as
    ``path:line`` in the new file, and how many added paths hold one in their name.
    Header lines are read only between ``diff --git`` and the first hunk, since an
    added line ``++ x`` appears in the diff as ``+++ x``."""
    listed = tuple(terms)
    found: list[str] = []
    named: set[str] = set()
    path: str | None = None
    header = False
    line = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            header, path = True, None
            continue
        hunk = _HUNK.match(raw)
        if header and hunk is None:
            if raw.startswith("+++ "):
                target = unquote(raw[4:])
                path = None if target == "/dev/null" else target.removeprefix("b/")
                if path is not None and _holds(listed, path):
                    named.add(path)
            continue
        if hunk is not None:
            header, line = False, int(hunk.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            text = raw[1:]
            if _holds(listed, text) and (path, line, text) not in accepted:
                found.append(f"{path}:{line}")
            line += 1
        elif raw.startswith(" "):
            line += 1
    if named:
        found.append(f"the names of {len(named)} staged paths")
    return found


def text_matches(terms: Iterable[str], texts: Mapping[str, str]) -> list[str]:
    """Where each named text holds a term, as its name and line."""
    listed = tuple(terms)
    found: list[str] = []
    for name, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if _holds(listed, line):
                found.append(f"the {name}, line {number}")
    return found


def _holds(terms: Sequence[str], text: str) -> bool:
    folded = text.casefold()
    return any(term in folded for term in terms)
