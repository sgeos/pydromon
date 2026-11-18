# Upgrading a Project from an Old Revision

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

**Round 18, item 1, asked for by the operator.** A project can run a daemon pinned to an
old revision of Dromon for a long time, and there was no supported way to move it to a
current one. [`RESTART.md`](../protocol/RESTART.md) moves a running daemon to the code
installed beside it, a few commits on, and its preflight runs the new code's checks
against the live state. A jump across many rounds needs more, since it changes the
event log's format, the project file's rules and the engines, and since a return to the
old revision may be lost once the new daemon writes. This document is the supported
procedure. `dromon upgrade-check` is the command that backs it, and
`src/dromon/upgrade.py` describes its checks in full.

Two revisions are named throughout. **FROM** is the commit the project's daemon runs
now. **TO** is the commit it moves to, a commit of `main` whose gate passed in
continuous integration.

## What decides the outcome

**Whether TO opens FROM's log.** **Fact, from the codec's rule.** Every change to the
log is one way and adds, a kind, a field or a value, so code at TO reads every kind,
field and value an older revision wrote. A log opens under TO unless it is damaged or
holds a change TO does not know, which happens only where FROM is newer than TO.
`upgrade-check` does not rely on this rule. Its `replay` check opens a copy of the log
with TO's own code and replays every event, so the answer is observed for the log in
question.

**Whether TO uses FROM's snapshot.** A snapshot beside the log carries the state
version of the code that wrote it, the digest of the set of files in
`src/dromon/statechanges/`. TO uses it only where its own version is the same and a
full replay agrees with it. Otherwise TO ignores it once, replays the whole log, which
is always correct, and writes its own. Revisions before round 15 wrote no snapshot.
The `snapshot` check says which case holds.

**Whether a return to FROM stays possible.** The rollback floor table in
[`EVENT_LOG.md`](../protocol/EVENT_LOG.md), with the fragments under
[`floors/`](../protocol/floors/README.md), names each one-way change with its floor, the
first commit of `main` that opens a log holding it. FROM refuses the whole log, and
starts nothing, once the log holds any change whose floor FROM does not contain. The
`floor` check lists those changes. **Inference, from the table.** Several of them, such
as `line_cost_recorded` and the failure fields of `line_turn_ended`, are written with
every turn, so the first turn under TO is taken to end any return to FROM on the live
log. A return is then made from the copy taken before the upgrade, as the last section
gives.

## Before the upgrade

1. **Record FROM.** Read `git rev-parse HEAD` in the checkout the daemon runs from. From
   round 14 `dromon status` names the daemon's commit as well. Keep the commit with the
   copies below.
2. **Choose TO.** Take a commit of `main` whose continuous integration passed, and read
   `CHANGELOG.md` between FROM and TO for changes of the project file and the engines.
3. **Read the machine.** Run `machine-status`. The copies below need about three times
   the size of `.dromon/events.sqlite` in free space, and the check replays the whole
   log, so a large log is checked while the machine is quiet.
4. **Note what is open.** With FROM's client, read `dromon status` and
   `dromon questions`, and note the held lines, the open questions, the running runs and
   the leases. They are in the log and survive the upgrade, and the `held` check names
   the lines that come back held.

## Testing TO against copies

Nothing here touches the running daemon or its checkout.

1. **Install TO apart.** Make a separate checkout of TO, such as with `git worktree add
   DIR TO`, and run `uv sync --locked` in it. The running daemon's checkout is not moved,
   since a daemon that restarts or loads a module later would load TO's code from it, as
   the failure modes of [`RESTART.md`](../protocol/RESTART.md) say.
2. **Copy the state.** Make a directory with mode 0700 outside every line's reach,
   since the log holds message bodies and answers, and copy into it
   `.dromon/events.sqlite`, and where they exist `.dromon/events.sqlite.snapshot`,
   `.dromon/events.sqlite.sealed` and `.dromon/dromon.toml`. With the daemon stopped a
   plain copy is consistent. While it runs, `sqlite3 .dromon/events.sqlite ".backup
   COPY"` takes a consistent copy of the log.
3. **Give the worktrees.** The project file's working directories resolve under a
   project root. `--root` may name the live root, which the check only reads, as a test
   shows, or a copy of it holding the worktrees, without `.dromon`.
4. **Run the check** from TO's checkout.

   ```sh
   uv run dromon upgrade-check --from-log COPY/events.sqlite \
       --project-file COPY/dromon.toml --root ROOT --from-commit FROM
   ```

   It copies the copy again into a private scratch directory, which it removes, and
   never writes the file it is given, the root or the repository. It makes no socket,
   takes no lock, builds no engine and asks no daemon anything. `--json` prints the same
   checks as JSON. Its exit code is 0 when no check failed and 1 otherwise.

| Check | Fails or warns when | What to do |
|---|---|---|
| `copy` | A file given is missing, or cannot be copied | Copy again |
| `replay` | TO cannot open the log | Stop. TO is older than the log, or the log is damaged. Choose another TO, or restore an earlier copy |
| `snapshot` | A snapshot of TO's own version differs from a full replay | Stop and report it. A snapshot of another version only says it is ignored, which passes |
| `start` | TO's own start check, `dromond --check` on the copies, refuses, such as for a project file or sandbox TO no longer accepts | Change the copy of the project file until the check passes, then make the same change to the live file at the upgrade |
| `held` | Lines are held or hibernated in the started state, which warns | Plan their recovery, as below |
| `floor` | One-way changes are newer than FROM, which warns | Keep the final copy below, which is then the only way back |

## The upgrade

1. **Stop FROM's daemon.** Let running turns end, then run `dromon stop` with FROM's
   client. FROM's client is used, since a client and a daemon of revisions far apart may
   not share every request.
2. **Take the final copy.** Copy the state again, as above, now that nothing writes.
   This copy is the rollback point. Run `upgrade-check` on it once more.
3. **Move the checkout to TO**, and run `uv sync --locked` in it.
4. **Apply the project file changes** the `start` check needed.
5. **Check and start.** Run `uv run dromond --project ROOT --check`, which runs the same
   start checks on the live state, then `uv run dromon start`. `dromon status` then
   names TO's commit.

## After the upgrade

- **Held lines.** A line held before the upgrade stays held, with the question that
  holds it. A hold written by a revision before the hold cause was added, in #119,
  records none, which the `held` check shows as not recorded. Read the question, and once its cause is understood answer it, or
  run `dromon release LINE`. A hibernated line is awakened with `dromon awaken LINE`.
- **Lines held by their first turn.** An engine's adapter may have changed between FROM
  and TO, so a line's first turn can fail and hold it. The hold's question gives the
  cause. A line whose kept engine session cannot be resumed is started afresh with
  `dromon renew LINE`, which keeps its mail, brief and files.
- **Roles.** A role the project file no longer names is left as it was, and the start
  says so. Read `dromon role` and retire or update what is no longer wanted.

## Rolling back

- **Before TO has written a newer change.** Stop TO's daemon, move the checkout back to
  FROM, run `uv sync --locked`, and start. FROM opens the log only while it holds no
  change the `floor` check listed, so in practice only before the first turn.
- **After.** Stop TO's daemon. Move the live `.dromon/events.sqlite` and the files beside
  it aside, never deleting them, and put the final copy in their place. Move the
  checkout back to FROM, run `uv sync --locked`, and start. Everything recorded since
  the upgrade, mail, answers, holds and runs among it, is then absent from the log,
  though it stays in the file moved aside. Engine sessions that ran under TO kept their
  own history, so a line resumed under FROM may see turns its log does not record.
- **Never edit the log.** Removing the newer events by hand is refused by the log's own
  triggers and loses what they record. The copy is the supported way back.

## Residuals

- **What the check cannot find.** The `start` check is `dromond --check`, so recovery,
  the process table, engine sign-in and the first turns are not exercised, as
  [`RESTART.md`](../protocol/RESTART.md) says of the preflight. A line that fails only
  when its engine runs is found after the upgrade.
- **The floor is read from the repository.** It needs TO's repository with FROM in its
  history. A shallow clone, or an installation without its repository, reports the
  floor as unknown, and `--repository` names another. The check lists changes TO may
  write, not the ones it will.
- **What the check reads of the user's.** The start check reads what `dromond --check`
  reads, such as each Grok line's profile under the home directory, and the
  environment's credentials for lines that name secrets. It writes none of them.
- **Tested against one old revision.** `tests/test_upgrade.py` checks a log written by
  the code of `6567167`, from 2026-10-31. A log from another revision is checked by
  running the command on it, which is the procedure's point.
