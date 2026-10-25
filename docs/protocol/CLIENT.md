# Client Protocol

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, protocol version 1, written on 2026-10-25 for roadmap task 3.
Normative. The Python modules `dromon.protocol`, `dromon.daemon` and `dromon.client`
implement it. Until the specification is marked stable, a draft version may change
without a new version number.

## Transport

One daemon serves one project on a Unix domain socket. A client connects, sends
requests, and reads one response per request in the order sent. A connection may carry
any number of requests. The daemon closes a connection after 30 seconds without a
request, after a line it cannot frame, or when it stops.

Each message is one line of canonical JSON, encoded as in
[`EVENT_LOG.md`](EVENT_LOG.md), followed by a newline. A line is at most 65,536 bytes
including the newline. A longer line receives a `too_long` error and the connection is
closed, because the rest of the line cannot be skipped safely.

## Locations

| Path | Default | Purpose |
|---|---|---|
| State directory | `<project>/.dromon` | Holds `events.sqlite`, the event log |
| Runtime directory | `<project>/.dromon/run`, or `DROMON_RUNTIME_DIR` | Holds `dromond.sock`, `dromond.lock` and `dromond.log` |

A socket path is at most 103 bytes, the smaller of the macOS and Linux limits. A
project whose default socket path is longer must set `DROMON_RUNTIME_DIR`.

## Requests

```json
{"id":"7f3a9c01","op":"status","v":1}
```

| Key | Value |
|---|---|
| `v` | The protocol version, the integer `1` |
| `id` | 1 to 64 characters from `A` to `Z`, `a` to `z`, `0` to `9`, `.`, `_` and `-`, chosen by the client and echoed in the response |
| `op` | The operation |
| `args` | Optional. An object of the operation's arguments, each of which is checked exactly |

| Operation | Result |
|---|---|
| `ping` | An empty object |
| `status` | `daemon_version`, `protocol_version`, `pid`, `project`, `started_at`, and `event_log` with `last_seq`, `lines`, `operators` and `open_questions` |
| `stop` | `{"stopping": true}`, after which the daemon closes its connections, removes its socket and exits |
| `create_line` | Arguments `line_id`, `model`, `strategy`, and optionally `config`, an object of [`LINE_CONFIG.md`](LINE_CONFIG.md). Result `{"line_id": ...}`. Refused when no packaged driver serves the model and strategy |
| `configure_line` | Arguments `line_id` and `config`. Replaces the line's configuration from its next turn |
| `questions` | Optionally `operator`. Result `{"questions": [...]}`, the open questions for that operator, or for all when omitted, oldest first, each with `question_id`, `line`, `operator`, `text` and `choices`. Listing marks each question delivered, because the client has shown it |
| `answer` | Arguments `question_id`, `body` and `operator`. Records the operator's reply, which must come from the operator the question was asked of. Result `{"message_id": ...}`. The answer wakes the asking line in a later tick |
| `send` | Arguments `recipient`, a line or `@operator`, `body`, `operator`, and optionally `reply_to`. Registers the operator if new and records a note from them. Result `{"message_id": ...}` |

`status` also returns `tick`, the ticks run in this daemon's lifetime, and `lines`, one
entry per line with `line_id`, `engine`, `strategy`, `state`, `pending`, the count of
undelivered messages, `turns`, the turns run in this daemon's lifetime, and
`tool_policy`.

## Responses

```json
{"id":"7f3a9c01","ok":true,"result":{},"v":1}
{"error":{"code":"unknown_operation","message":"unknown operation 'reboot'"},"id":"7f3a9c01","ok":false,"v":1}
```

A response carries exactly `v`, `id`, `ok`, and either `result` when `ok` is true or
`error` when it is false. The `id` is the request's, or the empty string when the
request's `id` could not be read.

| Error code | Meaning |
|---|---|
| `bad_request` | The line is not a well-formed request |
| `unsupported_version` | The request's `v` is missing or not `1` |
| `unknown_operation` | The `op` is not one listed above |
| `too_long` | The line exceeds the limit |
| `rejected` | The request is well formed but does not apply, such as a message to no line |
| `internal` | The daemon failed while handling a valid request |

Both sides decode with the strictness of the event codec. Duplicate keys, numbers other
than the version, `NaN` and `Infinity`, and missing or extra keys are refused.

## Lifecycle

- One daemon runs per runtime directory, enforced by an exclusive lock on
  `dromond.lock`. A second daemon exits with status 5 and changes nothing.
- The lock holder removes a socket file left by a crash before listening.
- `stop`, SIGTERM and SIGINT end the daemon cleanly with status 0.

## Commands

| Command | Behaviour | Exit status |
|---|---|---|
| `dromon start` | Starts a detached `dromond` in a new session and waits up to 10 seconds for it to answer `ping`. Succeeds without action when a daemon already answers | 0, or 1 on failure |
| `dromon status` | Prints the `status` result | 0, 4 when no daemon answers, or 1 |
| `dromon stop` | Sends `stop` and waits up to 10 seconds for the socket to disappear | 0, 4 when no daemon answers, or 1 |
| `dromon create LINE` | Sends `create_line`, with `--model`, default `claude-haiku-5-5`, `--strategy`, default `subscription`, and the tool options below | 0, 4, or 1 |
| `dromon configure LINE` | Sends `configure_line` with the tool options below. No option means the default configuration | 0, 4, or 1 |
| `dromon send RECIPIENT BODY` | Sends `send`, with `--operator`, default the login name, and `--reply-to` | 0, 4, or 1 |
| `dromon questions` | Sends `questions` for `--operator`, default the login name, or for every operator with `--all`, and prints each question with its choices | 0, 4, or 1 |
| `dromon answer QUESTION BODY` | Sends `answer`, with `--operator`, default the login name | 0, 4, or 1 |
| `dromon probe` | Runs one short turn without the daemon and reports the engine's capabilities | 0 or 1 |
| `dromond` | Runs the daemon in the foreground. `--max-turns-per-line N` caps the turns run for each line, default 10, and `--hold-wakes` records messages without waking any line, and `--tick-seconds S` sets the time between ticks while work is in flight, default 1, at most 60 | 0, 1 on failure, 2 on a bad cap or cadence, or 5 when another daemon holds the lock |

The tool options are `--tool NAME`, repeatable, `--mcp-config FILE`, a JSON file of MCP
servers that the client reads, `--allow-tool NAME`, repeatable, `--allow-all-tools` and
`--inherit-account-mcp`.

Status 2 is a usage error and status 3 marks a subcommand not yet implemented.

## Trust

Whoever can connect runs as the operator's user, so a request carries operator authority
and names, in `send`, the operator it speaks for. Lines never use the socket. They send
through their `send_message` tool, and the daemon fixes each tool's sender to its line.

The socket's access control is the runtime directory, which must be owned by the
current user with mode 0700. The daemon refuses to start otherwise and never changes an
existing directory's mode. The socket and the event log are created with mode 0600. Any
process running as the same user can connect, so the protocol treats every request as
untrusted input and checks it strictly. A peer credential check on each connection is
not yet implemented, because its interface differs between macOS and Linux.

## Not yet specified

- Attaching an interactive client, and the graphical interface roadmap task 8 calls for.
- Authentication of a client beyond the directory's permissions.
- Notifications pushed from the daemon to an attached client.
