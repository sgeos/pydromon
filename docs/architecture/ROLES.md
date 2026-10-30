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
whose engine cannot confine it waits while another seat owns its role. Automatic
switching, which is the failover policy, the planned handoff by budget and the budgets
engines report at a plan limit, is implemented as step 3, amended by the operator on
2026-10-27. The read-only sandbox itself and the question at an owner that does not
hand over are not yet built. Facts about the present code are marked as such.

## Terms

| Term | Meaning |
|---|---|
| Role | A named line of work with one worktree, such as `core`. It outlives every seat that works on it |
| Seat | A session line bound to one role, on one engine and model. A role has one or more seats |
| Owner | The one seat of a role that may write the role's worktree. A role may also be vacant, with no owner |
| Handoff | A planned passing of ownership. The owner leaves the work in order, writes a handoff note and relinquishes |
| Recovery | An unplanned passing of ownership. Another seat takes it while the owner cannot act, and records what it found |
| Role journal | The role's handoff notes, recovery records and findings from any seat, which every seat can read |
| Provider budget | What remains of one provider's plan and when it resets, as an operator states it or an engine reports it |

**Fact.** Dromon already has a context handoff, `context_handed_off`, which carries a
line's own context across a cleared or compacted session. It is unrelated. Where both
could be meant, this document says ownership handoff.

## The data model

A seat is an ordinary line. It keeps its own engine session, mailbox, limits, cost,
hibernate and hold, so everything the daemon does for a line it does for a seat. What a role
adds is a binding, one owner and a journal, all of it in the event log, so it survives
a restart and a port carries it unchanged.

| Event | Fields | Records |
|---|---|---|
| `role_declared` | `role_id`, `workdir`, `handoff_seconds`, `seats`, `failover`, `handoff_below_percent` | A role, its worktree relative to the project root, how long a planned handoff may take, an hour by default, its seats in the order a failover follows, its failover policy, and the budget below which its owner is asked to prepare a handoff |
| `seat_bound` | `line_id`, `role_id`, `may_own` | A line becomes a seat of a role. `may_own` false makes a seat that may read and advise but never own |
| `ownership_changed` | `role_id`, `owner` or null, `previous` or null, `cause`, `by`, `entry_id` or null, `changed_at`, `back_to` or null | Every change of owner. `cause` is `declared`, `handoff`, `recovery`, `operator`, `relinquished` or `failover`. `by` is the operator or seat that made it. `back_to` is a handoff's seat to return to |
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
seats = ["core", "core-luna", "core-grok"]   # the order a failover follows
handoff_seconds = 3600   # the default
failover = "automatic"   # the default, or "ask"
handoff_below_percent = 10   # the default, or 0 for never

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
role. Its mail waits, as a hibernated line's does, until it owns the role or the operator
lets it run unconfined. This is default deny. Which adapters can confine a seat is for
the engines role to establish, and the capability table is where it is recorded.
**Implemented,** except the operator's leave to run unconfined, which is not built.
**Fact.** The engines role's read-only seats are on a branch not yet merged, so the
daemon counts no engine as able to confine a seat, and a seat runs only while it owns
its role or the role is vacant. A seat of a vacant role runs so that it can take the
role. Once the branch merges, the engines whose capability table entry says they can
make the worktree read only are counted, and their seats are built with read access.
**Decided by the operator on 2026-10-27.** The merge derives the set from the
capability table's entries that give `read_only_workdir` as yes, and does not type it
in by hand, so the set cannot fall out of step with the table. Grok's entry is
unknown, so Grok stays out of the set until an operator probe confirms it. Until the
merge the set stays empty, which `roles.CONFINING_ENGINES` records.

**Never two writers.** A take while the owner runs is refused, and the operator who
must take it now sends `interrupt` first. **Decided by the operator on 2026-10-27.** An
owner's `relinquish_ownership` inside its own turn is the planned handoff, and is
kept, but the change takes effect only when that turn ends. The call is checked at
once, so a refusal reaches the owner, and is made again when the turn ends, however it
ends, with the handoff entry stamped with the commit the turn left. The former owner's
engine is then dropped, so the turn keeps no write access after it. Should the change
no longer apply by then, the owner keeps the role and the operator is told. A daemon
that stops during the turn loses the request, and the owner keeps the role.
**Implemented.** A seat's `take_ownership` inside its own turn is recorded at once, but
that turn goes on with the read access it started with, and no seat's turn as the
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
   both commits, then `ownership_changed` with cause `handoff`, when the owner's turn
   ends. It awakens the
   successor if it was hibernated, and sends it a note that wakes it with the entry's
   identifier.
4. The successor reads the entry with `role_status`, checks it as the next section
   checks a recovery, and continues.

The handoff note follows one template, so that a seat on any provider writes and reads
the same seam. It names the task, commit it describes, each uncommitted change and its
owner, the gate result and its coverage, unresolved decisions, mail sent and not
answered, checks not performed, and the next action. Use labeled lines for `Task`,
`Diff summary`, `Gate and coverage`, `Unresolved decisions`, and `Next action`. The
successor's first turn leads with these journal fields, a current worktree status and
a diff summary since the journal entry's commit. It marks fields that the entry does
not provide as unknown. This package is a starting point for verification, since the
worktree may have moved since the entry was recorded.

Any seat, including one that may not own the role, can record a journal note with
`record_role_note`. A reviewer can use it to record findings and the daemon stamps the
commit read. Journal prose is untrusted and is presented as data. A note does not
change ownership.

**Short alternation.** `relinquish_ownership` takes an optional `back_to`. When the
successor next relinquishes, ownership returns to that seat with cause `handoff` and no
operator step, so two seats can pass a role back and forth for a few tasks. Any seat
with `may_own` can receive a role by a handoff, whatever its place in the order.

**A handoff asked for by budget.** **Implemented, at the operator's instruction of
2026-10-27.** When a provider's budget is stated above 0 and below a role's
`handoff_below_percent`, 10 percent unless the role declares otherwise, and the role's
owner runs on that provider, the daemon asks the owner to prepare a handoff to the seat
a failover would choose, with the handoff note's template. The request is a note from
the harness, `!dromon`, from 2026-10-28, which claims no operator's authority, and
before that date was a note from the owner's own address. It is not priority mail, so the owner's work
in flight is not cut short. With no usable seat the owner is not asked, and the
statement's output says so. A threshold of 0 never asks.

**An owner that does not hand over.** If the owner has not relinquished within the
role's `handoff_seconds` of the request, an hour unless the role declares otherwise, the
operator is asked whether to recover the role instead. **Not yet built.** The request
is ordinary priority mail, and the daemon keeps no record of it that a restart would
keep, so the question waits for an event that records a handoff request.

**Implemented.** `dromon role handoff` checks that the successor is a live seat that
may own the role, then sends the request with the handoff note's template. An owner
that is hibernated reads it once awakened, and the command says so.

## Automatic failover

**Decided by the operator on 2026-10-27, implemented as step 3.** Each role has a
failover policy, `automatic` by default or `ask`. When the owner cannot act, by the
first three rows of the recovery table below, the harness itself applies the policy.
It looks when one of the owner's turns ends, which covers a turn held at a plan limit,
and when an operator states a budget for the owner's provider. It never moves a role
while the owner's turn is in flight, and the statement's output says the role fails
over when that turn ends.

Under `automatic` the role passes to the next usable seat in the declared order, from
the owner round to the front. A seat is usable when it may own the role, is not
stopped, hibernated, held or failed, and its provider's stated budget does not show it
spent. **Decided by the operator on 2026-10-27.** A seat with a turn in flight is
usable. It becomes the owner at once, but that turn goes on with the read access it
started with, and the seat gains write access only from its next turn, which is
built as the owner. The change is recorded with cause `failover` and rests on a note entry that says
the harness wrote it, why, and from whom to whom. The new owner is woken by a note from
the harness, and its turn leads with the recovery checklist. The operator who last wrote
to the former owner is told in a note from the harness. **Decided by the operator on
2026-10-28.** These notes, and a request to prepare a handoff, were recorded as from a
line that did not write them until the log gained the harness's sender address
`!dromon`, as [`EVENT_LOG.md`](../protocol/EVENT_LOG.md) specifies. **Decided by the
operator on 2026-10-28.** The questions that a plan-limit hold, a ceiling and a consent
ask stay questions from the line they concern, since their bodies say that Dromon asked
them and the operator's answer is routed back to that line. Asking them from the harness
is deferred, because it would change the rule that a hold's question comes from the held
line and the path by which an answer reaches the line. With no usable seat nothing changes, and the operator is told once until
what it would be told changes.

Under `ask` no owner changes. A plan limit holds the owner and asks the operator as it
always has, and `dromon role take` gives the role to another seat.

**Fact.** A hibernated seat is not usable, at the operator's instruction, so an alternate
that is hibernated at creation is passed over until an operator awakens it. Its awakening
does not by itself fail the role over. The next statement of the owner's budget, or the
end of the owner's next turn, does.

**What counts as unable to act.** The plan-limit hold of the recovery table, with the
budget an engine reports, and a budget stated at 0 percent whose reset has not passed.
Each engine's out-of-credit errors are classified only where their shape is on
record, as the next section says. **Decided by the operator on 2026-10-27.** Only a
shape observed live, or one the Agent SDK types and documents, may move a role. A
limit read from Codex's inferred codes or free-text phrases holds the line and asks,
records no budget, and grounds neither a failover nor a seat's own take, so the
operator moves the role, if at all. A turn that fails in any other way holds the line
as before, and is not a ground.

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
awakened and sent a note from the operator, which wakes it once no former writer's turn
is in flight, and its turn leads with the recovery checklist. An operator's take has no
journal entry, since the model takes entries only from seats.

| Owner's state | A seat may take | The operator may take |
|---|---|---|
| Vacant, after a relinquish without a successor | Yes | Yes |
| Held at a plan limit its engine reported, from an observed or typed shape | Yes | Yes |
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
changes, if any, are named in the recovery record it can read. Each affected seat is
told once for the change. The event log records whether that seat ended a turn after
the change, so replay preserves the notice state across a daemon restart. If no turn
ended, the next turn repeats the notice until one ends. A later ownership change
starts a new notice for each affected seat.

### The role transition package

**Implemented on 2026-10-29.** The first turn of a new owner by any cause but the
declared one leads with a role transition package. Before this, the package read Task,
Gate and coverage, Unresolved decisions and Next action only from the role's latest
journal entry. **Fact.** A failover takes a role with no handoff note, so in the live
failover of 2026-10-28 the new owner found every field missing, could not tell an
absent path from an unreadable one, and was not told that the former owner's process
state was unknown.

Each item of the package now says where it came from, in one of four ways.

| Marking | Meaning |
|---|---|
| Observed by the daemon, with the time | The daemon read it as it composed the package, as the worktree status and the diff summary are |
| Reported by a line, with the line, the source and the time | A line wrote it, in a journal entry or in its standing brief, and the daemon did not check it |
| Unknown | No source states it |
| Could not be read by the daemon, with the time | The daemon tried and failed, which says nothing of what is there |

A labeled field the journal entry does not state is taken from the former owner's
standing brief, which `set_brief` records, as [`LINE_CONFIG.md`](../protocol/LINE_CONFIG.md)
specifies. Task, Unresolved decisions and Next action have a counterpart in the brief,
and the brief's artifact paths are an item of their own. The former owner is the seat
the role passed from, or, after a vacancy, the author of the entry.

**Fields the daemon observed, task of 2026-10-29.** Where the journal entry and the
brief are both silent, two fields are filled from the daemon's own records, each marked
observed by the daemon. Gate and coverage is the former owner's latest task record, with
its command, final status, exit code, output digest and size, and commit, and the time
the daemon learned it ended. Task is the latest operator mail delivered to the former
owner, cut to its first 600 bytes and named by its message identifier. **Fact.** The
state records no time for a message, so the item says so instead of giving one.
**Decided by the operator.** Mail from a line, the harness, an agent or a peer never
fills a field, since only an operator holds operator authority. Each value is checked
against the disclosure list and withheld whole when it holds a term. **Assumption.** The
latest task need not be a gate and the latest mail need not state the task, so each
item says what it is and leaves the judgement to the new owner. The journal's diff
summary, Unresolved decisions and Next action have no observed counterpart and stay
unknown when no line states them.

**A path the daemon cannot read is never reported as absent or untracked.** **Fact,
observed with git on 2026-10-29.** `git status` leaves a directory it cannot open out
of its output, warns on its error stream and exits 0. The package names each such path
as one the daemon could not read, and says that it may exist and be tracked, untracked
or ignored. The query runs with `LC_ALL=C`, so git gives its untranslated wording
whatever the operator's locale. **Assumption.** A later change of that wording leaves
such a path out without naming it. **Fact, observed.** Git quotes the path without
escaping it, so the daemon reads the name up to the quote that git's own text follows.
A name that itself holds that sequence is recorded only up to it, and the recorded name
may then not exist.

The former owner's processes are given as the daemon recorded them when its engine
ended, or as none recorded. Where the platform does not let the daemon identify a
line's processes, the item says that their state is unknown.

The fields, the brief and the journal entry are untrusted data that lines wrote, and the
package delimits them as such. Each part is checked against the disclosure list as the
package is composed and withheld whole when it holds a listed term.

## Messages between seats

**Fact.** Any line may already send any line a note, so seats of a role can message
each other today. **Implemented.** `role_status`, a tool offered only to a seat,
lists the role's worktree and handoff bound, the owner and the latest change of owner,
each seat's condition, hibernation, model, provider, stated budget and worktree commit, and
the role's latest five journal entries, the newest with its body, so a seat can find
its peers. Until the budget step, each engine counts as its own provider. **Item B3 of 2026-10-28.** Each seat also lists `recent_tasks`, its latest three background task ends from `task_recorded`, each with its command, status, exit code, output file, the SHA-256 digest and size of its output and the worktree commit, so a reviewing seat can check a gate the owner ran without running it again. The commit says nothing of uncommitted changes, and an end the engine reports during a turn is not recorded. A note between seats is ordinary line mail, data from its
sender like any other, and counts toward the autonomous limits as mail between lines
does. No role-wide broadcast is proposed, because a broadcast multiplies turns across
providers whose budgets are short.

## Provider budget

A provider is the account behind an engine, such as the Claude plan, the Codex plan or
the Grok plan. An engine maps to one provider, and Pi's provider is read from its line
configuration.

**Sources.** An operator states a budget with `dromon budget set PROVIDER` and a
remaining percentage, a reset time or both. An engine reports one when it stops at a
plan limit, which becomes `budget_stated` with `remaining_percent` 0, `source`
`engine`, and the reset time where one was given in the model's form. **Implemented.**
The classification of each engine is as follows, and each rests on a shape that is on
record. Only Codex's camel-case shapes were observed live, by the operator on
2026-10-27, from a Codex app-server probe with no credit left.

| Engine | Classified as budget exhausted | Source of the shape |
|---|---|---|
| Claude Code | A failed turn while the engine's latest `rate_limit_event` has status `rejected` and its overage is not `allowed` or `allowed_warning`, with its reset time. An assistant message whose `error` is `billing_error`, in any turn | The `RateLimitInfo` and `AssistantMessageError` types of the installed Agent SDK |
| Codex | A turn that does not complete after an `error` notice, or with a `turn.error`, whose `codexErrorInfo` is `usageLimitExceeded`, with the reset time read from its message. Before any turn fails, an `account/rateLimits/updated` notice whose `credits` has `hasCredits` and `unlimited` both false and whose `primary` and `secondary` windows are both null, which records the budget at 0 with no reset at once. The older snake-case codes and phrases of [`CODEX.md`](../protocol/CODEX.md) hold the line and ask, but record no budget and never move a role | The camel-case shapes observed live on 2026-10-27. The snake-case codes and phrases are inferred and unverified |
| Grok, Pi, Cursor, Antigravity | Nothing. A failed turn holds the line as before | No shape is on record |

**Inference.** The engine reports a change of rate-limit status, not each turn, so the
Claude Code adapter keeps the latest across turns. A rebuilt engine starts with none,
so a failure before its first report is not classified. A
stated budget is not adjusted by the cost of later turns, since a per-turn cost does
not convert reliably to a plan's percentage, so a statement is a snapshot with its time.
**Implemented** for an operator. `dromon budget set` accepts only an engine's name as
the provider until the budget step, since a statement for any other name would ground
nothing, and `dromon budget` and `dromon role status` show each statement with its
source and time. **Implemented** for engines. A plan limit an engine reports records
the budget, and so does a Codex notice of no credit left, which fails an owner over at
once when it arrives between the owner's turns.

**Use.** `role_status` and `dromon role` show the failover policy, the threshold, the
seat a failover would choose now, and each seat in the failover order with its
provider's budget, its source and its time, and whether it is usable. A budget at 0
percent fails an owner over, and a budget below the threshold asks for a planned
handoff, as the sections above say. **Decided by the operator on 2026-10-27.** That
holds for a budget an engine reports from an observed or typed shape as well as for
an operator's statement, since the operator wants switching automatic, and the
operator is told at once of each failover. **Superseded on 2026-10-27.** The earlier design
had the harness move no role by budget, and ask the operator with the other seats as
choices instead. That question is not built, and `ask` keeps the hold's question.

## Mapping the existing lines

**Decided on 2026-10-27.** The primary seats stay `core`, `engines` and `review`, each
its role's first seat and owner, keeping its identity, session and mailbox. An
alternate seat is named for its role and its engine.

| Role | Worktree | First seat and owner | Alternate seats, created hibernated |
|---|---|---|---|
| core | `tmp/worktrees/core` | `core` | `core-luna`, `core-grok` |
| engines | `tmp/worktrees/engines` | `engines` | `engines-opus`, `engines-grok` |
| review | `tmp/worktrees/review` | `review` | `review-luna`, `review-grok` |

The alternate seats are created hibernated, so that they spend nothing until a handoff or a
recovery awakens them, and the Grok seats have `may_own` false until an operator probe
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
- **A misclassified limit.** A failure counted as budget exhausted when it was not
  moves the role and records the provider spent until an operator states otherwise.
  Only the Claude Code shapes, from the Agent SDK's types, and Codex's camel-case
  shapes, observed live, can do so. Codex's inferred snake-case codes and phrases only
  hold the line and ask. Only an account with no
  credit and no reported plan window was observed, so a notice of no credit counts only
  when its `primary` and `secondary` windows are both null, by the operator's decision of
  2026-10-27. A plan with a window therefore never reads as spent from that notice,
  and its turn's limit error is what moves the role. A limit not counted holds the
  line, and the role stays put.
- **A stale spent budget.** A statement of 0 percent with no reset time keeps its
  provider's seats unusable until an operator states another budget. A Codex notice of
  no credit left records such a statement. The turn that then fails at the limit
  records the reset its message gives. A later notice with credit is not recorded,
  since it states no percentage.
- **A reset in the wrong zone.** Codex's limit message names no time zone, and is read
  in the daemon host's local zone, on the inference that Codex formats it there. A
  wrong zone moves the reset by whole hours.
- **Failover at a restart.** A plan-limit hold is known only in the daemon run that saw
  it, but its budget is in the log, so a failover after a restart still sees the
  provider spent. The daemon does not look for a failover at its start. The first
  statement of a budget, or the end of the owner's next turn, does.

## Decisions

The operator answered the four open questions on 2026-10-27, and may overrule any of
them.

1. A seat may take ownership itself when the harness shows the owner cannot act, by the
   three conditions of the recovery table. An operator can always take.
2. A seat that does not own its role may read the worktree at any time, read only, and
   writes only `tmp/`.
3. The primary seats are `core`, `engines` and `review`, and the alternates are named
   for role and engine, hibernated at creation, with the Grok seats unable to own until the
   probe. `worker`, `reviewer` and `luna` are retired.
4. The handoff bound is one hour, configurable per role as `handoff_seconds`.

**Amended by the operator on 2026-10-27, for step 3.**

5. Switching is automatic by default. Each role has a failover policy, `automatic` or
   `ask`. Under `automatic` the harness passes the role to the next seat in the
   declared order that may own it, is not hibernated, and has a provider with budget, and
   records why, wakes the new owner with the recovery checklist and tells the
   operator. Under `ask` it changes nothing and the operator decides.
6. A stated budget below a role's threshold asks the owner to prepare a handoff to the
   next seat.
7. Each engine's out-of-credit errors are classified as budget exhausted only where
   their shape is known. An unknown shape holds the line as before.
8. External agents may not change ownership, and each engine is its own provider until
   the budget step.

**Amended by the operator on 2026-10-27, after the review of step 3.**

9. Only observed shapes and the Agent SDK's typed, documented shapes may move a role.
   Codex's inferred codes and free-text phrases hold the line and ask, and never fail
   over.
10. `relinquish_ownership` inside the owner's turn is the planned handoff, and takes
    effect when that turn ends, after which the former owner has no write access.
11. Automatic failover on an engine's budget report stays, and the operator is told at
    once.
12. The confining engines are derived from the capability table once read-only seats
    merge, with Grok out until a probe, and the set is empty until then.
13. A seat with a turn in flight may be chosen, and gains write access only from its
    next turn.

## Order of work

1. The events, model checks and codec, and `role_status`, then the project file's
   roles, recorded at the daemon's start and on reload. Both are done.
2. `relinquish_ownership`, `take_ownership` and `record_role_note`, then the client
   operations and the `dromon role` and `dromon budget` commands. All are done.
3. Automatic switching, which is the failover policy, the declared order, the
   planned handoff by budget, and `budget_stated` from plan limits. Done.
4. Read-only seats in each adapter's sandbox profile, which is the engines role's work,
   and the rule for seats that cannot be confined until then. The rule is done, and the
   profiles wait for the engines role's branch to merge.
5. The question at an owner that does not hand over within `handoff_seconds`, and
   providers that are not engines, such as Pi's.

## Porting notes

Roles, seats, owners, journal entries and budgets are plain records in the event log,
checked by the model as lines are. Ownership is one optional field per role, changed
only by an event that names the value it replaces, so it maps onto a compare and swap
in a Rust or Keleusma state machine.

**The package's unreadable paths.** The prototype learns which paths git could not read
by matching git's untranslated warnings. **Porting hazard.** A port that reads the
worktree through a git library rather than the git command receives these as errors of
its own, and must still report each as unreadable rather than drop it.
