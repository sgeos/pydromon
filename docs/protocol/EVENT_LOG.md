# Event Log

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, format version 1 and schema version 1, first written on 2026-10-25
for roadmap task 2 and revised the same day to make operators addressable. Until the
specification is marked stable, a draft version may change without a new version
number, because nothing yet depends on it. This document is normative. The Python modules
`dromon.model`, `dromon.codec` and `dromon.eventlog` implement it, and where they
disagree with it, the disagreement is a defect to be resolved here first.

## Purpose

The event log is the daemon's only durable state. Every change to session lines,
operators, messages, wakes and operator questions is one event, appended once and never
changed.
The state is never stored. It is rebuilt by applying the events in order to an empty
state, so recovery after a crash, a newly attached client and a port all agree on the
state by replaying the same log.

## Values

| Value | Rule |
|---|---|
| Identifier | 1 to 64 ASCII characters from `a` to `z`, `0` to `9`, `.`, `_` and `-`, starting with a letter or digit. Compared bytewise |
| Line address | A line's identifier, such as `alpha` |
| Operator address | `@` followed by an operator's identifier, such as `@ana`. `@` never appears in an identifier, so the two kinds of address cannot collide |
| Agent address | `&` followed by an external agent's identifier, such as `&claude-main`. `&` never appears in an identifier |
| Address | A line address, an operator address or an agent address |
| Agent access | `none`, `observe`, `message`, `manage` or `operator`, each including the levels before it |
| Token digest | 64 lowercase hexadecimal characters, the SHA-256 of a token, or empty for an agent admitted without one |
| Text | A Unicode string with a valid UTF-8 encoding of at most 1,048,576 bytes. Lone surrogates are invalid |
| Choice | Text of at most 1,024 bytes |
| Choices | At most 16 distinct choices, in order |
| Boolean | `true` or `false`, never a number |
| Line state | `idle`, `running` or `stopped` |
| Message kind | `note` or `question` |
| Due time | A time in Coordinated Universal Time to the second, written exactly as `YYYY-MM-DDTHH:MM:SSZ`, such as `2026-10-26T09:30:00Z`, and a valid date and time |
| Wake note | Text of at most 2,000 bytes |

Identifiers are never reused within their kind, even after a line stops or a question
closes. A resent event is therefore rejected rather than applied twice. Lines and
operators have separate identifier spaces.

## Parties

Session lines and operators are both parties with addresses, and any number of each may
exist. A project with five people and thirty lines has five operators, each addressed
separately. An operator question is a message of kind `question` from a line to one
operator, and its answer is that operator's reply. Questions therefore travel by the
same path as every other message, and the question lifecycle is tracked on the message.
An external agent is a party too, with its own mailbox, as
[`AGENTS.md`](AGENTS.md) specifies. It may send and receive notes but not ask
questions, and a revoked agent neither sends nor receives.

## Events

Each event has a kind and fields, all required. An optional value is written as `null`.

| Kind | Fields | Applies when |
|---|---|---|
| `line_created` | `line_id`, `engine` and `strategy` identifiers | The line does not exist. The new line is idle. The engine names the model, and the strategy names the driver's credential strategy |
| `line_state_changed` | `line_id`, `state` | The line exists and the change is in the transition table |
| `line_configured` | `line_id`, `config` text of at most 262,144 bytes | The line exists. The text is the canonical JSON of [`LINE_CONFIG.md`](LINE_CONFIG.md), validated by the daemon before it appends, and replaces any earlier configuration |
| `line_session` | `line_id`, `engine_session` text of 1 to 256 bytes | The line exists. Records the engine session the line's next turn resumes, so a restarted daemon continues the line's conversation. Appended after a turn, or right after `line_created` when the line adopts an existing session |
| `line_session_cleared` | `line_id` | The line exists. Its next turn starts a new engine session, as an operator asked with `clear_session`, or because the line's `workdir` changed. The engine keeps the old session |
| `line_overridden` | `line_id`, `overrides` text of at most 262,144 bytes | The line exists. The text is the canonical JSON of the line's override document, as [`CLIENT.md`](CLIENT.md) specifies under settings, validated by the daemon before it appends, and replaces any earlier overrides of the line |
| `project_overridden` | `overrides` text of at most 262,144 bytes | Always. The project's override document, replacing any earlier one |
| `line_held` | `line_id`, `question_id` | The line exists and is not held, and the question is open and from the line. The daemon wakes the line no more until it is released |
| `line_released` | `line_id` | The line is held |
| `operator_registered` | `operator_id` | The operator does not exist |
| `agent_registered` | `agent_id`, `access`, `token_sha256` | Always. Registers the agent, or replaces its access and digest and restores it if revoked. A token itself is never stored |
| `agent_revoked` | `agent_id` | The agent exists and is not revoked |
| `message_sent` | `message_id`, `sender` and `recipient` addresses, `kind`, `body` text, `choices`, `reply_to` message identifier or `null` | See the message rules below |
| `message_read` | `message_id` | The message was delivered and not yet read. Its recipient opened it, which obliges nothing |
| `message_acknowledged` | `message_id` | The message was delivered and not yet acknowledged. Its recipient marked it dealt with without a reply. Its sender sees the acknowledgement as distinct from a reply. A question stays open |
| `message_delivered` | `message_id` | The message exists and was not delivered. For a line, delivery is into a turn. For an operator, delivery is acceptance by the operator's relay or display in a client |
| `wake_requested` | `wake_id`, `line_id`, `message_id` | The wake does not exist, the line exists and is not stopped, and the message exists with the line as its recipient |
| `wake_issued` | `wake_id` | The wake exists and was not issued, and its line is idle |
| `wake_scheduled` | `line_id`, `message_id`, `due` due time, `note` wake note | The line exists, no message has the identifier, and no other line's pending scheduled wake reserves it. Becomes the line's one pending scheduled wake, replacing any earlier one, and reserves `message_id` for its note |
| `question_withdrawn` | `message_id` | The message is a question and is open |
| `question_superseded` | `message_id`, `superseded_by` | Both messages are open questions, they differ, and they come from the same line |

**Message rules.** A `message_sent` applies when every one of these holds.

1. The message does not exist, and the sender and recipient both exist and differ. The
   one exception is the firing of a scheduled wake, below. A message whose identifier a
   pending scheduled wake reserves is refused unless it is that firing.
2. A note has no choices.
3. A question comes from a line that is not stopped and goes to an operator. A new
   question is open.
4. A reply, which has a `reply_to`, names an existing message and reverses its route.
   The reply's sender is the earlier message's recipient, and its recipient is the
   earlier message's sender. An answer from any other operator, or to any other line,
   is refused.
5. A reply to a question requires the question to be open, must be a note, and marks
   the question answered by this reply.

**Scheduled wakes.** **Decided by the operator on 2026-10-26.** A line schedules a wake
for itself with the `wake_after` tool, as [`LINE_CONFIG.md`](LINE_CONFIG.md)
specifies. A line has at most one pending scheduled wake. When its due time has passed,
the daemon fires it by appending one `message_sent` whose `message_id` is the reserved
identifier, whose sender and recipient are both the line, whose kind is `note`, whose
body is the note or the fixed withheld text below, and which has no choices and no
`reply_to`. Applying it clears the
pending wake. That is the only message a party may send itself.

- **Sender.** The model has no address for the daemon, an operator sender would make
  the note operator mail, which carries authority and resets a line's autonomous-turn
  count, and an agent sender would need a registered agent. A note from the line to
  itself is accurate, since the line scheduled it, shows as `from alpha` and never as
  operator mail, and needs no new kind of address in any port.
- **Idempotence.** Firing is a single append, so a crash leaves the wake either pending
  or fired. A pending wake fires at the daemon's first tick after it restarts, and a
  fired one cannot fire again, since its message exists.
- **Withheld note.** The daemon checks the note again when it fires, against the
  disclosure list then in force, which a reload or a restart may have changed. A note
  that now contains a listed term fires with the body
  `Your scheduled wake fell due. Its note was withheld, because it contains a term from the project's disclosure list.`
  The line is still woken, the note goes no further, and the fixed text reveals no
  term. The model admits exactly these two bodies.
- **Due time.** The model checks only the form. The daemon compares the due time with
  its wall clock in Coordinated Universal Time.

**Line transitions.** `idle` to `running`, `running` to `idle`, `idle` to `stopped`,
`running` to `stopped`, and `stopped` to `idle`. No other change is permitted, including
a change to the same state.

**No blocking.** **Decided by the operator on 2026-10-25.** Nothing blocks a line. After
asking a question, a line may end its turn or keep working, as the line-agent decides,
and the answer arrives as mail in a later turn, like any reply. An open question never
stops its line from being woken.

**Answers.** An answer is free text even when the question offers choices, so the
operator is never forced into an option that does not fit.

**Rejection.** An event that does not apply is rejected whole and changes nothing.

## Encoding

An event is stored as one JSON object.

```json
{"data":{"engine":"claude-opus-5-5","line_id":"alpha","strategy":"subscription"},"kind":"line_created","v":1}
```

Addresses are written as their text forms, such as `"beta"`, `"@ana"` and `"&claude"`.

- `v` is the format version, the integer `1`.
- `kind` is the event kind and `data` holds exactly its fields.
- Choices are a JSON array of strings.
- The encoding is canonical. Keys are sorted by code point at every level, there is no
  whitespace outside strings, and text is UTF-8 without escapes for non-ASCII characters.
  The same event always encodes to the same bytes. Inside strings, `"` and `\` are
  escaped as `\"` and `\\`. The control characters U+0008, U+0009, U+000A, U+000C and
  U+000D are escaped as `\b`, `\t`, `\n`, `\f` and `\r`. Every other character below
  U+0020 is escaped as `\u` with four lowercase hexadecimal digits. Nothing else is
  escaped, including `/`, U+007F and U+2028.
- An encoded event is at most 8,388,608 bytes.

**Decoding.** A decoder treats every payload as untrusted. It rejects a payload that is
not one JSON object, that has a duplicate key at any level, that contains any number
other than the version, that contains `NaN` or `Infinity`, that has a missing or extra
key, or whose values break the rules above.

## Storage

The log is an SQLite database whose `user_version` is the schema version, `1`, holding
one table.

```sql
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL
) STRICT
```

- `seq` starts at 1 and increases by one per event with no gaps.
- `recorded_at` is the time of the append in Coordinated Universal Time, in ISO 8601 with
  a `Z` suffix. It is metadata and never affects the state.
- `payload` is the canonical encoding of the event.
- Triggers abort every `UPDATE` and `DELETE` on the table.
- A new log file is created with owner-only read and write permission.

**Replay.** A reader applies the events in `seq` order to an empty state. A gap in `seq`,
a payload that does not decode, or an event that does not apply makes the log corrupt,
reported with the offending `seq`. A reader never skips a row.

**Writers.** One process writes a log. Before each append, the writer checks inside an
immediate transaction that the newest `seq` is the one it last read, and refuses to
append otherwise.

## Trust

The log records events and does not authenticate them. The daemon attributes each event
before appending it. In particular, a `message_sent` whose sender is an operator must
come from that operator, through a client or an inbound channel that authenticates
them. The route rule for replies stops one operator from answering a question addressed
to another, but only if senders are attributed correctly. The triggers prevent mistakes and do not stop a local process
with write access to the file, so the file's permissions are the security boundary.

## Not yet specified

- Events for limit counts, turns and engine output. Settings an operator changes from a
  client are recorded by `line_overridden` and `project_overridden`.
- How a line chooses which operator to ask, which belongs to per-project configuration.
- Retiring an operator or a line.
- Compaction or snapshots for long logs.
- Migration between schema versions. Version 1 refuses every other version.
