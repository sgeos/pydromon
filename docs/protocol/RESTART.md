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
| `interrupt` | Each is asked to end at its next safe point, the point priority mail uses. The engine reports the turn and keeps its session. A hibernated line's turn is interrupted too, since the operator asked for it |

A drain is bounded by `wait_seconds`, a whole number of seconds, 600 by default and at
most 3,600. Once no turn
runs, the daemon stops as `stop` does and starts its successor. A turn still running at
the bound is one whose interrupt could not be honoured, such as a turn in one long tool
call or a turn that makes no tool call, since the safe point comes only after a tool
result or at the end of a message without a tool use, and the restart is then
abandoned. Turns start again, the line's activity says
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
autonomous toward the line's limits. A hibernated line's note waits until it is awakened.

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
| Hibernates | They are in the log as `line_hibernated` |
| Scheduled wakes | They are in the log, and one that fell due during the drain fires at the successor's first tick |
| Mail and notes | Undelivered messages start in the successor's post office |
| Holds and questions | They are in the log |
| Leases | They are in the log from 2026-10-31, each until it expires |
| Supervised runs | They are in the log from 2026-10-31, and their supervisors run in sessions of their own, so a run outlives the restart and is reconciled once the successor starts, as [`RUNS.md`](../architecture/RUNS.md) specifies |

The following do not survive, as for any stop. The counts of priority mail
against the rate limit, kept engine connections and their background tasks, and the
activity that attached clients show.

**A successor that does not start.** The old daemon writes the reason to `dromond.log`
and exits, and no daemon runs until `dromon start` succeeds. On 2026-10-31 a restart
onto new rules left the project with no daemon for about eight minutes, because the new
code refused the project for two lines that existed only in the event log, which an
offline check of the project file could not see. The preflight below exists to catch
such a refusal while the old daemon still serves.

## The preflight

**Asked for by the operator on 2026-10-31.** Before `dromon restart` sends anything that
stops the running daemon, it runs the installed code's own start checks against the
live state, and refuses the restart when they fail, printing the new code's own
message and leaving the old daemon running.

- **How.** The client reads `status`, whose `options` give the running daemon's
  command line, and runs `python -m dromon.daemon --project ROOT --runtime-dir DIR
  OPTIONS --check` on its own interpreter, which loads the code installed now, as the
  successor will. The check is `run` in `dromon.daemon` itself with `check_only`, so
  every check a start makes is a check of the preflight, in the same order, and a
  check added to a start later is covered without a second list to keep.
- **The event log is read, never written.** The check copies the log, as the next
  item describes, into a new directory, mode 0700, under the runtime directory, opens
  the copy, lets whatever a start appends go to the copy, and removes the directory
  when it ends. Each range is read in one statement, which sees committed events
  only, so a copy taken while the old daemon writes is consistent. The copy holds message bodies, so it is never
  placed outside the runtime directory.
- **A preflight can briefly delay the daemon's writes.** The log keeps SQLite's
  rollback journal, in which a reader's lock keeps a writer's commit waiting.
  SQLite's online backup starts again whenever the source is written between its
  steps, so on a busy, large log it would never finish, as audit's re-review of P1
  found. The copy is therefore made from the schema and the rows, decided by the
  operator on 2026-10-31. The schema's statements and its version are copied
  exactly. The newest sequence number is read once, and the events up to it are
  copied by sequence number in ranges of at most 2,000 events and about 1 MiB, each
  read through one cursor closed before the range is written, with a pause of 1
  millisecond and no lock after it. A body may be up to 1 MiB, so the byte bound is
  what keeps a range short. A range stops at the event that reaches it, so it reads
  at most a little over 2 MiB, and always one event. The events table is append
  only, so a range once read never changes, and an event the daemon appends during
  the copy lies above the bound and is left out, as anything recorded during the
  drain is. The copy finishes in at most as many ranges as there are events, and a
  commit of the running daemon waits at most for one range to be read. The live log
  is attached read-only, since a copy's own write transaction would otherwise take
  the live log's write lock too, and a test pins this.
- **Leftover copies are swept.** A check removes its own directory as it ends. A check
  the client kills at its bound cannot, so the client sweeps the preflight directories
  after a timeout, each check sweeps those older than the bound, and each start, which
  holds the lock, sweeps them all, decided by the operator on 2026-10-31 after audit's
  finding P2. Only directories whose names begin `preflight-` are removed, never a
  link. **Assumption.** Two clients checking at once are rare. One whose check times
  out sweeps the other's copy too, which makes the other check fail, closed.
- **The interpreter can differ.** The check runs on the interpreter `dromon` runs on,
  and the successor on the one the old daemon runs on. When the two are different
  environments, such as a client installed with pipx and a daemon in the project's
  virtual environment, the check tests other installed code than the successor loads.
  Run `dromon restart` from the environment the daemon was started from.
- **The reason.** The client prints the check's lines that begin `dromond:`, which are
  the reasons a real start prints, or else its last line, as for code that does not
  import.
- **Its bound.** The check may take 120 seconds, since it replays the whole log as a
  start does.

| The check covers | Because |
|---|---|
| Code that does not import | The check runs the installed daemon module |
| The runtime and state directories and the peers' token store | They are checked as a start checks them |
| The project file, its sandboxes, working directories, plugins, `[git_ssh]` table, engine tables and disclosure list | They are read and checked as a start reads them |
| An event log the new code refuses, such as an unknown schema or an event it cannot decode | The copy is opened and replayed by the new code |
| A file that contradicts the log, a line's model or strategy, its roles, the one-writer rule, a required sandbox a line created by a client lacks | These are checked against the copy's state, which holds the lines only the log knows |
| Applying the file's lines, roles and peers | They are applied to the copy, so an event the new code would reject is found |
| The operators' relay routes | They are checked as a start checks them before its sockets |

| Only a real start reveals | Because |
|---|---|
| The lock | The running daemon holds it, so the check does not take it |
| Sockets, the TLS listener and peer connections | The check makes none, and never removes the running daemon's |
| The open-federation registry | Registering would meet the running daemon's own entry, so the check does not write it, and the refusals of a name on the disclosure list or a registry inside the project are found only at the start |
| The fleet index | A check writes nothing of the operator's, so it neither makes the index nor writes an entry in it, and a failure to make or write it is found only at the start, which starts unlisted then, as [`FLEET.md`](../architecture/FLEET.md) gives |
| The teacher | Bringing it up loads a model, so the check does not |
| Recovery, the survivor scan and the process-table probe | They run in `serve`, or touch the process table, which the check does not |
| Engines | No engine is built until a line's first turn, at a start as at a check |
| What the drain records | The copy is taken before the request, so mail, lines or roles recorded during the drain are not checked. A line created during a drain can still make the successor refuse |
| The daemon's environment | The check runs with the client's environment, and the successor with the old daemon's, so a refusal that depends on `SSH_AUTH_SOCK` or `DROMON_REGISTRY_DIR` can differ |
| An installed checkout that moves after the check | The successor loads what is installed when it starts |
| Another interpreter's installed code | The check runs on the client's interpreter, and the successor on the old daemon's |

**Compatibility.** A daemon from before 2026-10-31 gives no `options`, and the check
then runs with none, which can differ from the successor's command line only in the
limits, operator, tick and held wakes, all of which the check validates the same way.

## `dromon restart`

The command reads the daemon's `pid` and commit, runs the preflight, sends `restart`,
and polls `status` four times a second until a daemon with another `pid` answers. It then prints the successor's commit and the commit before, and says when
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
spawn, or leaves the start to a supervisor where the platform has one. The preflight is
the start path with a flag that swaps the log for a copy and stops before the first
side effect outside the copy, so a port keeps one start path, not two. The copy is the
schema and the events below a sequence bound read once, which a port with any store can
make.
