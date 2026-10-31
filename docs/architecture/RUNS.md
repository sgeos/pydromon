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
run's identifier.

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
  owns the role, so the one-writer rule holds for what a run writes. Any other line
  needs `write` access to its working directory. A line with `read` access may read a
  run's record but not start one.
- **The output path.** The output file is under the working directory, which a line can
  write, while the daemon writes it outside any sandbox. The daemon therefore opens
  `tmp`, `tmp/runs` and the file one step at a time, each relative to the one before,
  with `O_NOFOLLOW`, and creates the file exclusively. A symbolic link put in the way
  is refused, and no swap after a check can redirect the open. The digest is read the
  same way.

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
   no exit code.

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
   daemon's standard error and the next reconciliation tries again.

Each end records `run_ended` with the digest and size of the output file, read when the
end is recorded, then removes the receipt, and sends a note from the harness to the
scope's present holder, the role's owner or else the line that started it.

## The scope and the successor

A seat's run belongs to its role, and any other line's run to the line. A scope has at
most one open run.

**A successor must read the record first.** `start_run` takes `previous`, the scope's
latest run as `run_status` names it, or `none` when there is none. The event log
refuses a `run_started` whose `previous` is not the scope's latest run, and one while
the latest is open. A seat that gains the role therefore cannot start a run without
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
has a run to read.

## Events and rollback hazards

`run_started` and `run_ended` are specified in
[`EVENT_LOG.md`](../protocol/EVENT_LOG.md), as are `lease_acquired` and
`lease_released`, which the same task added. **Rollback hazard.** A daemon older than
2026-10-31 does not know these kinds, so once any is recorded it cannot read the log.
A supervisor started by a newer daemon also keeps running under an older one, which
does not know of it, and its receipt waits unread in the report directory.

## Failure modes

- **A supervisor killed with `SIGKILL`,** or a machine restart, leaves no receipt. The
  run ends as `lost`, and its command may still run, in a group of its own. The note
  says so. Dromon does not end that group, since the group identifier could already be
  reused.
- **A daemon that dies after recording a run and before the word to go** leaves a
  supervisor whose input closes, so it exits without starting the command and without
  a receipt. The run then ends as `lost` with the note above, although its command
  never ran. **Inference.** The window is one write to a pipe, so this is rare, and the
  record errs toward saying less is known.
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
- Ending a lost run's surviving command group.

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
a stop before and after the command starts, and the tools. **Coverage gap.** `ps` cannot
run inside an agent sandbox, so most real-process tests stand in for the start time
with whether the process answers a signal, and the one test that reads the real table
is skipped there.
