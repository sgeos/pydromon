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
| Runtime directory | `<project>/.dromon/run`, or `DROMON_RUNTIME_DIR` | Holds `dromond.sock`, `dromond.lock`, `dromond.log`, `lines.sock` for engines that reach their line's tools over MCP, as [`GROK.md`](GROK.md) specifies, and `agents.sock` when the project lets external agents in, as [`AGENTS.md`](AGENTS.md) specifies |

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
| `status` | `daemon_version`, `protocol_version`, `pid`, `project`, `started_at`, `daemon`, the object `line_status` gives, with the commit the daemon started from, `restart`, the draining restart or the last one abandoned or `null`, as [`RESTART.md`](RESTART.md) specifies, and `event_log` with `last_seq`, `lines`, `operators`, `open_questions`, and the log's size and growth, `events`, `bytes`, `since_start`, `events_per_hour`, `bytes_per_hour` and `compaction`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for `line_status`, where an unknown size is `null` and never zero |
| `stop` | `{"stopping": true}`, after which the daemon closes its connections, removes its socket and exits |
| `restart` | Arguments `operator`, and optionally `mode`, `wait` or `interrupt` with `wait` the default, `wait_seconds`, a whole number from 1 to 3,600 with 600 the default, and `end_tasks`, a boolean with false the default. Begins a restart to upgrade, as [`RESTART.md`](RESTART.md) specifies. Returns `restarting`, true, `mode`, `wait_seconds`, `running`, the lines with a turn in flight, `pid` and `commit`, the daemon's, and with `end_tasks` the warning `stop` gives about background tasks. Refused while a restart drains, and while kept engine sessions run background tasks without `end_tasks`. Operator only, and not offered to external agents |
| `create_line` | Arguments `line_id`, `model`, `strategy`, and optionally `config`, an object of [`LINE_CONFIG.md`](LINE_CONFIG.md), and `engine_session`, an existing engine session to continue, recorded as `line_session` right after `line_created` and checked as that event is. Result `{"line_id": ...}`. Refused when no packaged driver serves the model and strategy, when the configuration's `workdir` is not allowed, or when the project requires a sandbox and the line would run on Claude Code without one |
| `configure_line` | Arguments `line_id` and `config`. Replaces the line's configuration from its next turn. A changed `workdir` also appends `line_session_cleared`. Refused when the `workdir` is not allowed, or when the project requires a sandbox and the line would run on Claude Code without one |
| `questions` | Optionally `operator`. Result `{"questions": [...]}`, the open questions for that operator, or for all when omitted, oldest first, each with `question_id`, `line`, `operator`, `text` and `choices`. Listing marks each question delivered, because the client has shown it |
| `mail` | Arguments `operator`, and optionally `unread_only`, `sender`, `offset` and `limit`, at most 50. Delivers the operator's undelivered mail, then returns `total`, `unread`, `unanswered` and `messages`, a page of headers with `message_id`, `sender`, `kind`, `reply_to`, `read` and `outcome`, one of `replied`, `acknowledged` and `unanswered`, oldest first |
| `acknowledge` | Arguments `operator` and `message_id`. Marks a message in the operator's mailbox dealt with, without a reply |
| `sent` | Arguments `operator`, and optionally `unanswered_only`, `offset` and `limit`. Returns `total` and `messages`, the operator's sent mail with `message_id`, `recipient`, `kind`, `delivered`, `read` and `outcome` |
| `read` | Arguments `operator` and `message_id`. Returns the message with its `body` and `choices`, and marks it read the first time |
| `line_view` | Arguments `line_id`, and optionally `limit`, at most 50. Returns `line`, the line's status entry, `tick`, `total`, the count of messages involving the line, `messages`, the newest of them oldest first, each with a body preview of at most 600 characters and `truncated`, kept within a 40,000 byte budget, and `open_questions`, the line's open questions. What an attached client polls |
| `line_activity` | Arguments `line_id`, and optionally `after`, a revision, default 0. Returns `line_id`, `seq`, the daemon's newest revision, `entries`, the line's activity entries changed after `after`, oldest first, the newest kept within a 40,000 byte budget, and `complete`, false when older changed entries were left out. Each entry has `entry_id`, `seq`, `kind` and `text`, as the activity section below specifies. What an attached client polls to follow a turn |
| `settings` | Optionally `line_id`. Returns `project`, the project's settings, `models`, every model a line may be created on, and with `line_id` also `line_id` and `line`, the line's settings, each as the settings section below specifies |
| `set_setting` | Arguments `scope`, `line` with `line_id` or `project` without it, `key`, and optionally `value`. Records the value as an override, or removes the key's override when `value` is omitted. Returns `scope`, `line_id` for a line, `key`, `value` and `source`, the value now in force and its layer, and `shadowed`, true when a value was set but a higher layer still decides. Refused, recording nothing, for an unknown key or a value its key or its meaning does not accept |
| `clear_session` | Argument `line_id`. The line's next turn starts a new engine session, and the leases it holds end. Refused while the line runs a turn |
| `rebind_line` | Arguments `line_id`, `model` and `strategy`. Moves the line to another model and driver strategy, recorded as `line_rebound`. The line keeps its identity, mailbox, configuration and overrides, except a model override, which is dropped with a `line_overridden`. Its engine session is cleared, its engine rebuilt and its leases end. Result `{"line_id": ..., "model": ..., "strategy": ...}`. Refused while the line runs a turn, for a model and strategy with no packaged driver or adapter, for the binding the line already has, for a configuration or override the new engine cannot run, as `create_line` checks one, for a move onto Codex or Grok while the project's own sandbox or settings would reach the line, and for a move onto Claude Code without a sandbox the project requires. Operator only, and not offered to external agents |
| `reload_project` | No arguments. Reads `.dromon/dromon.toml` again and applies its lines, limits, wake policy, operator, tick cadence, sandbox requirement and disclosure list, which it reads again. Returns `file`, `present`, `lines`, the lines it declares, and `roles`, the roles it declares, which it records as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. Refused, applying nothing, for an invalid file, a file that changes a line's model or strategy, one whose roles conflict with the log, one that changes the sandbox, the engine settings, the teacher or the external agent policy, which apply only when the daemon restarts, one whose disclosure list cannot be read, or one that requires a sandbox a line lacks |
| `roles` | Optionally `role_id`. Returns `roles`, every role by identifier or only the one named, each as the `role_status` tool of [`LINE_CONFIG.md`](LINE_CONFIG.md) gives it without `you_own` and `you_may_own`. Refused for an unknown role |
| `role_entry` | Argument `entry_id`. Returns the journal entry with its `role_id`, `entry_id`, `line_id`, `kind`, `workdir_commit`, `daemon_commit`, `noted_at`, `chars` and `body`, since `roles` gives the body of the newest entry only. Refused for an unknown entry |
| `role_take` | Arguments `role_id`, `seat` and `operator`. Gives the role to the seat, recorded as `ownership_changed` with cause `operator`, registering the operator if new. The seat is awakened and sent a note from the operator, and its next turn leads with the recovery checklist. Returns `role_id`, `owner`, `previous` and, when the former owner's kept engine session runs background tasks, the warning `stop` gives about them. Refused, recording nothing, while the owner has a turn in flight, which an operator interrupts first, for a seat that owns the role already, is not a seat of it, may not own it or is stopped, and for an unknown role. Operator only, and not offered to external agents |
| `role_vacate` | Arguments `role_id` and `operator`. Leaves the role vacant with cause `operator`, as `role_take` does otherwise, and refused as it is, or when the role is vacant already. Operator only |
| `role_handoff` | Arguments `role_id`, `to` and `operator`. Asks the role's owner, in priority mail from the operator, to hand the role to `to` with `relinquish_ownership`, and gives the handoff note's template. Changes no owner. Returns `role_id`, `owner`, `to`, `message_id` and `owner_hibernated`, true when the owner reads it only once awakened. Refused, sending nothing, for a vacant role and for a `to` that owns the role, is not a seat of it, may not own it or is stopped. Operator only |
| `budgets` | No arguments. Returns `providers`, the names a budget may be stated for, which until the budget step are the engines, and `budgets`, each provider's latest statement with `provider`, `remaining_percent`, `resets_at`, `source`, `note`, `stated_at` and `spent`, true when it is 0 percent and its reset, if it names one, has not passed |
| `budget_set` | Arguments `provider`, and optionally `remaining_percent`, a whole number from 0 to 100, `resets_at`, a time of the form `YYYY-MM-DDTHH:MM:SSZ`, and `note`, at least one of the first two. Records `budget_stated` with source `operator` and the daemon's time, replacing the provider's earlier statement, and returns it as `budgets` gives one, without `spent`, with `followed`, a list of sentences saying what the statement set off for each role whose owner runs on the provider, which is a failover when it shows the provider spent, or a request to the owner to prepare a handoff when it is below the role's threshold. Refused for a provider no engine is. Operator only |
| `interrupt` | Argument `line_id`. Stops the line's turn in flight, as the interrupt paragraph below specifies. Returns `line_id` and `interrupted`, false when no turn was in flight, in which case nothing changes. Refused for an unknown line |
| `release` | Argument `line_id`. Releases a line held at a conversation limit and resets its counts |
| `hibernate` | Argument `line_id`. Hibernates the line, recorded as `line_hibernated`, so it starts no turn until awakened, as [`PRIORITY.md`](PRIORITY.md) specifies. A turn in flight finishes. Returns `line_id`, `hibernated`, true, `changed`, false when the line was hibernated already, in which case nothing is recorded, and `running`, whether a turn is in flight. Refused for an unknown line. Operator only, and not offered to external agents |
| `awaken` | Argument `line_id`. Ends the hibernation, recorded as `line_awakened`. Returns `line_id`, `hibernated`, false, and `changed`, false when the line was not hibernated. Refused for an unknown line. Operator only |
| `agent_add` | Arguments `agent` and `access`. Registers an external agent, or replaces its access and token. Returns `agent`, `access`, `effective_access` under the policy's ceiling, `token`, shown only here, and `listening` |
| `agent_revoke` | Argument `agent`. Revokes it |
| `agent_list` | No arguments. Returns `policy`, with `listen`, `anonymous` and `max_access`, and `agents`, each with `agent`, `access`, `token` and `revoked` |
| `answer` | Arguments `question_id`, `body` and `operator`. Records the operator's reply, which must come from the operator the question was asked of. Result `{"message_id": ...}`. The answer wakes the asking line in a later tick |
| `send` | Arguments `recipient`, a line or `@operator`, `body`, `operator`, and optionally `reply_to` and `priority`, a boolean, default false. Registers the operator if new and records a note from them. With `priority` true the note is priority mail, as [`PRIORITY.md`](PRIORITY.md) specifies, which must go to a line and which for an operator is unlimited. Result `message_id` and `priority`. A refused priority note records nothing and registers no operator |

`status` also returns `tick`, the ticks run in this daemon's lifetime, and `lines`, one
entry per line with `line_id`, `engine`, `strategy`, `state`, `condition`, as
`line_status` gives it, `hibernated`, `pending`, the count of
undelivered messages, `turns`, the turns run in this daemon's lifetime,
`tool_policy`, `autonomous_turns`, the turns in a row without operator mail,
`cost_estimate_usd`, a decimal string, or `null` when no turn since the line's release
reported a cost and some reported none, `cost_unknown_turns`, how many turns since the
release reported no cost, so a client keeps an unknown cost apart from zero, `held_by`,
the question holding the line or `null`, `scheduled_wake`, the line's pending scheduled wake as `due`, its due time, and
`message_id`, the identifier its note will carry, or `null`, and `leases`, the leases
the line holds, each with `name` and `expires`, its end as a due time. The note itself
is not shown. `line_view` returns the same entry as `line`.

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
| `dromon start` | Starts a detached `dromond` in a new session and waits for it to answer `ping`, up to 60 seconds or the `--wait` seconds given, above 0 and at most 600. On a loaded host the daemon was observed still starting after 10 seconds. Succeeds without action when a daemon already answers | 0, or 1 on failure |
| `dromon status` | Prints the `status` result, with one row per line giving its condition, its pending mail, and a hibernation or hold its condition does not already name | 0, 4 when no daemon answers, or 1 |
| `dromon stop` | Sends `stop` and waits up to 10 seconds for the socket to disappear | 0, 4 when no daemon answers, or 1 |
| `dromon restart [--interrupt] [--wait-seconds S] [--end-tasks]` | Checks that the installed daemon imports, by running it with `--version`, and stops nothing if it does not. Sends `restart`, then polls `status` until a daemon with another `pid` answers, and prints its commit and the commit before. Fails if the restart is abandoned or no successor answers within the wait and 70 seconds more | 0, 4, or 1 |
| `dromon create LINE` | Sends `create_line`, with `--model`, default `claude-haiku-5-5`, `--strategy`, default `subscription`, `--session`, an engine session to continue, which must have been started in the line's working directory, and the tool options below | 0, 4, or 1 |
| `dromon configure LINE` | Sends `configure_line` with the tool options below. No option means the default configuration | 0, 4, or 1 |
| `dromon send RECIPIENT BODY` | Sends `send`, with `--operator`, default the login name, `--reply-to`, and `--priority`, which sends priority mail. Prints `message_id` and the identifier, and then `priority mail` on a second line only for priority mail | 0, 4, or 1 |
| `dromon questions` | Sends `questions` for `--operator`, default the login name, or for every operator with `--all`, and prints each question with its choices | 0, 4, or 1 |
| `dromon mail` | Sends `mail`, with `--operator`, `--unread`, `--from ADDRESS`, `--offset N` and `--limit N`, default 20 | 0, 4, or 1 |
| `dromon read MESSAGE` | Sends `read` and prints the message | 0, 4, or 1 |
| `dromon ack MESSAGE` | Sends `acknowledge` | 0, 4, or 1 |
| `dromon sent` | Sends `sent`, with `--operator`, `--unanswered`, `--offset N` and `--limit N`, and prints each message's fate | 0, 4, or 1 |
| `dromon attach LINE` | Opens an interactive client attached to the line, in the terminal or in a Tk window with `--gui`, speaking for `--operator` and refreshing every `--poll-seconds`, default 1, and four times as often while the line runs a turn. Text sends a message. The commands, keys and panes are those of the attached client section below. Any number may attach at once | 0, or 2 on a poll interval that is not positive |
| `dromon release LINE` | Sends `release` | 0, 4, or 1 |
| `dromon interrupt LINE` | Sends `interrupt`, as Escape does in an attached client, and prints whether a turn was in flight. A line with none is not a failure, since its turn may have ended as the request was made | 0, 4, or 1 |
| `dromon hibernate LINE` | Sends `hibernate`, and says whether a turn in flight finishes first | 0, 4, or 1 |
| `dromon awaken LINE` | Sends `awaken`, and says whether the line was hibernated | 0, 4, or 1 |
| `dromon rebind LINE` | Sends `rebind_line`, with `--model` and `--strategy`, both required | 0, 2, 4, or 1 |
| `dromon settings [LINE]` | Sends `settings` and prints the project's settings, and the line's when given, each with its layer | 0, 4, or 1 |
| `dromon set KEY [VALUE...]` | Checks the value against `settings` as the attached client does, then sends `set_setting`. A line key needs `--line LINE`, and a project key is written with the `project.` prefix. `--unset` removes the override instead | 0, 2, 4, or 1 |
| `dromon reload` | Sends `reload_project` | 0, 4, or 1 |
| `dromon role [status] [ROLE]` | Sends `roles` and prints each role with its owner, the last change, each seat with its condition, model, budget and commit, and the latest journal entries | 0, 4, or 1 |
| `dromon role entry ENTRY` | Sends `role_entry` and prints the entry's stamp and then its body | 0, 2, 4, or 1 |
| `dromon role take ROLE --seat SEAT`, `dromon role vacate ROLE` | Sends `role_take` or `role_vacate` for `--operator`, default the login name, and prints the change. Without `--seat`, `take` is a usage error | 0, 2, 4, or 1 |
| `dromon role handoff ROLE --to SEAT` | Sends `role_handoff` for `--operator` and prints the request's message identifier, and says so when the owner is hibernated | 0, 2, 4, or 1 |
| `dromon budget [list]`, `dromon budget set PROVIDER` | Sends `budgets` and prints each statement and the providers, or sends `budget_set` with `--percent N`, `--resets-at TIME`, at least one of them, and `--note TEXT`, and prints the statement and each sentence of `followed` | 0, 2, 4, or 1 |
| `dromon answer QUESTION BODY` | Sends `answer`, with `--operator`, default the login name | 0, 4, or 1 |
| `dromon probe` | Runs one short turn without the daemon on the engine of `--model`, and reports the driver, the engine, the credential and the capabilities the engine reports | 0 or 1 |
| `dromon agent add NAME --access LEVEL`, `dromon agent list`, `dromon agent revoke NAME` | Sends `agent_add`, `agent_list` or `agent_revoke`. `add` prints the token once with an MCP configuration for the agent | 0, 2, 4, or 1 |
| `dromon mcp --agent NAME` or `--line LINE` | Serves one external agent over MCP on standard input and output, reading its token from `DROMON_AGENT_TOKEN`, as [`AGENTS.md`](AGENTS.md) specifies, or one line's own tools to its engine through `lines.sock`, reading the line's token from `DROMON_LINE_TOKEN` | 0, or 1 on an oversized message |
| `dromon teacher [up\|status\|down]` | Brings the project's teacher up, reports it, or releases it, without the daemon, as [`TEACHER.md`](TEACHER.md) specifies. The default is `status`. `--force` loads the model when too little memory is available | 0, or 1 on failure or when the project declares no teacher |
| `dromon config` | Validates `.dromon/dromon.toml` without a daemon, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, and prints its limits, wake policy and lines | 0, or 1 on an invalid file |
| `dromond` | Runs the daemon in the foreground, after applying `.dromon/dromon.toml`. Each flag overrides the file. `--max-autonomous-turns N`, default 12, `--max-cost-usd X`, default 2, and `--cooldown-seconds S`, default 0, set the conversation limits, `--operator NAME`, default `operator`, names who hears about a held line no operator has messaged, `--hold-wakes` records messages without waking any line, and `--tick-seconds S` sets the time between ticks while work is in flight, default 1, at most 60 | 0, 1 on failure, 2 on a bad cap or cadence, or 5 when another daemon holds the lock |

The tool options are `--tool NAME`, repeatable, `--mcp-config FILE`, a JSON file of MCP
servers that the client reads, `--allow-tool NAME`, repeatable, `--allow-all-tools`,
`--inherit-account-mcp`, `--sandbox-config FILE`, a JSON file holding a Claude Code
sandbox object or a settings file whose `sandbox` key is taken, as
[`SANDBOX.md`](SANDBOX.md) specifies, `--settings-config FILE`, a Claude Code settings
file whose permissions, hooks, `env`, attribution and sandbox are taken, and
`--effort LEVEL`, both as [`SETTINGS.md`](SETTINGS.md) specifies, and
`--instructions FILE`, repeatable, `--claude-code-prompt`, and `--workdir DIR`, a
directory the daemon resolves against the project root, as
[`LINE_CONFIG.md`](LINE_CONFIG.md) specifies.

Status 2 is a usage error and status 3 marks a subcommand not yet implemented.

## Line activity

The daemon keeps, in memory and per line, a bounded tail of what the line's engine does
during its turns, so that an attached client can show a turn as it runs. The tail is
not part of the event log, is not persisted, and is lost when the daemon stops.

| Kind | Text |
|---|---|
| `turn_started` | The prompt the line received, which holds every message delivered into the turn |
| `text` | Text the engine wrote |
| `thinking` | Empty. The engine reasoned, and reasoning is not shown |
| `tool` | The name of the tool the engine called |
| `turn_ended` | How the turn ended. The engine's result subtype `success`, another subtype with the reason when its driver interrupted it, or with `ended at a safe point for` and the priority note when priority mail ended it, `failed: the engine ended the turn as` and any other subtype, `failed:` and the error, `interrupted by an operator`, or `cancelled` at shutdown |
| `notice` | Dromon's own remark on the turn, such as that the engine's start timed out at its initialize request and was tried once more. **Fact, read from Agent SDK 0.2.174 and tested.** The client's failed connect disconnects before it raises, which ends the first engine process before the second start |

Each entry has an `entry_id`, which never changes, and a `seq`, the daemon-wide revision
at which it last changed. Only a line's newest entry changes, when an engine that
streams its text sends another chunk, or reasons again, so a client that asks for
entries after the highest `seq` it holds and replaces entries by `entry_id` stays exact.
A `seq` lower than one the client holds means the daemon restarted, and the client
starts again from 0. A line keeps at most 256 entries and an entry at most 4,000
characters, the rest replaced by ` [truncated]`.

The Claude Code and Codex adapters report each text message whole, and Codex reports
it only once it completes. The Grok adapter reports chunks as they stream.

An interrupt cancels the turn's task, which closes the engine's session or subprocess
wherever it is. Mail the turn had received stays delivered. The engine session resumes
from what the engine itself recorded, which may include part of the cancelled turn. An
interrupted turn produces no turn report, so it counts toward no limit.

**Row 1 of the T3 Code review, decided by the operator on 2026-10-26.** A stop holds.
An interrupt that cancels a turn also withdraws the line's pending scheduled wake,
recorded as `wake_withdrawn`, and holds the line with an operator question, as a limit
does, unless it is held already. So no wake, task note or mail starts its next turn
until an operator answers the question or releases the line. Once the stopped turn has
ended, a kept engine connection whose interrupt did not settle, or settled while the
engine still runs background work, is closed, which ends the engine and, by the line's
stop policy, what it started, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. The
line's activity then says so in a `notice`.

## The attached client

The terminal and the window run one client core and differ only in how they read keys
and paint rows. The layout, from the top, is the transcript of the chosen pane, a card
for the line's open questions, a status bar, the input, and suggestions or a hint.

| Pane | Shows |
|---|---|
| Activity, the default | The line's activity entries, with fenced code and headings marked, and a spinner while a turn runs |
| Mail | The line's recent messages, as `line_view` returns them |

| Command | Effect |
|---|---|
| Plain text | Sends a message to the line |
| `/answer QUESTION TEXT` | Answers one of the line's questions |
| `/ack MESSAGE` | Acknowledges a message the line sent the operator |
| `/release` | Releases the line from a hold |
| `/interrupt` | Sends `interrupt`, as Escape does while a turn runs |
| `/config` | Opens the settings panel, the line's settings and then the project's, each with its layer. Enter on a row fills the input with the `/set` that edits it |
| `/model [MODEL]`, `/effort [LEVEL]` | Sets the line's model or reasoning effort, or without an argument opens the settings panel at that row |
| `/set KEY VALUE` | Sets a line setting, or a project setting written as `project.KEY`. A list is written with commas or spaces, `none` for an empty one, and a boolean as `on` or `off` |
| `/unset KEY` | Removes the override, so the layers below decide again |
| `/clear` | Sends `clear_session` |
| `/create LINE MODEL [STRATEGY]` | Sends `create_line`, with the subscription strategy by default |
| `/reload` | Sends `reload_project` |
| `/line LINE`, `/lines` | Attaches to another line, or lists the daemon's lines to pick one |
| `/activity`, `/mail` | Chooses the pane |
| `/help`, `/?` | Lists commands and keys |
| `/quit`, `/exit` | Detaches |

| Key | Effect |
|---|---|
| Enter | Sends the input, or takes the highlighted suggestion and sends it when it is complete |
| Shift or Alt with Enter, Control-J in the window, Escape then Enter in the terminal, or a trailing backslash | Breaks the input into rows |
| Tab, Shift-Tab | Completes a command, a line, a question, a message or an offered choice, or moves through the suggestions |
| Up, Down | Moves between input rows, or walks the input history of up to 200 entries |
| Control-A, Control-E, Control-B, Control-F | Moves to the start or end of the row, or by a character |
| Control-U, Control-K, Control-W | Cuts before or after the cursor, or the word before it |
| PageUp, PageDown, the mouse wheel in the window | Scrolls the transcript. A reader scrolled back keeps their place as rows arrive |
| Escape | Closes a panel or the suggestions, or interrupts a running turn |
| Control-O | Switches between the activity and mail panes |
| Control-N, Control-P | Attaches to the next or previous line |
| Control-L | Refreshes now |
| Control-C | Clears the input, or detaches on a second press within two seconds |
| Control-D | Detaches when the input is empty |
| `?` on an empty input | Opens the help panel |

The client rings the platform's bell once when a question arrives while it is attached.
It replaces every control character in what it shows, except the line break, so that no
message body or engine output can drive the terminal.

## Settings

Settings have two scopes, the project and a line. Each value comes from a layer, and
the first layer that gives a value decides it.

| Scope | Layers, highest first |
|---|---|
| Project | The command line of `dromond`, the project override, `.dromon/dromon.toml`, the built-in default |
| Line | The line override, the line's configuration from the project file, its creation or `configure_line`, and then, for limits and the wake policy, the line's own tables in the project file and the project's value |

Overrides are what an operator sets from a client, recorded in the event log as
`line_overridden` and `project_overridden`, so they survive a restart. The daemon never
writes the project file, which stays the shared, reviewed layer. A reload or a restart
applies the file under the overrides. The command line wins over a project override,
as Claude Code's command line wins over its settings files, and `set_setting` reports
such a value as shadowed.

| Scope | Key | Kind |
|---|---|---|
| Line | `model` | A choice of the models on the line's engine. The engine session is resumed on the new model |
| Line | `effort` | A choice of `low`, `medium`, `high`, `xhigh` and `max` |
| Line | `builtin_tools`, `allowed_tools`, `instructions` | Lists, checked as in [`LINE_CONFIG.md`](LINE_CONFIG.md) |
| Line | `tool_policy` | `allowlist` or `allow_all` |
| Line | `inherit_account_mcp`, `claude_code_prompt` | Booleans |
| Both | `limits.max_autonomous_turns`, `wake.every_ticks` | Whole numbers |
| Both | `limits.max_cost_usd`, `limits.cooldown_seconds` | Decimal strings |
| Project | `daemon.operator` | An operator identifier |
| Project | `daemon.tick_seconds` | A decimal string |

An override document is a JSON object from keys to values, written canonically with
sorted keys. Each entry of a `settings` result has `key`, `kind`, one of `bool`, `int`,
`decimal`, `text`, `choice`, `list` and `info`, `summary`, `choices`, `value`, `source`
and `editable`. Entries of kind `info` cannot be changed from a client. They show a
line's engine, strategy, engine session, working directory and `project_settings`, its
MCP servers by name only, whether a sandbox or engine settings document is set, and the
project's file, declared lines, teacher, external agent policy,
`daemon.require_sandbox`, `daemon.disclosure_terms` by its path only, never its terms,
and its leases with their time to live.

A line's model shows the source `created`, or `rebound` once `rebind_line` has set its
current binding, unless an override decides it.

A client cannot change a line's strategy, because it decides whether usage is billed
per token, and such usage never starts from a one-word command. It cannot change MCP
servers, sandbox or engine settings documents, the teacher or the agent policy either,
because they are long documents that belong in a reviewed file. Nor can it change a
line's working directory, which moves the line to a new engine session, so it changes
only through `configure_line` or the project file. A stored override
that no longer applies, for example after an upgrade, is skipped and reported on
standard error once, rather than stopping the line or the daemon.

A change applies from the line's next turn, and a turn in flight keeps what it began
with. Limits, the wake policy, the operator and the tick cadence apply at once.

## Trust

Whoever can connect runs as the operator's user, so a request carries operator authority
and names, in `send`, the operator it speaks for. Lines never use the socket. They send
through their `send_message` tool, and the daemon fixes each tool's sender to its line.

The socket's access control is the runtime directory, which must be owned by the
current user with mode 0700. The daemon refuses to start otherwise and never changes an
existing directory's mode. The socket and the event log are created with mode 0600. Any
process running as the same user can connect, so the protocol treats every request as
untrusted input and checks it strictly. A peer credential check on each connection is
not yet implemented, because its interface differs between macOS and Linux. External
agents never use this socket. They reach the separate agent socket, whose requests are
authorized by agent and access level.

## Not yet specified

- Updates pushed to attached clients, which now poll, faster while a turn runs.
- Authentication of a client beyond the directory's permissions.
- Notifications pushed from the daemon to an attached client.
