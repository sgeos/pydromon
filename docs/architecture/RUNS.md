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
- **Who may start or stop.** A seat may start or stop its role's runs only while it
  owns the role, so the one-writer rule holds for what a run writes. Every line, a
  seat that owns its role included, also needs `write` access to its working
  directory, since a run writes there outside the line's sandbox. A line with `read`
  access may read a run's record but not start or stop one. **Review of 2026-10-31,
  R3.** A seat that owned its role was once let through without this check, so a
  read-only seat could write its worktree by a run.
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

## Tools

| Tool | Effect |
|---|---|
| `start_run` | Arguments `command` and `previous`. Starts the declared command, and answers with the run's identifier and output file |
| `run_status` | No arguments. The scope's latest ten runs, newest first, with what `previous` must be and the declared commands |
| `stop_run` | Argument `run_id`, the scope's open run. Signals its supervisor after checking its start time again |

They are offered while the project declares a run command, or while the line's scope
has a run to read. The operator's `dromon run clear RUN` is the only way to clear a
lost run by hand, and is offered to no line or external agent.

## Events and rollback hazards

`run_started`, `run_ended` and `run_cleared` are specified in
[`EVENT_LOG.md`](../protocol/EVENT_LOG.md), as are `lease_acquired` and
`lease_released`, which the same task added. **Rollback hazard.** A daemon older than
2026-10-31 does not know these kinds, so once any is recorded it cannot read the log.
A supervisor started by a newer daemon also keeps running under an older one, which
does not know of it, and its receipt waits unread in the report directory. A daemon
of 09003fa, before the review, reads no `run_cleared` and lets a lost run's scope
start another run.

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
  The supervisor ends its group only on a stop.
- **A receipt that is malformed** counts as none, so its run ends as `lost` once the
  supervisor is gone.
- **The report directory** is the daemon's state, which no line can write. A daemon
  without one refuses every run.
- **A daemon in a sandbox that denies `ps`,** as an agent's sandbox does, cannot
  identify a supervisor, so every `start_run` is refused and nothing starts.
- **An output larger than 1 GiB** is recorded with its size and without a digest.

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
run by any test that runs in the sandbox.
