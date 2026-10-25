# Event Log

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, format version 1 and schema version 1, written on 2026-10-25 for
roadmap task 2. This document is normative. The Python modules `dromon.model`,
`dromon.codec` and `dromon.eventlog` implement it, and where they disagree with it, the
disagreement is a defect to be resolved here first.

## Purpose

The event log is the daemon's only durable state. Every change to session lines,
messages, wakes and operator questions is one event, appended once and never changed.
The state is never stored. It is rebuilt by applying the events in order to an empty
state, so recovery after a crash, a newly attached client and a port all agree on the
state by replaying the same log.

## Values

| Value | Rule |
|---|---|
| Identifier | 1 to 64 ASCII characters from `a` to `z`, `0` to `9`, `.`, `_` and `-`, starting with a letter or digit. Compared bytewise |
| Operator | The literal `@operator`, valid only as a message sender. It can never be an identifier |
| Text | A Unicode string with a valid UTF-8 encoding of at most 1,048,576 bytes. Lone surrogates are invalid |
| Choice | Text of at most 1,024 bytes |
| Choices | At most 16 distinct choices, in order |
| Boolean | `true` or `false`, never a number |
| Line state | `idle`, `running` or `stopped` |
| Wake cause | `message` or `answer` |

Identifiers are never reused within their kind, even after a line stops or a question
closes. A resent event is therefore rejected rather than applied twice.

## Events

Each event has a kind and fields, all required.

| Kind | Fields | Applies when |
|---|---|---|
| `line_created` | `line_id`, `engine` identifiers | The line does not exist. The new line is idle |
| `line_state_changed` | `line_id`, `state` | The line exists, the change is in the transition table, and a change to `running` is refused while the line is blocked |
| `message_sent` | `message_id`, `sender` operator or identifier, `recipient`, `body` text | The message does not exist, the recipient exists, a line sender exists, and sender and recipient differ |
| `message_delivered` | `message_id` | The message exists and was not delivered |
| `wake_requested` | `wake_id`, `line_id`, `cause`, `cause_id` | The wake does not exist and the line exists and is not stopped. For `message`, the message exists and its recipient is the line. For `answer`, the question exists, was asked by the line and is answered |
| `wake_issued` | `wake_id` | The wake exists and was not issued, and its line is idle and not blocked |
| `question_asked` | `question_id`, `line_id`, `text`, `choices`, `blocking` boolean | The question does not exist and the line exists and is not stopped. The question is open |
| `question_answered` | `question_id`, `answer` text | The question is open |
| `question_withdrawn` | `question_id` | The question is open |
| `question_superseded` | `question_id`, `superseded_by` | Both questions are open, they differ, and they were asked by the same line |

**Line transitions.** `idle` to `running`, `running` to `idle`, `idle` to `stopped`,
`running` to `stopped`, and `stopped` to `idle`. No other change is permitted, including
a change to the same state.

**Blocking.** A line is blocked while it has an open question with `blocking` true. A
blocked line cannot start a turn or be woken, and nothing else is affected.

**Answers.** An answer is free text even when the question offers choices, so the
operator is never forced into an option that does not fit.

**Rejection.** An event that does not apply is rejected whole and changes nothing.

## Encoding

An event is stored as one JSON object.

```json
{"data":{"engine":"claude-opus-5-5","line_id":"alpha"},"kind":"line_created","v":1}
```

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
before appending it. In particular, only the daemon acting for the operator through a
client may append `question_answered`, and a `message_sent` with the sender `@operator`
must come from a client. The triggers prevent mistakes and do not stop a local process
with write access to the file, so the file's permissions are the security boundary.

## Not yet specified

- Events for configuration, conversation limits, turns and engine output.
- Compaction or snapshots for long logs.
- Migration between schema versions. Version 1 refuses every other version.
