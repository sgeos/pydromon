# Supervised Runs

**Status.** Task of 2026-10-31. Specified and implemented in `dromon.runs`, the
scheduler and the event log. This document is normative for the feature.

## Purpose

A line could already start a long command in the background, but nothing recorded who
owned it. A successor seat, or a daemon restarted while the command ran, could not tell
whether it still ran, how it ended or where its output went. A supervised run is the
smallest feature that closes that gap. It runs a command the project declares, records
the supervisor's process identity and start time durably, records a receipt when it
ends, and reconciles the record against the process table after a restart.

## The command

The project file declares the commands, and a line names one.

```toml
[runs.commands]
gate = ["./scripts/check.sh", "-m", "not socket and not live"]
device = ["./scripts/device-run.sh"]
```

Each command is an array, its program and its arguments, never a shell string. A line
passes no arguments, so a name runs exactly what the operator wrote. **Decided.** Free
arguments, a working directory of the line's choosing and an environment of the line's
choosing are left out, because each widens what a line can make the daemon run, and no
case needs them yet.

The command runs in the line's working directory, resolved to its real path, with the
daemon's `PATH`, `HOME`, `LANG`, `TERM` and `TMPDIR` and with `DROMON_RUN_ID`, the
run's identifier. A program named by a relative path, such as `./scripts/check.sh`,
resolves against that working directory, and a bare name such as `make` through the
daemon's `PATH`. An operator who wants a command whose code no line can write declares
it by an absolute path outside every line's working directory.

A declared command must not daemonize, by `setsid` or otherwise, because a process that
leaves the supervisor's session is not found when a lost run is reconciled, so the run
is cleared automatically while that process may still run, as the lost run section
below says. **Decided by the operator, 2026-10-31.**

## Trust

A run executes with the daemon's authority and outside every engine sandbox. That is
the point of it, since it must outlive the engine, and it is also its risk.

- **Declaring is the operator's decision.** Only the project file declares a command,
  and the file carries operator authority. A line cannot add one.
- **A writing line can change what runs.** A declared script lives in a working
  directory that a writing line can edit. Such a line can therefore run code outside
  its sandbox by editing the script and starting the run. **Inference.** This is the
  same exposure as the operator running the gate by hand in that worktree. An operator
  who does not accept it declares only commands whose code no line can write.
- **Syncing a worktree runs the line's build.** `dromon sync-worktrees` runs
  `uv sync --locked` as the operator, outside any sandbox and with the network, and uv
  builds the project from the worktree's `pyproject.toml`, its local build backend and
  `uv.lock`, all of which a writing line can change. A supervised run of the line's
  gate already runs that line's code, but a sync runs it for every role at once, at
  the operator's hand. **Round 14, audit finding S1.** The command therefore compares
  each worktree's `pyproject.toml`, `uv.lock` and every `backend-path` directory with
  `origin/main`, in the working tree and with untracked files, prints the difference,
  and syncs a worktree that differs only on the operator's yes or with
  `--accept-changes`. A worktree that matches is synced without a question, and one
  whose difference cannot be read is not synced. **Round 14, re-review of 522c146,
  S1b.** Those three missed inputs, so a vendored dependency's edited backend ran in a
  sync with no difference shown. The command now compares `pyproject.toml`, `uv.lock`,
  `uv.toml`, `.python-version`, `.python-versions` and every local source directory
  that `[tool.uv.sources]` or `uv.lock` names. Where the files a build reads cannot be
  bounded it compares the whole worktree and says why. That is so for a backend path,
  a workspace, a build backend other than uv's own `uv_build`, which may run any file
  of the project, an index, link, cache or interpreter mirror in a local place, a
  source from a local git repository or address, and a configuration file Python's
  reader cannot parse. A local source outside the worktree leaves it unsynced.
  **Residual.** A file git ignores inside a compared directory is not shown, a build
  backend fetched from an index is shown only as the `pyproject.toml` line that names
  it, and configuration uv reads outside the worktree is not compared. **Round 14,
  final check of 951b9e7, item 1.** The worktree's `.venv`, which git ignores, was not
  compared either, and the audit line confirmed that `uv sync` ran an executable
  `.venv/bin/python` a line had written, with no difference shown. Each sync now
  builds the environment in a fresh private directory in the state directory's
  `sync-envs`, which no line's sandbox can write. It runs `uv venv --relocatable`
  there, then `uv sync --locked --link-mode copy` with `UV_PROJECT_ENVIRONMENT`
  naming it and `VIRTUAL_ENV` removed, and checks that `pyvenv.cfg` says
  `relocatable = true`. Only then does it move the worktree's `.venv` into the
  private directory, move the new environment into its place, and remove the private
  directory without following links, so nothing in the old environment is read or
  run. **Fact, observed with uv 0.13.5 on 2026-11-01.** Neither step ran a planted
  interpreter, while a plain `uv sync` in the same worktree did, and a moved
  environment ran `pytest` with nothing reinstalled by a later `uv sync --locked`.
  **The gate's own uv run.** It is unchanged. A line's `uv sync --locked` and `uv run`
  use the worktree's `.venv` as before and find it current. Whatever a line then
  writes there, the next sync moves aside unread. Packages are copied, since on Linux
  uv hard links from the operator's cache by default, and a line writing such a file
  would change that cache. **Residual.** A line that makes a new `.venv` between the
  two moves leaves the worktree with no environment until the next sync. A gate
  running during the swap may fail on a module it had not loaded, so sync while no
  line runs one. A line whose sandbox is disabled can write the state directory too.
- **Who may start or stop.** A seat may start or stop its role's runs only while it
  owns the role, so the one-writer rule holds for what a run writes. Every line, a
  seat that owns its role included, also needs `write` access to its working
  directory, since a run writes there outside the line's sandbox. A line with `read`
  access may read a run's record but not start or stop one. **Review of 2026-10-31,
  R3.** A seat that owned its role was once let through without this check, so a
  read-only seat could write its worktree by a run. **Round 14.** A commit command is
  the one exception, since it runs in the daemon's checkout of a committed tree and
  not in the working directory, as the section on a gate run that a reading line may
  start argues.
- **The output path.** The output file is under the working directory, which a line can
  write, while the daemon writes it outside any sandbox. The daemon therefore opens
  `tmp`, `tmp/runs` and the file one step at a time, each relative to the one before,
  with `O_NOFOLLOW`, and creates the file exclusively. A symbolic link put in the way
  is refused, and no swap after a check can redirect the open. The digest is read the
  same way, opened with `O_NONBLOCK` and read only once `fstat` shows a regular file,
  so a named pipe put at the name returns at once with no digest rather than holding
  the process worker until a writer comes. **Review of 2026-10-31, R1.** Before that
  fix such a pipe stalled every reconciliation, as a probe showed.

## The supervisor

`python -I -m dromon.runs` is the launcher's counterpart for a run. **Decided.** The
launcher itself is not reused, because it ends its harness when the daemon dies, which
is right for an engine and wrong for a build that must outlive the daemon.

1. The daemon opens the output file and starts the supervisor in a session of its own,
   with that file as its output, a pipe as its input and a second pipe for readiness.
   No stop of the daemon's group reaches it. `-I` keeps a module in the working
   directory from being imported, and the supervisor's own directory is `/`.
2. The supervisor installs its stop handlers and writes `R` on the readiness pipe. The
   daemon waits for that, so a stop can never find the supervisor without its
   handlers.
3. The daemon reads the supervisor's start time from the process table, records
   `run_started`, and only then writes `1`, the word to go, on its input.
4. The supervisor starts the command in a process group of its own and waits for it.
   Its input closing without the word means the run was never recorded, and it exits
   with status 3 without starting anything. So a daemon that dies between the start and
   the record leaves no unrecorded run.
5. When the command ends, the supervisor writes a receipt, `RUN.run` in the daemon's
   report directory, under a temporary name first and then renamed, with the status
   `exited` and the exit code, which is negative for a signal as Python reports it.
6. `stop_run` sends the supervisor `SIGTERM`. It ends the command's group with
   `SIGTERM`, and after 10 seconds with `SIGKILL`, and its receipt says `stopped`. A
   stop before the command starts means it never starts, with a `stopped` receipt and
   no exit code. The signal handler only notes the stop. The thread that waits for
   the command sends both signals, each between two polls that found the command not
   yet reaped. The group's identifier is the command's own, which cannot be reused
   while the command is unreaped, so no signal reaches a later group of the same
   identifier. **Review of 2026-10-31, R7.** A timer once sent `SIGKILL` on its own
   and could do so after the command was reaped.
   **Round 15, item 2.** The same two signals also go to the descendants below the
   supervisor, found by a walk of the process table made just before each signal and
   before the group signal, because that signal ends the command and orphans what it
   left. The first full-gate run of the change failed for walking after it. A
   descendant that called `setsid` is ended with the command. See the section on
   descendants in a session of their own.

A program that cannot be started gives an `exited` receipt with exit code 127 and the
reason.

## Identity

The record holds the supervisor's process identifier and the start time `ps` gives for
it, read as `lstart` in the C locale and in Coordinated Universal Time. A later process
that reuses the identifier has another start time, so the record is never taken for it.

**Platform limit.** `ps` gives the start time to the second. A reuse of the same
identifier within the same second is not told apart. **Assumption.** That requires the
identifier space to wrap within a second, which this design takes as negligible.

## Reconciliation

The daemon reconciles every run that has not ended whenever it collects launcher
reports, which is at each rest of the scheduler, at most a minute apart, after each
turn and at start. It runs on the scheduler's process worker, never on the event loop.
For each open run, in order:

1. A supervisor this daemon started is reaped first, so one that has exited is not seen
   as a process not yet reaped.
2. A receipt ends the run as the receipt says.
3. A supervisor whose identifier still shows its recorded start time is running, and
   the run stays open.
4. Otherwise the receipt is read once more, since the supervisor writes it and then
   exits. Without one, the run ends as `lost`. The note says either that the supervisor
   ended without a receipt, as when it is killed or the machine restarts, so the
   command may still run, or that its identifier now belongs to another process.
5. A process table that cannot be read ends nothing. The failure is said once on the
   daemon's standard error and the next reconciliation tries again. `ps` answers a
   missing process with status 1 and nothing on either stream, on macOS and Linux, so
   a status 1 with a complaint, such as a denial, counts as a failure to read.
6. A lost run, once recorded, is looked at again at each reconciliation until it is
   cleared, as the next section says.

Each end records `run_ended` with the digest and size of the output file, read when the
end is recorded, then removes the receipt, and sends a note from the harness to the
scope's present holder, the role's owner or else the line that started it.

**The test summary, round 14, item 2.** The same reconciliation reads the last 64 KiB
of the output, opened as the digest is, and finds pytest's final line, such as `5162
passed, 10 skipped, 48 deselected in 231.52s`, with the skip reasons of the short
summary before it, which `-rfEs` in `pyproject.toml` makes pytest print. `run_ended`'s
note then ends with `Unverified, the run's own output ends with the test summary` and
that summary in quotation marks, at most eight reasons and the rest counted, each
line cut to 200 printable characters, each reason to 80 characters after its place,
and the whole note to 1,024 bytes. pytest names no deselected test, so only their
count is given, and the run's command in the log says which marker expression
deselected them. **Trust.** The output is under the line's working directory, which
a writing line can edit, so the summary is what the output says, never what the
daemon saw run. **Audit of round 14.** A command can print a forged summary last, and
the note is shown by `run_status`, by the holder's `Run ended` note and by `dromon
run status`, so the label travels inside the note to all three. Only the last short
summary block is read, which is the first final line after the last `short test
summary info` mark, or, with no mark, the last final line, and a mark with no final
line after it gives nothing. A lost run records no summary at all, since it has no
exit code and a green summary would read as its result. An output without a final
line adds nothing. **Residual.** A command that prints a whole forged block last is
still believed as far as the label allows, since only the exit code is the daemon's
own observation.

## A lost run

A lost run's supervisor is gone without a receipt, so its command may still run, in a
group of its own. **Decided on 2026-10-31, review R2.** It therefore keeps its scope
blocked, so a successor cannot start a second command while the first may still run.
It stops blocking only when one of these is recorded as `run_cleared`.

- **No process is left.** Each reconciliation lists every process with its group from
  `ps -A -o pid=,pgid=` and asks each one's session with `getsid`, since `ps` on macOS
  gives no session identifier. The supervisor started its own session, so its
  identifier names both its group and its session, and the command's group lies
  within that session. When no process is in either, the run is cleared with cause
  `empty`.
- **An operator clears it.** `dromon run clear RUN`, the operation `run_clear`,
  clears it with cause `operator` and the operator's name, after the operator has
  inspected what is left of the command. No line can clear a run.

A process table that cannot be read, or a session that cannot be asked, keeps the run
blocking. A process that left the session with `setsid` is not found, so a command
that daemonizes itself in that way may outlive its clearance. A later process that
reuses the supervisor's identifier as a group or session leader is taken for the
run's, which keeps the scope blocked until an operator clears it, the safe side.

## The scope and the successor

A seat's run belongs to its role, and any other line's run to the line. A scope has at
most one open run.

**A successor must read the record first.** `start_run` takes `previous`, the scope's
latest run as `run_status` names it, or `none` when there is none. The event log
refuses a `run_started` whose `previous` is not the scope's latest run, one while the
latest is open, and one while the latest is lost and not cleared. A seat that gains the role therefore cannot start a run without
having read its predecessor's, and cannot start a second one while the first may still
run. A refusal names the latest run with its record, so the refusal itself is the
reading.

**A failed run and its correction, round 14, item 2.** A run fails when it ends other
than by exiting with code 0, so `stopped` and `lost` fail too. Each failed run is
corrected by the next run of the same command in its scope that exits with code 0.
`run_status` names, for a failed run, the run that corrected it, and for that run each
failed run it corrected, read from every run of the scope, so a link reaches a run
older than the ten shown. **Decided.** The link is derived from the log, not recorded,
so no event changes and an older log is linked as well. Both runs stay in the log, and
each output file stays under `tmp/runs` with its own name, since Dromon removes none.
**Inference.** A pass of the same command is taken as the correction even where the
tree changed for another reason, since the daemon cannot tell why a run passed.

## Tools

| Tool | Effect |
|---|---|
| `start_run` | Arguments `command` and `previous`, and `commit` for a commit command only. Starts the declared command, and answers with the run's identifier and output file |
| `run_status` | No arguments. The scope's latest ten runs, newest first, with what `previous` must be and the declared commands |
| `stop_run` | Argument `run_id`, the scope's open run. Signals its supervisor after checking its start time again |

They are offered while the project declares a run command, or while the line's scope
has a run to read. The operator's `dromon run clear RUN` is the only way to clear a
lost run by hand, and is offered to no line or external agent.

**Round 14, item 11.** The operator reads runs with `dromon run status`, the request
`runs`, which lists the latest ten runs of every scope with each run's command, line,
start, end, exit code and output file, and with `dromon run status RUN` for one run
with the rest of its receipt. Before this the operator read `run_ended` from the event
log by hand. The request reads the log and records nothing. It is bounded by the
number of scopes, since each scope gives at most ten runs.

## Events and rollback hazards

`run_started`, `run_ended` and `run_cleared` are specified in
[`EVENT_LOG.md`](../protocol/EVENT_LOG.md), as are `lease_acquired` and
`lease_released`, which the same task added. **Rollback hazard.** A daemon older than
2026-10-31 does not know these kinds, so once any is recorded it cannot read the log.
A supervisor started by a newer daemon also keeps running under an older one, which
does not know of it, and its receipt waits unread in the report directory. A daemon
of 09003fa, before the review, reads no `run_cleared` and lets a lost run's scope
start another run. The rollback floor in [`EVENT_LOG.md`](../protocol/EVENT_LOG.md)
names the commits of these and every other one-way change.

## Descendants in a session of their own

**Round 15, item 2, requested by the operator on 2026-11-01.** A signal to the
command's group misses a descendant that called `setsid`, which leaves the group. The
question was whether Dromon can find such a descendant by the mark it puts in a line's
environment, or by another means that cannot reach another line's or the operator's
processes. What was decided and why.

- **The mark is not used.** **Fact, observed on macOS, Darwin 27.** The daemon cannot
  read another process's environment there, as `environment_reading_works` finds, so a
  walk by mark finds nothing. **Fact, by design.** A run's command carries no line's
  mark, only `DROMON_RUN_ID`, so that it is not a line's process and no line can stop
  it with `stop_process`. **Inference.** Adding a mark of the run's own would work on
  Linux, which reads `/proc/PID/environ`, but a descendant can clear its environment
  with `env -i`, and the mark would have to survive a daemon restart, which means
  recording a secret in the event log. Ancestry has neither weakness.
- **What is used.** `dromon.processes.attached` lists what descends from a root by
  parent, and the members of the groups those lead, whatever session each is in. The
  supervisor walks from its own process at `SIGTERM` and again at `SIGKILL`, and
  signals only what that walk found. A process identifier remembered from the first
  walk is never signalled at the second, so one that ended and was reused is not
  reached unless it descends from the supervisor. **Re-review of 4da3e0e.** Within one
  stop, between the walk and a signal, a descendant can also end and its identifier go
  to a process that is not one. The walk therefore reads each process's start time
  with `lstart`, and just before each signal `signal_if_same` reads the process's
  parent and start time again and turns the signal away if either differs, if the
  process is gone, or if `ps` cannot say. These checks come before the group signal,
  since the group signal ends the command and every orphan then has another parent.
  For the same reason a child is signalled before its parent, deepest first, because a
  parent that ends orphans its child and the parent check would turn the child away.
  The second full-gate run of 2026-11-01 showed this with a descendant in a session of
  its own, whose parent was the command.
  **Limit.** The check is itself a reading, so a window of a few milliseconds remains,
  smaller and not zero. It runs the `ps` command once per descendant. The `ps` runs in
  the C locale so that `lstart` is five words on every host and is read the same way
  twice. **Hypothesis.** `lstart` has one-second resolution, so a reuse within the
  same second by a process with the same parent is not told apart, and such a process
  is a child of the same parent and so a descendant anyway. A process is found only by its
  ancestry from the supervisor, and a process in another session cannot join a group a
  descendant leads, so a line's other processes and the operator's are out of reach.
- **A command that ends first** is not followed by a return until the descendants
  outside its group are gone or the 10 seconds have passed, and the survivors are
  then sent `SIGKILL`. The receipt note of a stopped run says how many descendants
  outside the group were signalled, or that the process table could not be read.

**What is not found, and what an operator must avoid.**

- **A descendant whose parent has exited** is no longer below the supervisor and is
  not found, because it was given to the system's first process. The launcher is a
  child subreaper on Linux and so finds such orphans, but it also reaps them. A run's
  supervisor is not a subreaper, since every orphan would stay unreaped until the run
  ends, and that behaviour could not be tested on this platform. **Hypothesis.** A
  subreaper supervisor that reaps adopted children with `waitid` and `WNOWAIT` is
  feasible on Linux and is the next step. macOS has no subreaper, so there a
  daemonizing command escapes whatever is built.
- **A command must not double fork or daemonize,** as the section on the command
  already says. The walk improves a stop for a descendant that is still attached to a
  living parent, such as a build tool's worker, and not for one that detached itself.
- **A supervisor that is killed,** or a command that ends and leaves background
  processes without any stop, is the lost-run check's, which finds only the
  supervisor's group and session.
- **The walk is not frozen.** The launcher stops the harness's group first so that
  nothing forks during its walk. That cannot be done here, because a stopped child
  makes Python 3.15's asyncio child watcher block on it, as the relay found. A
  descendant forked between the walk and the signal is not found.

## Failure modes

- **A supervisor killed with `SIGKILL`,** or a machine restart, leaves no receipt. The
  run ends as `lost`, and its command may still run, in a group of its own. The note
  says so, and the run blocks its scope as the section on a lost run says. Dromon does
  not end that group, since the group identifier could already be reused.
- **A daemon that dies after recording a run and before the word to go** leaves a
  supervisor whose input closes, so it exits without starting the command and without
  a receipt. The run then ends as `lost` with the note above, although its command
  never ran, and is cleared once its session is found empty. **Inference.** The window
  is one write to a pipe, so this is rare, and the record errs toward saying less is
  known.
- **A word to go that cannot be delivered,** because the supervisor is already gone,
  means the command never started. The daemon closes the supervisor's input, records
  the run as `stopped` with no exit code, and tells the line that nothing started.
  **Review of 2026-10-31, R6.** The result was once ignored, and the caller told the
  run had started.
- **Two starts at once** both pass the scheduler's checks before either is recorded.
  The event log refuses the second, and its supervisor is abandoned without the word
  to go, so its command never runs.
- **Checking and signalling a supervisor are two steps.** `stop_run` reads the
  supervisor's start time and then signals it, and POSIX offers no way to signal a
  process only if it still has a given start time. A supervisor that ends between the
  two, with its identifier reused by then, sends `SIGTERM` to the later process.
  **Accepted, review of 2026-10-31, R5.** That needs the identifier to come round
  within the time `ps` takes to answer. A port on Linux may close it with a process
  descriptor.
- **A command that leaves background processes** after it exits leaves them running.
  The supervisor ends its group, and the descendants below it, only on a stop.
- **A receipt that is malformed** counts as none, so its run ends as `lost` once the
  supervisor is gone.
- **The report directory** is the daemon's state, which no line can write. A daemon
  without one refuses every run.
- **A daemon in a sandbox that denies `ps`,** as an agent's sandbox does, cannot
  identify a supervisor, so every `start_run` is refused and nothing starts.
- **An output larger than 1 GiB** is recorded with its size and without a digest.

## Example, a run of the full gate

**Status.** An example of a declaration and of how a line uses it. This document does
not declare it. **Declaring a command is the operator's decision**, made in the project
file, and nothing here changes what the project declares.

**The problem.** A line's engine sandbox denies binding a Unix domain socket and running
`ps`, so a line runs `./scripts/check.sh -m "not socket and not live"` and reports that
the socket tests were deselected and that seven launcher tests that read the process
table were skipped. A run executes outside every engine sandbox, as the trust section
says, so it can run the whole gate.

**The declaration.**

```toml
[runs.commands]
full-gate = ["./scripts/check.sh"]
```

With no arguments the gate runs the socket tests and the process-table tests, and
`pyproject.toml` still deselects the live tests, which spend subscriptions and are
never part of it. A run takes no arguments from a line, so a line cannot add `-m` to
this one or ask for the live tests. The command is not given a time limit, as the
deferred list says, and the gate takes a few minutes. Two cases stay refused. A daemon
that itself runs in a sandbox that denies `ps` refuses every `start_run`, and a line
with `read` access to its working directory may read a run but not start one, so a
review seat cannot.

**How a line starts it.** The line must own its role, or be a line of its own, and have
`write` access to its working directory.

1. Call `run_status`. It names the scope's latest runs and gives the `previous` value
   the next start must carry, or `none`. A scope with an open run, or a lost run that
   no one has cleared, cannot start another.
2. Call `start_run` with `command` set to `full-gate` and that `previous`. It answers
   with the run's identifier and its output file under `tmp/runs` in the working
   directory.
3. End the turn or do other work. When the run ends, a note from the harness reaches
   the scope's present holder, which is the role's owner or else the line that started
   it, and a daemon restart or a handoff does not lose the run.

**How a line reads the result.** The receipt is `RUN.run` in the daemon's report
directory, which no line can read, so a line reads what the daemon recorded from it.

- `run_status` shows the run's end, `exited` with an exit code, `stopped` or `lost`.
  Exit code 0 means every check of the gate passed. Any other code means a check failed,
  and 127 means the program could not start. A `lost` run says its supervisor ended
  without a receipt, and it blocks its scope until it clears or the operator clears it.
- The output file holds the gate's output, and `run_ended` records its size and digest.
  The line reads the file for the counts in pytest's summary line and reports them
  with the run's identifier and the commit it read, as a sandbox gate is reported.

**What the result proves.** The gate ran in the worktree as the line left it, with the
line's own `scripts/check.sh` and tests, which a writing line can edit. A green run
shows that the tree passes the gate it carries, not that the gate was untouched. An
operator who wants more declares a script outside every worktree, by an absolute path,
that fetches the gate from the reviewed commit. The same trust section says why.

## A gate run that a reading line may start

**Status.** Round 14, item 1. Designed, then implemented in `dromon.commitruns` and
the scheduler at the operator's direction, since four lines asked for such a gate.
Two decisions changed from the design, each said where it applies. The checkout is
in the daemon's state directory rather than under `tmp`, and `run_started` gains
`checkout` beside `commit`.

**The problem.** A review seat has `read` access to its working directory, so it may
not start a run, as the trust section requires, since a run writes outside the line's
sandbox. A review therefore reports a sandbox gate with the socket and process-table
tests left out, and the operator runs the full gate by hand. The run proposed here lets
a reading line start a gate on a commit it names, without letting it write any
worktree, by running the gate in a checkout that no line can write and that Dromon
removes afterwards.

**The declaration.** The operator declares such commands apart from the others, so
that no existing command becomes startable by a reading line.

```toml
[runs.commit_commands]
review-gate = ["./scripts/check.sh"]

[runs.commit_refs]
patterns = ["refs/remotes/origin/main", "refs/remotes/origin/feat/*"]
```

A name may not be in both tables, and declaring a commit command requires at least one
pattern, at most 16. **Round 14, review of 1449c5c, C1.** Each pattern must begin
`refs/remotes/REMOTE/`, with a remote named without wildcards, and anything else is
refused at start and reload.

**Who can move each allowed ref.** The following table is the operator's to check
against the project's lines before declaring a pattern.

| Ref | Who can move it |
|---|---|
| `refs/remotes/REMOTE/...` | The operator, by a fetch from that remote. A Claude Code line's enabled sandbox denies writing `refs/remotes` and `packed-refs` in the common git directory, as [`SANDBOX.md`](../protocol/SANDBOX.md) gives, and a Codex or Grok line keeps the whole `.git` read-only. Whoever can push to the remote moves it at the next fetch, so the remote's own access rules bound it too |
| `refs/heads/...`, `refs/tags/...`, `refs/notes/...` and every other ref | Any line whose sandbox may write the common git directory, which a Claude Code line's may, so such patterns are refused |

**Assumption.** The repository keeps its refs in git's files backend. A commit run is
refused in a repository whose `extensions.refStorage` names another, since the guards
name the files backend's places. A line whose sandbox is disabled is not confined at
all, and could move any ref, as it could write any file.

**How a line starts it.** `start_run` takes an optional argument `commit`, required for
a commit command and refused for any other. It must be a full commit identifier of 40
or 64 lowercase hexadecimal characters, never a name such as a branch, so it cannot
move between the check and the checkout. The line needs only `read` access to its
working directory, and a seat must still own its role, so the scope rule holds. A
commit run is stopped with the same access it was started with.

1. The daemon refuses a start while another commit run of the same scope is starting,
   and within 120 seconds after the scope's latest refused start, which
   `commit_run_refused` records. It then checks the commit with `git cat-file -e
   COMMIT^{commit}`, checks that the repository keeps its refs in the files backend,
   and checks that a ref matching one of `patterns` points at it, with `git
   for-each-ref --points-at`, in the project's repository, with arguments as an array
   and never a shell string. **Round 14, review of 1449c5c, C1.** Only a tip
   qualifies. Before, any commit a matching ref contained did, so a commit merged and
   reverted since, or fixed since, could run. **Round 14, re-review of 522c146, C1a.**
   The checkout's `.git`, the common git directory, its `refs`, `refs/remotes` and
   `packed-refs`, and each directory and loose file of a matched ref below
   `refs/remotes` must be real and owned by the daemon's user, not links, checked with
   `lstat`. `refs`, `refs/remotes` and `packed-refs` are read before and after the
   match, and a change of any refuses the start, so a swap while git reads them is
   caught too, and so is a fetch that ends meanwhile, which a retry after the cooldown
   passes. Before, with `refs` replaced by a link to a line's directory, a line's
   commit was accepted as the tip of `origin/main`. **Round 14, final check of
   951b9e7, item 2.** A real directory a line moved into the place of `refs` passed
   these checks, as the audit line confirmed. The daemon therefore reads the device
   and inode of `refs` and `refs/remotes` once as it starts, and refuses every commit
   run while either differs, with a restart as the remedy once the operator has
   checked them. Refs that cannot be read at the start refuse every commit run until a
   restart. **Fact, observed with git 2.56.** `git update-ref` of a remote-tracking
   ref, `git pack-refs --all`, `git gc --prune=now` and `git remote remove` keep both
   directories. **Inference.** A fetch, which updates remote-tracking refs the same
   way, keeps them too. It was not run, since lines have no network. **Residual.** A
   swap made before the daemon starts is anchored as found, and the sandbox's rule
   against renaming a parent of a denied path remains the first barrier, as
   [`SANDBOX.md`](../protocol/SANDBOX.md) gives.
2. It makes `commit-runs/RUN`, mode 0700, in the daemon's state directory, holding
   `home`, empty and mode 0700, and `tree`, filled with `git clone --no-checkout
   --shared` of the project's repository, then an `info/attributes` of `* -filter` in
   the clone, then `git checkout --detach COMMIT`, then a check that `HEAD` is the
   commit. A clone writes nothing into the project's own repository, as `git worktree
   add` would. **Changed from the design.** The design put the checkout under `tmp` in
   the project root. A line whose working directory is the project root could write
   there, so it is in the state directory instead, which every line's sandbox denies.
   The start makes or checks `commit-runs` with the other private directories.
3. The run then starts as any run does, with `tree` as its command's working
   directory, `home` as its `HOME`, and its output file under `tmp/runs` in the
   reading line's working directory, which the daemon writes, not the line, as it
   writes the output of any run.

**Every git call of a commit run.** **Round 14, review of 1449c5c, C2.** The review
found that a commit's `.gitattributes` and `.lfsconfig` made the checkout start the
large file filter the operator's global configuration defines, which read the
commit's `.lfsconfig` and reached for the address it named before the command ran,
with the operator's credential helper available. Each git call therefore runs with
an environment of the daemon's `PATH` and fixed values alone, so no `HOME`, with
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` set to `/dev/null` and
`GIT_CONFIG_NOSYSTEM`, `GIT_ATTR_NOSYSTEM`, `GIT_NO_REPLACE_OBJECTS`,
`GIT_LFS_SKIP_SMUDGE` and `GIT_TERMINAL_PROMPT=0`. It passes `core.hooksPath=/dev/null`,
`core.fsmonitor=false`, an empty `credential.helper`, `submodule.recurse=false` and an
empty large file filter that is not required. The clone's `info/attributes` outranks
every `.gitattributes` the commit carries, so no path names a filter even where a
configuration git still reads, the project repository's own, defines one. Each call
runs in a session of its own and is bounded to 60 seconds, after which its whole
process group is killed, so a filter or other child does not outlive it.

**Starts are limited and isolated.** **Round 14, review of 1449c5c, C3.** The checks,
the clone and the removal run on a worker of their own, so a slow checkout never
delays the stops, launches and reconciliation the process worker serves. A scope has
at most one commit run starting at a time, and a second start is refused, not queued.
A start refused in the checks or the checkout costs the commit worker up to six git
calls, so it is recorded as `commit_run_refused`, with the line, its role, the
command, the commit, the reason and the time, and its scope starts no commit run for
120 seconds after, a cooldown that outlives a restart since it is read from the log.
A start refused before it reaches the worker, such as for a malformed commit, records
nothing.
4. `run_started` gains the optional fields `commit` and `checkout`, the directory the
   command ran in, so the record names what ran and where without reading the
   project's configuration. `workdir` then holds only the output.
5. The daemon removes every checkout that no blocking run names, after each
   reconciliation, with a removal that does not follow links, `shutil.rmtree` with a
   directory descriptor where `avoids_symlink_attacks` is true. It refuses commit runs
   where it is not. A lost run's checkout is kept until the run is cleared, since its
   command may still run there.

**The trust argument.** It is written for the operator to accept or refuse.

- **What runs.** The code at the tip of a remote-tracking ref matching `patterns`,
  which the operator's own fetch put there. **Fact.** A reading line can neither write
  that code nor change it after the check, since the identifier is a digest of the
  tree and the checkout is made by the daemon in a directory no line can write.
  **Inference.** A writing line with an enabled sandbox cannot move such a ref either,
  as the table above gives, so a line cannot choose what a commit run executes beyond
  choosing among the tips the operator fetched.
- **What a reading line gains.** It can make the daemon run any such commit's gate
  outside every sandbox, with the daemon's authority. **Inference.** This is no more
  than the exposure the trust section already accepts for a writing line that starts
  its own gate, since the code was committed by a line that could have started it
  itself. It is more where a commit's author could not start runs, such as a seat that
  does not own its role, since a reviewer can then run that author's code.
  `patterns` is how the operator bounds it, such as to `refs/remotes/origin/main`
  alone.
- **What the run can write.** A run is not sandboxed, so the commit's code may write
  anywhere the daemon may, other worktrees included. The checkout keeps the run's own
  writes out of every worktree, but a hostile commit is not contained. **Assumption.**
  The operator declares commit commands only where the committed gate is trusted as
  much as the gate a writing line runs.
- **What a reading seat can read.** **Round 14, review of 1449c5c, C4.** The command
  runs unsandboxed with the daemon's user, and its output file is written into the
  reading line's working directory. So a commit's code can print anything the
  operator's user can read, such as a credential, and a reading seat that could not
  read it itself can then read it in the output. This is the exposure an ordinary run
  gives a writing line, now given to a reader too. The fresh `home` only keeps the
  operator's settings, credentials and caches from being found by `HOME`. It is not a
  confinement, since the command can still name any path.
- **What is not shared.** The checkout has no virtual environment, so the command makes
  its own, with `uv sync --locked` in its first step. Its `HOME` is the fresh `home`,
  so uv starts with an empty cache, and a tool that keeps an interpreter or a download
  under `HOME` fetches it again. **Hypothesis, not verified.** The project's gate then
  still passes, given the network, more slowly, since the supervisor runs outside
  every sandbox and uv and pyright fetch what they need. No commit run with the fresh
  home has been run, since the daemon running when it was written held older code.
  Nothing of a line's uncommitted work reaches it.

**What a green commit run proves.** **Round 14, review of 1449c5c, C4.** Exit code 0
means that the commit's own command, such as its own `scripts/check.sh`, exited 0 in
a clean checkout of that commit, without any line's uncommitted files. It does not
show that the project's current gate passed, since the commit carries its own gate,
which can differ from the one on `main` now. It does not show that the commit is still
a ref's tip, only that it was one at the start. The output's test summary is the
commit's own output, which the commit's code writes, so it is labelled unverified as
for any run. The receipt names the commit and the checkout, so a reader sees what ran.
- **Scope and successor.** A commit run belongs to the reading line's scope as any run
  does, with the same `previous` rule, so a successor reads its record first.

**Failure modes.** A commit that is not found or not a tip is refused before
anything is made. A clone or checkout that fails removes its directory, and from round
14 records `commit_run_refused`. A daemon that dies during a run leaves the checkout, which a later
reconciliation removes once the run has ended or been cleared. A checkout left by a
crash before the run was recorded has no run, and the first check after a start
removes every entry of `commit-runs` that no open or lost run names, never following
a link, reading at most 4,096 entries at a time. A removal that fails is said once on
the daemon's standard error and tried again at the next check.

**Residual.** A slow start delays other scopes' commit run starts, which share the
commit worker, by at most the 60-second bound of each of its six git calls, and the
worker's queue of eight refuses further starts when full. A git call killed at its
bound is waited for 5 seconds, and a process that left its group by starting a
session of its own is not killed. The cooldown follows a refused start only, so a
scope may still repeat successful starts back to back, each a full gate. The checkout
is also a full working tree for the duration of the run, so disk use grows with each
open commit run, bounded by one open run per scope. **Rollback hazard.** A daemon from
before this change cannot decode `commit_run_refused`, so once one is recorded the
daemon cannot go back below it.

**Tests.** `tests/test_commitruns.py` covers the declaration and its refusals, the
event's fields together and its wire form without them, a refused name, short
identifier and uppercase identifier, an unknown commit, a commit outside `patterns`, a
checkout that holds the commit's tree and none of the uncommitted files and adds no
worktree to the repository, a failed checkout that leaves nothing, a wide or linked
base refused, a link in or at a checkout not followed, the sweep, a reading line
refused an ordinary command and allowed a commit command whose checkout is removed
after the end, the refusals before anything is made, a lost run's checkout kept until
it is cleared, a start's sweep, and the tool's `commit` argument. From the review of
1449c5c it also covers a commit a ref contains but does not point at, every pattern a
line could move, a repository without the files backend, git's environment, a filter
the operator's global configuration defines and one a configuration still read
defines, each run without the protections first as a control, a hook likewise, a git
call killed with its child at its bound, a refused start recorded with a cooldown
that outlives a restart, a second start refused while one is in progress, the
checkout made on the commit worker, and the fresh home. From the re-review of 522c146
it covers a link at `refs`, `refs/remotes`, a remote's directory, `packed-refs`, a
loose ref and `.git`, each with git shown reading through it first, a store of
another user, a store replaced during the match, and a matched ref outside
`refs/remotes`. From the final check of 951b9e7 it covers a real directory moved into
the place of `refs` and of `refs/remotes`, each accepted without the anchor first as a
control, the anchor kept through ref updates, packing and collection, a scheduler that
refuses and records a swap made after it was made, and refs unreadable when it was
made refusing every commit run until a new scheduler reads them. `tests/test_syncenv.py`
covers a planted `.venv/bin/python` never run and replaced, the environment every
step of uv receives, a worktree with no environment, a linked `.venv` moved without
following it, a failed step, an environment that is not relocatable, a `.venv` made
between the two moves, a wide or linked staging directory, and leftovers reported.
**Coverage gap.** No test runs uv itself, since uv needs the network, so its
behaviour is the experiment of 2026-11-01. No test runs Claude Code's sandbox, so the rule
against renaming `refs` is read from its code and not observed. The
real-process tests stand in for `ps` as the other run tests do. No test reaches the
network, so the large file filter's own behaviour is shown only by the review's
experiment, and the filter test stands in for it with a filter that records its run.

## Not implemented

**Deferred.** These are left out of the smallest feature, and named so they are not
mistaken for oversights.

- Arguments from the line, per-command environments and per-command working
  directories.
- A time limit on a run. A lease's expiry bounds the resource, not the process.
- Showing runs in `line_status`, `role_status` and the client's status. Only
  `run_status` shows them, and the end note reaches the holder.
- Ending a lost run's surviving command group. The operator inspects and ends it, and
  reconciliation then clears the run, or the operator clears it by hand.

## Porting notes

The supervisor is a Python process, which a port replaces with a native one. Opening one
step at a time is `openat` with `O_NOFOLLOW`. The start time is read with `ps`, whose
`lstart` field macOS and Linux share, and a port may read `/proc/PID/stat` on Linux or
`proc_pidinfo` on macOS instead, at a finer resolution.

## Tests

`tests/test_runs.py` covers the project table, the event rules, a restart that finds a
run ended while the daemon was down, a restart that finds it still running and stops
it, a handoff that makes the successor read the record first, a supervisor that died
while the daemon was down, a reused process identifier, a link in the output path,
a stop before and after the command starts, and the tools. The review of 2026-10-31
added a named pipe at the output path, which fails on 09003fa, a lost run whose
command survives and blocks until it ends, an unreadable table that keeps it
blocking, the operator's clearance through the client, a read-only seat that owns its
role, a word to go that cannot be delivered, two starts at once, a `ps` that
complains, a relative program, the declared argv in the log, and a command that
ignores `SIGTERM`. **Coverage gap.** `ps` cannot run inside an agent sandbox, so most
real-process tests stand in for the start time with whether the process answers a
signal, and for the process list with the processes the test started, while `getsid`
is real. The one test that reads the real table is skipped there, and `ps -A` is not
run by any test that runs in the sandbox. Round 14 added the test summary, its bounds,
its reading of a link or a named pipe, and the links between failed runs and their
corrections in `run_status`. **Round 15.** The descendant walk is stood in for by a
function in the sandbox, and a real-supervisor test that starts a child with `setsid`
runs only where `ps` runs, which is the supervised full gate.
