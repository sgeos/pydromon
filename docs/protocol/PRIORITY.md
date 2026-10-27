# Wake Mute, Priority Mail and Self Mail

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Decided by the operator on 2026-10-27.** Three changes to how a line is woken. An
operator may mute a line, so that no turn of it is spent without stopping the daemon.
An operator or a line may send a line priority mail, which redirects a long turn. A
line may send itself a note. The events are specified in [`EVENT_LOG.md`](EVENT_LOG.md)
and the requests in [`CLIENT.md`](CLIENT.md). This document gives their behaviour.

## Wake mute

`mute` records `line_muted` and `unmute` records `line_unmuted`. Both are operator
requests. External agents are not offered them, and no line tool reaches them.

While a line is muted, no turn of it starts. That covers every way a turn starts.

| Wake | While muted |
|---|---|
| Mail from an operator, a line or an agent | Kept undelivered in the line's mailbox |
| A note the line sent itself | Kept undelivered |
| A scheduled wake, from `wake_after` or `compact_session` | Kept pending. It does not fire while the line is muted, and fires at the first tick after the line is unmuted, with its lateness in its delivery note |
| A note that a background task ended | Recorded and kept undelivered |
| An operator's answer to a question, including a ceiling question | Recorded and kept undelivered. A ceiling choice still reaches the kept connection at once |
| Priority mail | Kept undelivered, and it ends no turn |

A turn the engine begins by itself between Dromon's turns is interrupted whether or not
the line is muted, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for kept sessions.

The line keeps its engine session, its kept connection and its leases. A mute is not a
hold. It asks no question, resets no count, and an operator's reply does not end it.
A line may be held and muted at once, and then it needs both a release and an unmute.

**A turn in flight.** Muting a line with a turn in flight lets that turn finish. The
line's own tools keep working until its end. The mute takes effect when the turn ends,
and no turn follows. An operator who wants the turn stopped as well sends `interrupt`,
which also holds the line, as [`CLIENT.md`](CLIENT.md) specifies.

**Status.** `line_status` gives `muted`, and `condition` is `muted` for a muted line
that is not running, ahead of every condition but `running`. `other_lines` gives each
line's `muted`. The daemon's `status` gives each line's `condition` and `muted`, and
`dromon status` prints one row per line naming either.

**Restart.** The mute is in the event log, so it survives a restart, and a scheduled
wake that fell due while the daemon was down still waits for the unmute.

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
   cooldown or its wake tick. A muted, held or failed line still starts none.

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
note leaves the window. The counts are kept in memory, so a restart starts them again.
**Inference.** A restart is an operator's act, so this does not let a line evade the
limit.

**Safe points.** Every engine adapter feeds the events of a turn to the line's Keleusma
driver, as [`DRIVER.md`](DRIVER.md) specifies, and ends the turn gracefully when the
driver answers `interrupt`. The engine then reports the turn, its cost when it reports
one, and its session, which the next turn resumes. Priority mail uses that path. At the
first `text` or `thinking` event of the recipient's turn after the note was sent, and
only when the driver itself would continue, the daemon answers `interrupt` in the
driver's place, with the reason as its text. The driver still sees every event, so its
state advances exactly as without priority mail. A `tool_use` event is never a safe
point, because the tool may already be starting.

| Engine | Safe point | How the turn ends |
|---|---|---|
| Claude Code | A text or thinking block, also one a subagent writes | The Agent SDK's interrupt request. A kept connection stays open |
| Codex | A completed agent message, or a reasoning item as it starts | `turn/interrupt` on the app-server |
| Grok, over ACP | A message or thought chunk | ACP's `session/cancel` |
| Antigravity, over ACP | A message or thought chunk | ACP's `session/cancel` |
| Pi | A text delta, or reasoning as it starts | Pi's `abort` command |
| Cursor Agent | A text or thinking event | The process is ended, since Cursor takes no command once it starts |

**Hypothesis, not verified live.** Claude Code streams each content block as it
completes, so a text block reaches the adapter before a tool call that follows it in
the same message has started. If it instead sent the text and the tool call together,
the interrupt could reach a tool that had just started, which the engine would then
stop as it stops any interrupted tool. A live check is owed.

**When no safe point comes.** A turn that reaches no text or thinking event after the
note, such as a turn spent in one long tool call until it ends, is not interrupted. The
note then leads the next turn as above. **Inference.** A model writes or reasons
between tool calls, so a turn of several calls reaches a safe point after the call in
progress. An operator who must stop a turn at once sends `interrupt`.

**A muted recipient.** Priority mail never ends a muted line's turn, and waits for the
unmute with the line's other mail.

**Crash.** The two appends are separate. A crash between them leaves an ordinary note,
which is delivered as one. A crash during a turn the note was ending loses nothing,
because the note is undelivered until the next turn.

## Self mail

A line may send itself a note with `send_message` to its own identifier, such as a note
to its next turn or a test of the tool. The note is delivered from the next tick, like
other mail, and wakes the line once its turn in flight has ended. It is from the line
and not from an operator, so the turn it starts counts as autonomous toward the line's
limits.

Operators and external agents may not send themselves mail, since no turn of theirs
receives it. A scheduled wake's note, which fires as a message from the line to itself
under its reserved identifier, is unchanged, and a self note may not take an identifier
a pending scheduled wake reserves.

## Porting notes

The mute is one flag per line in the state. The rate limit is two bounded rings of
monotonic times per party. The safe point is one comparison per driver event, in the
host rather than in the driver's Keleusma source, so the packaged drivers are unchanged
and a Keleusma host can make the same comparison in its stream processor.
