# Roles With Provider Seats

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

**Approved by the operator on 2026-10-27, with the decisions recorded below.** Item 4
of the iteration brief makes roles first class. A role has seats on several providers,
and one seat at a time holds write ownership. This document gives the data model, the
two flows the brief requires, and how the existing lines map onto roles. The events,
their model checks and codec, the `role_status` tool, the project file's roles
recorded at start and on reload, the seat tools `record_role_note`,
`relinquish_ownership` and `take_ownership`, and the client operations with the
`dromon role` and `dromon budget` commands are implemented, as is the rule that a seat
whose engine cannot confine it waits while another seat owns its role. The read-only
sandbox itself, the question at an owner that does not hand over and budgets from plan
limits are not yet built. Facts about the present code are marked as such.

## Terms

| Term | Meaning |
|---|---|
| Role | A named line of work with one worktree, such as `core`. It outlives every seat that works on it |
| Seat | A session line bound to one role, on one engine and model. A role has one or more seats |
| Owner | The one seat of a role that may write the role's worktree. A role may also be vacant, with no owner |
| Handoff | A planned passing of ownership. The owner leaves the work in order, writes a handoff note and relinquishes |
| Recovery | An unplanned passing of ownership. Another seat takes it while the owner cannot act, and records what it found |
| Role journal | The role's recorded handoff notes and recovery records, which every seat can read |
| Provider budget | What remains of one provider's plan and when it resets, as an operator states it or an engine reports it |

**Fact.** Dromon already has a context handoff, `context_handed_off`, which carries a
line's own context across a cleared or compacted session. It is unrelated. Where both
could be meant, this document says ownership handoff.

## The data model

A seat is an ordinary line. It keeps its own engine session, mailbox, limits, cost,
mute and hold, so everything the daemon does for a line it does for a seat. What a role
adds is a binding, one owner and a journal, all of it in the event log, so it survives
a restart and a port carries it unchanged.

| Event | Fields | Records |
|---|---|---|
| `role_declared` | `role_id`, `workdir`, `handoff_seconds` | A role, its worktree relative to the project root, and how long a planned handoff may take, an hour by default |
| `seat_bound` | `line_id`, `role_id`, `may_own` | A line becomes a seat of a role. `may_own` false makes a seat that may read and advise but never own |
| `ownership_changed` | `role_id`, `owner` or null, `previous` or null, `cause`, `by`, `entry_id` or null, `changed_at`, `back_to` or null | Every change of owner. `cause` is `declared`, `handoff`, `recovery`, `operator` or `relinquished`. `by` is the operator or seat that made it. `back_to` is a handoff's seat to return to |
| `role_noted` | `role_id`, `entry_id`, `line_id`, `kind`, `body`, `workdir_commit`, `daemon_commit`, `noted_at` | A journal entry. `kind` is `handoff`, `recovery` or `note`. The daemon stamps both commits itself, read from `HEAD`, so the stamp is observed and not claimed |
| `budget_stated` | `provider`, `remaining_percent` or null, `resets_at` or null, `source`, `note`, `stated_at` | A provider's budget. `source` is `operator` or `engine` |

The model's checks give these invariants. A role has at most one owner. The owner is a
seat of the role whose `may_own` is true, and is not stopped. A line is a seat of at
most one role. A journal entry names a seat of its role, and a handoff or recovery
entry is by the current owner. An `ownership_changed` whose `previous` is not the
current owner is refused, so two changes that race cannot both apply. A handoff rests
on the role's latest entry, a handoff entry by the previous owner. A role's worktree
changes only while it is vacant. The model never reads the log's `recorded_at`, so the
events whose time matters carry it. The normative rules are in
[`EVENT_LOG.md`](../protocol/EVENT_LOG.md).

**Declared in the project file.** A role and its seats are declared in
`.dromon/dromon.toml`, which an operator owns, in the way lines are declared today, and
the daemon records the events at start. A client can also declare them, as it creates
lines. The first owner is the seat the file names as `owner`, recorded with cause
`declared`.

```toml
[roles.core]
workdir = "tmp/worktrees/core"
owner = "core"
seats = ["core", "core-luna", "core-grok"]
handoff_seconds = 3600   # the default

[lines.core-grok]
model = "grok-4.7"
may_own = false   # until an operator probe has run Grok live in Dromon
```

## What write ownership means

**Enforced through the sandbox.** A seat that owns its role runs with the role's
worktree writable. Every other seat of the role runs with it read only, through the
sandbox profile each engine adapter already builds. **Decided on 2026-10-27.** A seat
that does not own the role may read the worktree at any time, also while the owner
writes, so it can prepare a handoff, and it writes only the worktree's scratch
directory `tmp/`, with `write_scratch` or its own tools. What it reads in the middle of
the owner's change may be inconsistent, which the recovery checklist accounts for. Ownership is read when a seat's turn starts. An ownership
change drops the engine of every seat whose access changed, so the next turn builds a
new session with the new profile, since a sandbox is fixed for an engine session's
life.

**An engine that cannot confine a seat.** **Inference.** Not every adapter can make one
directory read only. A seat on such an engine is not woken while another seat owns the
role. Its mail waits, as a muted line's does, until it owns the role or the operator
lets it run unconfined. This is default deny. Which adapters can confine a seat is for
the engines role to establish, and the capability table is where it is recorded.
**Implemented,** except the operator's leave to run unconfined, which is not built.
**Fact.** The engines role's read-only seats are on a branch not yet merged, so the
daemon counts no engine as able to confine a seat, and a seat runs only while it owns
its role or the role is vacant. A seat of a vacant role runs so that it can take the
role. Once the branch merges, the engines whose capability table entry says they can
make the worktree read only are counted, and their seats are built with read access.

**Never two writers.** A take while the owner runs is refused, and the operator who
must take it now sends `interrupt` first. **Implemented.** A seat's own tools change
ownership during its own turn, so the rule as built is that the change is recorded at
once, every turn goes on with the engine it started with, and no seat's turn as the
owner starts while another seat's turn that started as the owner is in flight, or
while that seat's kept connection is still closing. A seat that does not own the role
runs beside the owner where its engine can confine it. The former owner's engine is dropped and its kept connection closed,
by the line's stop policy, before the new owner's first turn starts. Processes the
former owner left are recorded as `processes_left` records them today. **Fact.** On macOS the
daemon cannot identify a line's processes, as `line_status` says, so a process that
outlived its engine cannot be ruled out there, and the new owner's first turn is told
so.

**Not the git gate.** Ownership governs who changes files. Committing, pushing and
merging stay with the operator, as `.dromon/LINES.md` says for this project.

## Planned handoff

The operator asks with `dromon role handoff ROLE --to SEAT`, or in words to the owner.

1. The daemon sends the owner a priority note from the operator, which ends its turn
   at a safe point, asking it to prepare the role for SEAT.
2. The owner leaves the work in order. It runs the gate, names its uncommitted changes
   and mail in flight, and calls `relinquish_ownership` with a handoff note and the
   successor's identifier.
3. The daemon records the note as a `role_noted` entry of kind `handoff`, stamped with
   both commits, then `ownership_changed` with cause `handoff`. It unmutes the
   successor if it was muted, and sends it a note that wakes it with the entry's
   identifier.
4. The successor reads the entry with `role_status`, checks it as the next section
   checks a recovery, and continues.

The handoff note follows one template, so that a seat on any provider writes and reads
the same seam. It gives the commit it describes, `git status`, each uncommitted change
and whose it is, the gate run and its coverage, mail sent and not answered, open items,
the checks not performed, and the next step.

**Short alternation.** `relinquish_ownership` takes an optional `back_to`. When the
successor next relinquishes, ownership returns to that seat with cause `handoff` and no
operator step, so two seats can pass a role back and forth for a few tasks. The seat
list has no order, and any seat with `may_own` can receive a role.

**An owner that does not hand over.** If the owner has not relinquished within the
role's `handoff_seconds` of the request, an hour unless the role declares otherwise, the
operator is asked whether to recover the role instead. **Not yet built.** The request
is ordinary priority mail, and the daemon keeps no record of it that a restart would
keep, so the question waits for an event that records a handoff request.

**Implemented.** `dromon role handoff` checks that the successor is a live seat that
may own the role, then sends the request with the handoff note's template. An owner
that is muted reads it once unmuted, and the command says so.

## Recovery

A seat takes ownership with `take_ownership` and a reason, or the operator gives it with
`dromon role take ROLE --seat SEAT`. **Decided on 2026-10-27.** A seat's own take is
allowed, since ownership must pass when the owner's provider is out of tokens, but
only when the harness itself shows that the owner cannot act, by the first three rows
below, so a line cannot take a role from a working seat by claiming otherwise. An
operator can always take.

**Implemented.** `dromon role take ROLE --seat SEAT` gives the role and `dromon role
vacate ROLE` leaves it vacant, each with cause `operator`. Either is refused while the
owner has a turn in flight, and says to interrupt it first. A seat given the role is
unmuted and sent a note from the operator, which wakes it once no former writer's turn
is in flight, and its turn leads with the recovery checklist. An operator's take has no
journal entry, since the model takes entries only from seats.

| Owner's state | A seat may take | The operator may take |
|---|---|---|
| Vacant, after a relinquish without a successor | Yes | Yes |
| Held at a plan limit its engine reported | Yes | Yes |
| Its provider's budget recorded as 0 percent | Yes | Yes |
| Failed, or held at a conversation limit | No | Yes |
| Idle with budget, or running | No | Yes, once the owner has no turn in flight |

The new owner's first turn leads with a recovery checklist. It reads `git status` and
the commits since the last journal entry. It checks that entry's stamp against `HEAD`
by ancestry and by content, since a stamp can stay an ancestor while its content goes
stale. It records uncommitted work and any work-in-progress commit or stash the former
owner left, without adopting it as its own. It reads the role's recent mail in and
out. Then it calls `record_role_note` with kind `recovery` before it changes anything,
so a further recovery can read what it found.

**Implemented.** A seat's own recovery rests on a note entry that records its reason
and the ground the harness observed. A plan-limit hold is known as such only in the
daemon run that saw it, so after a restart only a provider budget recorded as 0 percent
shows that a held owner cannot act, until the budget step records one at each plan
limit. A budget of 0 percent whose reset time has passed is no ground.

**The former owner learns of it.** The change is told to the former owner at the start
of its next turn, in the preamble Dromon already gives each turn, and not by mail,
since mail would wake a seat that has no budget. The preamble names the new owner, the
cause and the time, and says the worktree is now read only to it. Its own uncommitted
changes, if any, are named in the recovery record it can read.

## Messages between seats

**Fact.** Any line may already send any line a note, so seats of a role can message
each other today. **Implemented.** `role_status`, a tool offered only to a seat,
lists the role's worktree and handoff bound, the owner and the latest change of owner,
each seat's condition, mute, model, provider, stated budget and worktree commit, and
the role's latest five journal entries, the newest with its body, so a seat can find
its peers. Until the budget step, each engine counts as its own provider. A note between seats is ordinary line mail, data from its
sender like any other, and counts toward the autonomous limits as mail between lines
does. No role-wide broadcast is proposed, because a broadcast multiplies turns across
providers whose budgets are short.

## Provider budget

A provider is the account behind an engine, such as the Claude plan, the Codex plan or
the Grok plan. An engine maps to one provider, and Pi's provider is read from its line
configuration.

**Sources.** An operator states a budget with `dromon budget set PROVIDER` and a
remaining percentage, a reset time or both. An engine reports one when it stops at a
plan limit. **Fact.** The Claude Code and Codex adapters already raise a plan limit,
and Codex sometimes reports a reset time with it. That becomes `budget_stated` with
`remaining_percent` 0, `source` `engine`, and the reset time where one was given. A
stated budget is not adjusted by the cost of later turns, since a per-turn cost does
not convert reliably to a plan's percentage, so a statement is a snapshot with its time.
**Implemented** for an operator. `dromon budget set` accepts only an engine's name as
the provider until the budget step, since a statement for any other name would ground
nothing, and `dromon budget` and `dromon role status` show each statement with its
source and time. The engine's report at a plan limit is not built yet.

**Use.** `role_status` and `dromon status` show each seat's provider budget and its
source and age. The harness does not move ownership by budget alone. When an owner is
held at a plan limit, the daemon asks the operator a question with the role's other
seats as choices, ordered by stated budget, and an answer that names a seat makes the
recovery.

## Mapping the existing lines

**Decided on 2026-10-27.** The primary seats stay `core`, `engines` and `review`, each
its role's first seat and owner, keeping its identity, session and mailbox. An
alternate seat is named for its role and its engine.

| Role | Worktree | First seat and owner | Alternate seats, created muted |
|---|---|---|---|
| core | `tmp/worktrees/core` | `core` | `core-luna`, `core-grok` |
| engines | `tmp/worktrees/engines` | `engines` | `engines-opus`, `engines-grok` |
| review | `tmp/worktrees/review` | `review` | `review-luna`, `review-grok` |

The alternate seats are created muted, so that they spend nothing until a handoff or a
recovery unmutes them, and the Grok seats have `may_own` false until an operator probe
has run Grok live in Dromon. The review role's owner writes only its report, as today.
The lines `worker`, `reviewer` and `luna` are retired and are no longer declared. A
line without a role behaves exactly as today.

## Failure modes

- **Two writers.** Prevented by the sandbox where an engine can confine a seat, by not
  waking a seat that cannot be confined, and by never changing owner during a turn. A
  process that outlived its engine on a platform that cannot see it remains possible,
  and is said to the new owner.
- **A stale handoff.** The stamp is observed, but its content can be out of date. The
  checklist requires content checks as well as ancestry.
- **An inherited dirty tree.** The successor records what it found and does not adopt
  it. Who made a change cannot always be established.
- **A seat on an unverified provider.** `may_own` false keeps it from owning until an
  operator sets it.
- **A seat that loses a race.** Two takes at once cannot both apply, because each names
  the owner it replaces.
- **Approximate budget.** A budget is a snapshot from an operator or a plan-limit
  report, and is shown with its source and age.

## Decisions

The operator answered the four open questions on 2026-10-27, and may overrule any of
them.

1. A seat may take ownership itself when the harness shows the owner cannot act, by the
   three conditions of the recovery table. An operator can always take.
2. A seat that does not own its role may read the worktree at any time, read only, and
   writes only `tmp/`.
3. The primary seats are `core`, `engines` and `review`, and the alternates are named
   for role and engine, muted at creation, with the Grok seats unable to own until the
   probe. `worker`, `reviewer` and `luna` are retired.
4. The handoff bound is one hour, configurable per role as `handoff_seconds`.

## Order of work

1. The events, model checks and codec, and `role_status`, then the project file's
   roles, recorded at the daemon's start and on reload. Both are done.
2. `relinquish_ownership`, `take_ownership` and `record_role_note`, then the client
   operations and the `dromon role` and `dromon budget` commands. All are done.
3. Read-only seats in each adapter's sandbox profile, which is the engines role's work,
   and the rule for seats that cannot be confined until then. The rule is done, and the
   profiles wait for the engines role's branch to merge.
4. `budget_stated` from operators and from plan limits, and the operator question at a
   held owner.

## Porting notes

Roles, seats, owners, journal entries and budgets are plain records in the event log,
checked by the model as lines are. Ownership is one optional field per role, changed
only by an event that names the value it replaces, so it maps onto a compare and swap
in a Rust or Keleusma state machine.
