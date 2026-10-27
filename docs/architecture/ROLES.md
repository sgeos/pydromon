# Roles With Provider Seats

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

**Proposed on 2026-10-27, for the operator's review before any of it is built.** Item 4
of the iteration brief makes roles first class. A role has seats on several providers,
and one seat at a time holds write ownership. This document proposes the data model,
the two flows the brief requires, and how the existing lines map onto roles. Nothing
here is implemented. Facts about the present code are marked as such, and the rest is
proposal.

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
| `role_declared` | `role_id`, `workdir` | A role and its worktree, relative to the project root |
| `seat_bound` | `line_id`, `role_id`, `may_own` | A line becomes a seat of a role. `may_own` false makes a seat that may read and advise but never own |
| `ownership_changed` | `role_id`, `owner` or null, `previous` or null, `cause`, `by`, `entry_id` or null | Every change of owner. `cause` is `declared`, `handoff`, `recovery`, `operator` or `relinquished`. `by` is the operator or seat that made it |
| `role_noted` | `role_id`, `entry_id`, `line_id`, `kind`, `body`, `workdir_commit`, `daemon_commit` | A journal entry. `kind` is `handoff`, `recovery` or `note`. The daemon stamps both commits itself, read from `HEAD`, so the stamp is observed and not claimed |
| `budget_stated` | `provider`, `remaining_percent` or null, `resets_at` or null, `source`, `note` | A provider's budget. `source` is `operator` or `engine` |

The model's checks give these invariants. A role has at most one owner. The owner is a
seat of the role whose `may_own` is true. A line is a seat of at most one role. A
journal entry names a seat of its role. An `ownership_changed` whose `previous` is not
the current owner is refused, so two changes that race cannot both apply.

**Declared in the project file.** A role and its seats are declared in
`.dromon/dromon.toml`, which an operator owns, in the way lines are declared today, and
the daemon records the events at start. A client can also declare them, as it creates
lines. The first owner is the seat the file names as `owner`, recorded with cause
`declared`.

```toml
[roles.core]
workdir = "tmp/worktrees/core"
owner = "core"
seats = ["core", "core-codex", "core-grok"]

[lines.core-grok]
model = "grok-4.7"
may_own = false   # until an operator probe has run Grok live in Dromon
```

## What write ownership means

**Enforced through the sandbox.** A seat that owns its role runs with the role's
worktree writable. Every other seat of the role runs with it read only, through the
sandbox profile each engine adapter already builds. The scratch directory `tmp/` of the
worktree is the one exception, so a seat that does not own the role can still write its
notes with `write_scratch`. Ownership is read when a seat's turn starts. An ownership
change drops the engine of every seat whose access changed, so the next turn builds a
new session with the new profile, since a sandbox is fixed for an engine session's
life.

**An engine that cannot confine a seat.** **Inference.** Not every adapter can make one
directory read only. A seat on such an engine is not woken while another seat owns the
role. Its mail waits, as a muted line's does, until it owns the role or the operator
lets it run unconfined. This is default deny. Which adapters can confine a seat is for
the engines role to establish, and the capability table is where it is recorded.

**Never two writers.** Ownership never changes while either seat has a turn in flight.
A take while the owner runs is refused, and the operator who must take it now sends
`interrupt` first. The former owner's engine is dropped and its kept connection closed,
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

**An owner that does not hand over.** If the owner has not relinquished within an hour
of the request, the operator is asked whether to recover the role instead.

## Recovery

A seat takes ownership with `take_ownership` and a reason, or the operator gives it with
`dromon role take ROLE --seat SEAT`. A seat's own take is allowed only when the harness
itself shows that the owner cannot act, so a line cannot take a role from a working
seat by claiming otherwise.

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

**The former owner learns of it.** The change is told to the former owner at the start
of its next turn, in the preamble Dromon already gives each turn, and not by mail,
since mail would wake a seat that has no budget. The preamble names the new owner, the
cause and the time, and says the worktree is now read only to it. Its own uncommitted
changes, if any, are named in the recovery record it can read.

## Messages between seats

**Fact.** Any line may already send any line a note, so seats of a role can message
each other today. The proposal adds `role_status`, which lists the role's seats, each
seat's condition, mute, provider and budget, the owner, and the latest journal entries,
so a seat can find its peers. A note between seats is ordinary line mail, data from its
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

**Use.** `role_status` and `dromon status` show each seat's provider budget and its
source and age. The harness does not move ownership by budget alone. When an owner is
held at a plan limit, the daemon asks the operator a question with the role's other
seats as choices, ordered by stated budget, and an answer that names a seat makes the
recovery.

## Mapping the existing lines

**Proposed.** Each line that works in a role's worktree becomes that role's first seat
and owner, keeping its identity, session and mailbox.

| Role | Worktree | First seat and owner | Alternate seats, created muted |
|---|---|---|---|
| core | `tmp/worktrees/core` | `core` | `core-codex`, `core-grok` |
| engines | `tmp/worktrees/engines` | `engines` | `engines-claude`, `engines-grok` |
| review | `tmp/worktrees/review` | `review` | `review-codex`, `review-grok` |

The alternate seats are created muted, so that they spend nothing until a handoff or a
recovery unmutes them, and the Grok seats have `may_own` false until an operator probe
has run Grok live in Dromon. The review role's owner writes only its report, as today.
The other lines in this project are not mapped. The operator decides whether each
becomes a seat, stays a line without a role, or is retired. A line without a role
behaves exactly as today.

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

## Open questions for the operator

1. Whether a seat's own `take_ownership` should be allowed at all, or recovery should
   always need an operator. This proposal allows it only on harness-observed
   exhaustion.
2. Whether non-owner seats should read the worktree while the owner writes, which risks
   reading a tree in the middle of a change, or should run only between the owner's
   turns.
3. The names of alternate seats, and whether the other lines in this project become
   seats.
4. Whether the one-hour bound on a planned handoff suits you.

## Order of work, once approved

1. The events, model checks and codec, with the project file's roles, and `role_status`.
2. `relinquish_ownership`, `take_ownership`, `record_role_note`, and the client
   operations and `dromon role` commands.
3. Read-only seats in each adapter's sandbox profile, which is the engines role's work,
   and the rule for seats that cannot be confined until then.
4. `budget_stated` from operators and from plan limits, and the operator question at a
   held owner.

## Porting notes

Roles, seats, owners, journal entries and budgets are plain records in the event log,
checked by the model as lines are. Ownership is one optional field per role, changed
only by an event that names the value it replaces, so it maps onto a compare and swap
in a Rust or Keleusma state machine.
