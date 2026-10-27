# Restart to Upgrade

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Decided by the operator on 2026-10-27.** It is always possible to close and reopen the
harness so that every line's session migrates to the latest installed version. Running
turns finish first or are interrupted, at the operator's choice. Sessions resume
afterwards. The request is specified in [`CLIENT.md`](CLIENT.md), the event in
[`EVENT_LOG.md`](EVENT_LOG.md), and the safe point in [`PRIORITY.md`](PRIORITY.md). This
document gives the behaviour.

## The drain

A `restart` request begins a drain. From the request on, no turn of any line starts and
no scheduled wake fires. Mail, task notes and wakes that would start a turn wait in the
event log for the restarted daemon. Requests from clients and the tools of running
turns keep working.

| Mode | Running turns |
|---|---|
| `wait`, the default | Each ends by itself |
| `interrupt` | Each is asked to end at its next safe point, the point priority mail uses. The engine reports the turn and keeps its session. A muted line's turn is interrupted too, since the operator asked for it |

A drain is bounded by `wait_seconds`, 600 by default and at most 3,600. Once no turn
runs, the daemon stops as `stop` does and starts its successor. A turn still running at
the bound is one whose interrupt could not be honoured, such as a turn in one long tool
call, and the restart is then abandoned. Turns start again, the line's activity says
so, and the daemon's `status` gives the reason under `restart`. No turn is cut part
way. An operator who must end turns at once sends `stop` and then starts the daemon.

**Priority mail during a drain.** A priority note takes the safe point first, as it
would without a restart, and waits for the successor with the rest of the line's mail.

## Background tasks

Stopping the daemon closes every kept engine connection, which stops the background
tasks running in it. A restart is refused, before it drains and recording nothing, while
any kept session runs background tasks, unless the request gives `end_tasks`. A drain
without `end_tasks` is abandoned if such tasks have begun by the time no turn runs.

## The note that a restart ended a turn

A turn that ended at a safe point for the restart leaves the line a `restart_noted`
note, from the line to itself, as the turn ends. The successor delivers it like any
note, so the line is woken and its work continues in its resumed session. Its header in
the prompt and in `list_mail` says `a daemon restart ended your last turn at a safe
point`, and the body names the operator who asked. The turn it starts counts as
autonomous toward the line's limits. A muted line's note waits for its unmute.

A turn asked to end whose restart was then abandoned, before the turn's report arrived,
leaves a note that says the restart was abandoned, which wakes the line in the same
daemon after its cooldown.

A turn that ended by itself during a drain leaves no note, since nothing was cut.

## The successor

The draining daemon starts its successor itself, once it has closed its sockets and
released its lock, so the successor never finds the lock held and a client that leaves
early does not stop the restart. The successor runs `python -m dromon.daemon` on the
interpreter the daemon runs on, which loads the code installed now, with the project,
the runtime directory and every option the daemon's own command line gave. It reads
the event log as any start does, so the following survive.

| Kept across a restart | Because |
|---|---|
| Each line's engine session | It is in the log as `line_session`, and the next turn resumes it |
| Mutes | They are in the log as `line_muted` |
| Scheduled wakes | They are in the log, and one that fell due during the drain fires at the successor's first tick |
| Mail and notes | Undelivered messages start in the successor's post office |
| Holds and questions | They are in the log |

The following do not survive, as for any stop. Leases, the counts of priority mail
against the rate limit, kept engine connections and their background tasks, and the
activity that attached clients show.

**A successor that does not start.** The old daemon writes the reason to `dromond.log`
and exits, and no daemon runs until `dromon start` succeeds. **Inference.** The usual
cause is installed code that does not import, which `dromon restart` checks before it
sends the request, by running the installed daemon with `--version`. A failure that
appears only after the import, such as an event log the new code refuses, is not
caught by that check.

## `dromon restart`

The command checks the installed code, reads the daemon's `pid` and commit, sends
`restart`, and polls `status` four times a second until a daemon with another `pid`
answers. It then prints the successor's commit and the commit before, and says when
they are the same. It fails when the daemon refuses the request, when the restart is
abandoned, and when no successor answers within the wait and 70 seconds more, which
covers the shutdown and the successor's start.

## Failure modes

- **A crash during the drain.** The drain is held only in the daemon's memory. A crash
  leaves no restart pending, and the next start recovers lines left running, as after
  any crash.
- **A crash between the stop and the successor's start.** No daemon runs, and the event
  log is as a stop leaves it.
- **The installed code is the code that runs.** A successor runs whatever is installed
  when it starts, which can differ from what the operator reviewed if the checkout moves
  during the drain. `line_status` and `status` name the successor's commit, so a report
  can cite it.

## Porting notes

The drain is one optional record with a deadline on a monotonic clock, and the daemon's
watch is a poll bounded by a count derived from the wait. Starting the successor is a
detached spawn after the lock is released. A Rust or Keleusma daemon makes the same
spawn, or leaves the start to a supervisor where the platform has one.
