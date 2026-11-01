# Hibernation, Priority Mail and Self Mail

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Decided by the operator on 2026-10-27.** Three changes to how a line is woken. An
operator may hibernate a line, so that no turn of it is spent without stopping the daemon.
An operator or a line may send a line priority mail, which redirects a long turn. A
line may send itself a note. The events are specified in [`EVENT_LOG.md`](EVENT_LOG.md)
and the requests in [`CLIENT.md`](CLIENT.md). This document gives their behaviour.

## Hibernation

`hibernate` records `line_hibernated` and `awaken` records `line_awakened`. Both are operator
requests. External agents are not offered them, and no line tool reaches them.

While a line is hibernated, no turn of it starts. That covers every way a turn starts.

| Wake | While hibernated |
|---|---|
| Mail from an operator, a line or an agent | Kept undelivered in the line's mailbox |
| A note the line sent itself | Kept undelivered |
| A scheduled wake, from `wake_after` or `compact_session` | Kept pending. It does not fire while the line is hibernated, and fires at the first tick after the line is awakened, with its lateness in its delivery note |
| A note that a background task ended | Recorded and kept undelivered |
| An operator's answer to a question, including a ceiling question | Recorded and kept undelivered. A ceiling choice still reaches the kept connection at once |
| Priority mail | Kept undelivered, and it ends no turn |

A turn the engine begins by itself between Dromon's turns is interrupted whether or not
the line is hibernated, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for kept sessions.

The line keeps its engine session, its kept connection and its leases. Hibernation is not a
hold. It asks no question, resets no count, and an operator's reply does not end it.
A line may be held and hibernated at once, and then it needs both a release and an awakening.

**A turn in flight.** Hibernating a line with a turn in flight lets that turn finish. The
line's own tools keep working until its end. Hibernation takes effect when the turn ends,
and no turn follows. An operator who wants the turn stopped as well sends `interrupt`,
which also holds the line, as [`CLIENT.md`](CLIENT.md) specifies.

**Status.** `line_status` gives `hibernated`, and `condition` is `hibernated` for a hibernated line
that is not running, ahead of every condition but `running`. `other_lines` gives
`hibernated` for each hibernated line. The daemon's `status` gives each line's `condition` and `hibernated`, and
`dromon status` prints one row per line naming either.

**Restart.** Hibernation is in the event log, so it survives a restart, and a scheduled
wake that fell due while the daemon was down still waits until the line is awakened,.

## Priority mail

A priority note is a note to a line, recorded as `message_sent` followed by
`message_prioritized`. It differs from other mail in three ways.

1. When the recipient is running a turn, that turn is asked to end at its next safe
   point, below. The turn's activity says so in a `notice`, and its `turn_ended` text
   adds `ended at a safe point for priority mail` with the note and its sender.
2. Priority notes lead the delivery that follows, in the order sent, ahead of the
   recipient's other new mail. Their headers say `priority`, and a note that ended the
   last turn says so too. `list_mail` shows `priority` in its header notes.
3. A turn with a priority note to deliver starts without waiting for the line's
   cooldown or its wake tick. A hibernated, held or failed line still starts none.

A priority note is otherwise ordinary mail. It carries no more authority than its
sender has, and a line's priority note is data from that line like any other.

**Who may send it.** An operator, with `send` and `priority`, or `dromon send
--priority`. A line, with `send_message` and `priority`. The recipient must be a line.
A line may not send priority mail to itself, since the turn it would end is its own. An
external agent may not send priority mail, by default deny, since it has no operator
authority. Each refusal records nothing, so the sender may send the note without
priority.

**Rate limit.** Operator priority mail is unlimited. Priority mail from lines is
limited, so that one line cannot keep another from working.

| Limit | Value |
|---|---|
| Window | 600 seconds, on the daemon's monotonic clock |
| Priority notes one line may send, to all recipients | 3 per window |
| Priority notes one line may receive from lines | 2 per window |

A refusal over a limit names the limit and the whole seconds until the oldest counted
note leaves the window. The counts are kept only in the daemon's memory, so every
restart, whether by `dromon restart` or by a stop and a start, sets them to zero, and a
line may then send and receive the full allowance again at once. **Inference.** A
restart is an operator's act, which no line tool reaches, so this does not let a line
evade the limit.

**Safe points.** Every engine adapter feeds the events of a turn to the line's Keleusma
driver, as [`DRIVER.md`](DRIVER.md) specifies, and ends the turn gracefully when the
driver answers `interrupt`. The engine then reports the turn, its cost when it reports
one, and its session, which the next turn resumes. Priority mail uses that path.
**Decided by the operator on 2026-10-27, after the review of the first version.** A turn
may end at one of two points, and nowhere else.

1. After a tool result is delivered and before the next model call. The adapter feeds
   a `tool_result` event once a tool the turn used has finished. It is a safe point
   only when every `tool_use` the turn reported has its result, so a turn with calls
   running in parallel, or a subagent's calls inside a task, is not ended while any of
   them runs.
2. At the end of an assistant message that holds no tool use. The adapter feeds a
   `message_end` event whose number is the message's count of tool uses, and only a
   count of zero is a safe point.

At such a point, and only when the driver itself would continue, the daemon answers
`interrupt` in the driver's place, with the reason as its text. The driver still sees
every event, so its state advances exactly as without priority mail, and the packaged
drivers answer `continue` to both new events, as they answer every tag they do not
name. A `text`, `thinking` or `tool_use` event is never a safe point. A text or
thinking block can share a model message with a tool use after it, and the engine may
start that tool before the interrupt lands. The first version ended turns at text and
thinking events, and the live check of 2026-10-27 passed only because the text came in
its own message.

| Engine | Tool result | End of a message without a tool use | How the turn ends |
|---|---|---|---|
| Claude Code | A tool result block in the user message the engine echoes, also one a subagent's call returns | Not reported | The Agent SDK's interrupt request. A kept connection stays open |
| Codex | The completion of a tool item the line was given | Not reported | `turn/interrupt` on the app-server |
| Grok, over ACP | The first update that reports a call `completed` or `failed` | Not reported | ACP's `session/cancel` |
| Antigravity, over ACP | The first update that reports a call of the line's own tools `completed` or `failed` | Not reported | ACP's `session/cancel` |
| Pi | `tool_execution_end` for a tool the line was given | `message_end` of an assistant message without a `toolCall` block or a `toolUse` stop reason | Pi's `abort` command |
| Cursor Agent | A tool call's `completed` record | Not reported | The process is ended, since Cursor takes no command once it starts |

**Inference.** On the engines that do not report it, an assistant message without a
tool use ends the model's loop, so its end is the end of the turn and no point to
interrupt at. **Inference, not verified live.** Claude Code echoes a tool's result as
it sends the next model call, so the interrupt ends that call before it can start a
tool. Pi's block type and stop reason are as its documentation gives them, not
observed in a recording here, so either alone marks a message as holding a tool call.
A live check on each engine is owed.

**When no safe point comes.** A turn that reaches no safe point after the note, such
as a turn that makes no tool call, or one spent in a single long tool call until it
ends, is not interrupted. The note then leads the next turn as above. A tool use whose
result never arrives keeps every later point unsafe, which errs on the side of the
tool. An operator who must stop a turn at once sends `interrupt`.

**What the turn reports.** An interrupted turn's engine subtype can read as an error,
such as Claude Code's `error_during_execution`. It is not counted as a failure, and its
`recent_turns` entry in `line_status` gives `outcome` `interrupted` and
`interrupted_for` with the reason, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies.

**A hibernated recipient.** Priority mail never ends a hibernated line's turn, and waits with the
line's other mail until the line is awakened.

**Crash.** The two appends are separate. A crash between them leaves an ordinary note,
which is delivered as one. A crash during a turn the note was ending loses nothing,
because the note is undelivered until the next turn.

## Self mail

A line may send itself a note with `send_message` to its own identifier, such as a note
to its next turn or a test of the tool. The note is delivered from the next tick, like
other mail, and wakes the line once its turn in flight has ended. It is from the line
and not from an operator, so the turn it starts counts as autonomous toward the line's
limits.

**Self mail is not rate limited.** **Decided by the operator on 2026-10-27.** A line can
chain its own turns by sending itself a note in each, as two lines can by mailing each
other. The chain is bounded by the line's autonomous-turn limit and by its cost hold,
which hold the line once it passes either, so no separate limit is applied.

Operators and external agents may not send themselves mail, since no turn of theirs
receives it. A scheduled wake's note, which fires as a message from the line to itself
under its reserved identifier, is unchanged, and a self note may not take an identifier
a pending scheduled wake reserves.

## Porting notes

Hibernation is one flag per line in the state. The rate limit is two bounded rings of
monotonic times per party, checked and updated on the daemon's one event loop, so it
needs no lock there, and a port with threads must make the check and the update one
step. The safe point is a count of tool uses waiting for a result and one comparison
per driver event, in the host rather than in the driver's Keleusma source, so the
packaged drivers are unchanged and a Keleusma host can keep the same count in its
stream processor.
