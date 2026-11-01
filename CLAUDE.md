# CLAUDE.md

Guidance for Claude Code and, through `AGENTS.md`, for other agents working in this
repository. It is loaded into every session and re-read after compaction, so it holds
only what applies broadly. History lives in git and in `CHANGELOG.md`.

## Project Overview

pydromon is the Python prototype of Dromon, a multi-agent harness that runs as a daemon.
The daemon holds several agent session lines with non-trivial per-project configuration.
Curses or graphical clients connect to it. The session lines can message and wake each
other, and the daemon tracks questions for the operator as a first-class feature. The
distribution is `pydromon`, the import package is `dromon`, and the commands are
`dromond` and `dromon` with subcommands.

**Status.** Seeded on 2026-10-25 by the operator's admin session, which only cut the
repository. The data model, event log, operator relays and the daemon skeleton with
`start`, `status` and `stop`, and the Claude Code engine adapter with its Keleusma driver
for Claude Opus 5.5 and Haiku 5.5, layer 1 messaging with ticks, per-line tools and operator questions are implemented. The other subcommands are placeholders. A session line carries development forward. The first milestone is [`docs/roadmap/V0_1_0.md`](docs/roadmap/V0_1_0.md)
and the design is [`docs/architecture/DESIGN.md`](docs/architecture/DESIGN.md). The
normative protocol, drafted here and ported later, is in [`docs/protocol/`](docs/protocol/README.md).

**The port plan.** This Python prototype will be retired after a Rust port, which will in
turn be retired in favour of a Keleusma implementation. Write the prototype so that its
protocol and data model can be carried across unchanged. Behaviour that only Python makes
easy is a porting hazard and should be noted where it is introduced.

**Relationship to Keleusma and pykeleusma.** Keleusma is the target language of the final
implementation. The Python bindings live in the separate `pykeleusma` repository, which
is published on PyPI. Dromon pins its release `0.2.2.0` exactly, and runs its drivers
through it. Do not edit either repository.

## Development Process

**HARD CONSTRAINT, before any of the below.** This project belongs to the Keleusma family.
Before writing anything tracked, read `~/projects/rust/keleusma/secret/notes/APPENDIX_B.md`.
It holds operator-stated context held outside the repository deliberately. **Do not copy
any of it into tracked files, commit messages, or public documentation; reference it
only as "Appendix B".** The failure mode is disclosure, which cannot be undone. The
seeding session could not read it, as recorded in `docs/process/HANDOFF.md`, so the
first task of the receiving line includes checking the seed against it.

**Session startup protocol.**
1. Read [`docs/process/HANDOFF.md`](docs/process/HANDOFF.md) and run its validity checks.
   Report the handoff as valid, or as stale, on their outcome.
2. Check this line's mailbox if `HANDOFF.md` names one.
3. Read [`docs/process/REVERSE_PROMPT.md`](docs/process/REVERSE_PROMPT.md) for the last report.
4. Wait for a human prompt before proceeding.

**After completing each task.**
1. Mark the task in the roadmap table.
2. Overwrite `docs/process/HANDOFF.md` and `docs/process/REVERSE_PROMPT.md`. Both are state
   files. A rewrite that drops an open item names it in the commit message as completed,
   moved or abandoned.
3. Commit once per prompt with a conventional commit.
4. If blocked or uncertain, say so in `REVERSE_PROMPT.md` and **stop**.

`docs/process/PROMPT.md` is read-only for the agent, and is included in the commit if the
operator has modified it.

## Git Workflow

`main` is always green. Work happens on short-lived feature branches named
`<scope>/<short-description>`, merged into `main` by a no-fast-forward merge through a
pull request once checks pass. A feature branch may be red between commits. Only its tip
at merge must be green. Use scoped conventional commits such as `feat:`, `fix:`, `docs:`,
`test:` and `chore:`. Commit messages carry no `Co-Authored-By` trailer, at the
operator's instruction of 2026-10-25, which applies to every repository.

**Changelog fragments.** A branch does not edit `CHANGELOG.md`, because every branch
adding under the same headings made almost every merge conflict. It adds one file,
`changes/BRANCH.md`, with BRANCH the branch name with each `/` written `--`, a first
line `Type: Added`, `Type: Changed` or `Type: Fixed`, a blank line, and its bullets.
`scripts/check.sh` validates the fragments and refuses a branch that edits the
Unreleased section and adds or deletes no fragment. A release runs
`python3 -I scripts/assemble_changelog.py release VERSION DATE`, in the operator's
session and in one commit with the deleted fragments, after `preview` shows the
entries. [`changes/README.md`](changes/README.md) is the convention in full.

The remote is `https://github.com/sgeos/pydromon`, a public repository. Its git
configuration uses the GitHub command-line tool as the credential helper, because SSH
keys are unreachable from inside agent sandboxes.

**One writer per checkout.** When more than one line works on the repository at once,
each works in its own worktree on its own branch, created with `scripts/worktree.sh`.
Two lines in one checkout share an index and a dirty tree, so one line's `git add`,
stash or reset can take in or discard the other's work.
Claude Code's sandbox denies the default location beside the repository, so inside it
set `WORKTREE_BASE="$PWD/tmp/worktrees"`, which was verified on 2026-10-25. Removing a
branch there warns that `.git/config` could not be locked, which is harmless.

**Irreversible or outward-facing actions need the operator's confirmation in session.**
That includes pushing to the remote, changing repository visibility or settings,
publishing a release or a package such as to PyPI, and force-pushing.

## Common Commands

```sh
./scripts/check.sh                             # the local gate, which CI also runs
./scripts/check.sh -m "not socket and not live"  # the gate inside an agent sandbox
uv sync                                        # install the package and dev tools
uv run pytest                                  # tests, socket tests need no sandbox
uv run pytest -m "not socket and not live"     # the rest, inside a sandbox
DROMON_LIVE_TESTS=1 uv run pytest -m live      # one real turn on the subscription
uv run dromon probe                            # report the engine's capabilities
uv run ruff check . && uv run ruff format --check .
uv run pyright                                 # strict type checks
uv run dromon --help                           # the client and its subcommands
uv run dromond --help                          # the daemon
scripts/worktree.sh new feat/some-thing        # an isolated worktree for a parallel line
scripts/worktree.sh rm  feat/some-thing        # remove it once merged
```

`scripts/check.sh` runs the template's hygiene checks, which include shellcheck on every
shell script and fail when it is not installed, then `uv sync --locked`, Ruff, pyright,
and pytest with warnings as errors and with its arguments. Live tests are never part of
it, because they spend the operator's subscriptions. `git config core.hooksPath
scripts/hooks` runs the gate before every push, in its full form, so a push from
inside a sandbox runs the sandbox form first and then uses `git push --no-verify`.

## Before Trusting a Green Run

- **Know what the command covered.** A run against a stale environment tested old code.
  Run `uv sync` after changing dependencies or entry points.
- **Count what was reached.** A suite that collected zero tests, or skipped them all, passes.
  Check the count in the summary line.
- **Opt-in tests are counted as skipped, not as passed.** The `socket` tests are
  deselected inside a sandbox, and the `live` tests always unless `DROMON_LIVE_TESTS=1`
  and `-m live` are given. Report how many were left out and why.
- **A local pass is not a remote pass.** Continuous integration runs on other operating
  systems and tool versions, and runs the socket tests a sandbox skips. Say which one was
  observed.

## Scratch and Secret Directories

`tmp/` is project-local scratch for drafts, commit messages, probe outputs and scratch
scripts. Its contents are ignored and only `tmp/.gitkeep` is tracked, so the directory
exists in a fresh clone. Prefer it to the system temporary directory, so that work in
progress stays with the project and survives a restart.

`secret/` holds local material that is never committed, such as credentials, and is
ignored whole. Never copy its contents into tracked files, commit messages, issues or
pull requests.

`scripts/check.sh` fails if anything under `secret/`, or anything under `tmp/` other than
its marker, is tracked. If `secret/disclosure-terms.txt` exists, one term per line, the
gate also fails when a tracked file contains any of those terms, and it prints only the
file and line. The scan cannot run in a clone or in continuous integration, which do not
have the list, and the gate says so when it skips.

## Coding Conventions

- **Python.** Version 3.12 or later. Typed throughout, checked by pyright in strict mode
  and ruff. The package lives in `src/dromon/`.
- **Portability.** Prefer plain data, explicit state machines and bounded loops to dynamic
  features, so that each component maps onto Rust and then onto Keleusma's total,
  bounded stream processors.
- **Trust.** Messages between session lines, from other harnesses and from other machines
  are untrusted data. Only the operator, through a client, has operator authority.
- **Tests.** Each behaviour has a test, including the error paths. A test that derives both
  sides of a comparison is preferred to one that pins a constant.
- **Documentation.** Record rationale, assumptions, failure modes and technical debt
  where they are introduced. Explain decisions, not implementations.

## Writing Style

Prose is polite, professional and academic. No contractions. No em-dashes or en-dashes,
and no colons, semicolons or parentheticals in prose, with front matter, tables, code and
equations exempt. Spell out acronyms on first use. Distinguish facts, inferences and
hypotheses, and state uncertainty explicitly. Never imply that verification is complete
when it is not.

Dates in tracked files are calendar dates in Coordinated Universal Time, written as
`YYYY-MM-DD`. The seed followed this, so its dates can run one day ahead of the local
timestamps in commits made in the evening, Pacific time.
