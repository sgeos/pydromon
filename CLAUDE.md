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
repository. Nothing beyond placeholder commands is implemented. A new session line picks
up development. The first milestone is [`docs/roadmap/V0_1_0.md`](docs/roadmap/V0_1_0.md)
and the design is [`docs/architecture/DESIGN.md`](docs/architecture/DESIGN.md).

**The port plan.** This Python prototype will be retired after a Rust port, which will in
turn be retired in favour of a Keleusma implementation. Write the prototype so that its
protocol and data model can be carried across unchanged. Behaviour that only Python makes
easy is a porting hazard and should be noted where it is introduced.

**Relationship to Keleusma and pykeleusma.** Keleusma is the target language of the final
implementation. The Python bindings live in the separate `pykeleusma` repository, which
on 2026-10-24 could not yet run a script. Do not edit either repository. Add `pykeleusma`
as a dependency only when a roadmap task needs it and it supports that task.

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

`main` is always green. Work happens on short-lived feature branches merged into `main` by
a no-fast-forward merge through a pull request once checks pass. Use scoped conventional
commits such as `feat:`, `fix:`, `docs:`, `test:` and `chore:`. Attribute assisted
commits to the assistant that actually contributed, with
`Co-Authored-By: Claude <noreply@anthropic.com>` for Claude,
`Co-Authored-By: Codex <noreply@openai.com>` for Codex, and the equivalent line for any
other assistant.

The remote is `https://github.com/sgeos/pydromon`, a public repository. Its git
configuration uses the GitHub command-line tool as the credential helper, because SSH
keys are unreachable from inside agent sandboxes.

**Irreversible or outward-facing actions need the operator's confirmation in session.**
That includes pushing to the remote, publishing to PyPI and force-pushing.

## Common Commands

```sh
uv sync                                        # install the package and dev tools
uv run pytest                                  # tests
uv run ruff check . && uv run ruff format --check .
uv run pyright                                 # strict type checks
uv run dromon --help                           # the client and its subcommands
uv run dromond --help                          # the daemon
```

## Before Trusting a Green Run

- **Know what the command covered.** A run against a stale environment tested old code.
  Run `uv sync` after changing dependencies or entry points.
- **Count what was reached.** A suite that collected zero tests, or skipped them all, passes.
  Check the count in the summary line.

## Scratch and Secret Directories

`tmp/` is project-local scratch for drafts, commit messages, probe outputs and scratch
scripts. Its contents are ignored and only `tmp/.gitkeep` is tracked. `secret/` holds
local material that is never committed, such as credentials, and is ignored whole. Both
follow the Keleusma repository.

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

## Writing Style

Prose is polite, professional and academic. No contractions. No em-dashes or en-dashes,
and no colons, semicolons or parentheticals in prose, with front matter, tables, code and
equations exempt. Spell out acronyms on first use. Distinguish facts, inferences and
hypotheses, and state uncertainty explicitly.
