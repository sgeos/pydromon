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
| Registry of open federation | `.local/state/dromon/registry` under the user's home, or `DROMON_REGISTRY_DIR`, an absolute path | Holds an entry for each running daemon whose project sets `local = "open"`, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies. A malformed override refuses the start |

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
| `status` | `daemon_version`, `protocol_version`, `pid`, `project`, `started_at`, from 2026-10-31 `options`, the `dromond` options the daemon's command line gave, which a restart's successor and its preflight take, as a list of strings, `daemon`, the object `line_status` gives, with the commit the daemon started from, `restart`, the draining restart or the last one abandoned or `null`, as [`RESTART.md`](RESTART.md) specifies, and `event_log` with `last_seq`, `lines`, `operators`, `open_questions`, and the log's size and growth, `events`, `bytes`, `since_start`, `events_per_hour`, `bytes_per_hour` and `compaction`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for `line_status`, where an unknown size is `null` and never zero, and `peers`, each declared peer's delivery with `peer`, `state`, `waiting`, `oldest`, `oldest_sent_at`, `last_contact`, `backoff_seconds`, `fault` and `faults`, as [`FEDERATION.md`](../architecture/FEDERATION.md) describes, and from 2026-10-28 `scheduler`, the scheduler's watch with `fault`, its last unexpected exception until a tick completes or `null`, and `faults`, the count since the daemon started, and from 2026-10-31 `relays`, the operators' relayer with `routed`, the routed operators, `delivered`, the notices relayed since the start, `unrouted`, the operators' undelivered messages with no route at the last pass, `failing`, and `failures`, at most 10 failing notices each with `message_id`, `operator`, `attempts`, `reason` and `retry_in_seconds`, as [`RELAY.md`](RELAY.md) specifies, or `null` before the relayer starts. From 2026-10-28 the optional argument `correlation`, an identifier, adds `correlation`, that key's totals as [`AGENTS.md`](AGENTS.md) specifies, and a key that is not an identifier is refused as `bad_request` |
| `stop` | `{"stopping": true}`, after which the daemon closes its connections, removes its socket and exits |
| `restart` | Arguments `operator`, and optionally `mode`, `wait` or `interrupt` with `wait` the default, `wait_seconds`, a whole number from 1 to 3,600 with 600 the default, and `end_tasks`, a boolean with false the default. Begins a restart to upgrade, as [`RESTART.md`](RESTART.md) specifies. Returns `restarting`, true, `mode`, `wait_seconds`, `running`, the lines with a turn in flight, `pid` and `commit`, the daemon's, and with `end_tasks` the warning `stop` gives about background tasks. Refused while a restart drains, and while kept engine sessions run background tasks without `end_tasks`. Operator only, and not offered to external agents |
| `create_line` | Arguments `line_id`, `model`, `strategy`, and optionally `config`, an object of [`LINE_CONFIG.md`](LINE_CONFIG.md), and `engine_session`, an existing engine session to continue, recorded as `line_session` right after `line_created` and checked as that event is. Result `{"line_id": ...}`. Refused when no packaged driver serves the model and strategy, when the configuration's `workdir` is not allowed, when the project requires a sandbox and the line would run on Claude Code without one, or when the line could write the worktree of a role another seat owns and is not a seat of it, as the one-writer rule of [`ROLES.md`](../architecture/ROLES.md) gives, added in the round 10 review |
| `configure_line` | Arguments `line_id` and `config`. Replaces the line's configuration from its next turn. A changed `workdir` also appends `line_session_cleared`. Refused when the `workdir` is not allowed, when the project requires a sandbox and the line would run on Claude Code without one, when the line owns a role and the `workdir` is not the role's worktree, saying to vacate the role or hand it over first, or when the line, with its overrides over the new configuration, could write the worktree of a role another seat owns and is not a seat of it, both added in the round 10 review |
| `questions` | Optionally `operator`. Result `{"questions": [...]}`, the open questions for that operator, or for all when omitted, oldest first, each with `question_id`, `line`, `operator`, `text` and `choices`. Listing marks each question delivered, because the client has shown it |
| `mail` | Arguments `operator`, and optionally `unread_only`, `sender`, `issues_only`, `offset` and `limit`, at most 50. `unread_only` selects the messages `unread` counts, which leave out the operator's own notes, from the round 6 review. `issues_only`, from 2026-10-31, selects the open issues, messages of kind `issue` the operator has neither replied to nor acknowledged, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. A message an operator's relay delivered is listed as any delivered message is, and stays unread until it is read. Delivers the operator's undelivered mail, then returns `total`, `unread`, `unanswered`, from 2026-10-30 `own_notes`, which is 0 for an operator since an operator cannot send itself a note, and `messages`, a page of headers with `message_id`, `sender`, `kind`, `reply_to`, `read` and `outcome`, one of `replied`, `acknowledged` and `unanswered`, oldest first. A note the harness wrote has `sender` `!dromon`, from 2026-10-28, and may be acknowledged but not replied to. No argument of any request may name `!dromon`, `sender` included, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies |
| `replies` | From 2026-10-31, round 9. Arguments `operator` and `message_id`, which must name a message the log holds, and optionally `sender`, a line address, `unread_only`, `offset` and `limit`, at most 50. Delivers the operator's undelivered mail, as `mail` does, which appends `message_delivered` events, so the query is not read-only in the log, then returns `message_id`, `replies`, the count of messages in the operator's mailbox whose `reply_to` is that message, `unread`, how many of those are unread, `total`, how many match the filters, `left_out`, how many entries of the page did not fit, and `messages`, a page of them oldest first with `message_id`, `sender`, `kind`, `read`, `outcome`, `body` and `body_bytes`. The optional `sender` filters by line. Marks nothing read, so a wait loop can count unread replies and read them in one query. Each entry is counted against a 40,000 byte budget as written in JSON, its fields as well as its body, with the echoed `message_id`, so the answer fits one protocol line whatever its fields hold, as round 9's review, F13, asked. A body past what is left is `null`, with its size in `body_bytes`, for `read` to open. An entry whose fields alone do not fit is left out with every entry after it, and counted in `left_out`, so `offset` plus the entries given starts the next page. A `message_id` the log does not hold is refused as `no message`, which tells an operator whether an identifier exists. **Accepted by the operator in the review, F12,** since identifiers are random and the socket is the operator's own |
| `mail` | Arguments `operator`, and optionally `unread_only`, `sender`, `issues_only`, `offset` and `limit`, at most 50. `unread_only` selects the messages `unread` counts, which leave out the operator's own notes, from the round 6 review. `issues_only`, from 2026-10-31, selects the open issues, messages of kind `issue` the operator has neither replied to nor acknowledged, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. A message an operator's relay delivered is listed as any delivered message is, and stays unread until it is read. Delivers the operator's undelivered mail, then returns `total`, `unread`, `unanswered`, from 2026-10-30 `own_notes`, which is 0 for an operator since an operator cannot send itself a note, and `messages`, a page of headers with `message_id`, `sender`, `kind`, `reply_to`, `read` and `outcome`, one of `replied`, `acknowledged` and `unanswered`, oldest first. A note the harness wrote has `sender` `!dromon`, from 2026-10-28, and may be acknowledged but not replied to. No argument of any request may name `!dromon`, `sender` included, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies |
| `replies` | From 2026-10-31, round 9. Arguments `operator` and `message_id`, which must name a message the log holds, and optionally `unread_only`, `offset` and `limit`, at most 50. Delivers the operator's undelivered mail, as `mail` does, which appends `message_delivered` events, so the query is not read-only in the log, then returns `message_id`, `replies`, the count of messages in the operator's mailbox whose `reply_to` is that message, `unread`, how many of those are unread, `total`, how many match `unread_only`, `left_out`, how many entries of the page did not fit, and `messages`, a page of them oldest first with `message_id`, `sender`, `kind`, `read`, `outcome`, `body` and `body_bytes`. Marks nothing read, so a wait loop can count unread replies and read them in one query. Each entry is counted against a 40,000 byte budget as written in JSON, its fields as well as its body, with the echoed `message_id`, so the answer fits one protocol line whatever its fields hold, as round 9's review, F13, asked. A body past what is left is `null`, with its size in `body_bytes`, for `read` to open. An entry whose fields alone do not fit is left out with every entry after it, and counted in `left_out`, so `offset` plus the entries given starts the next page. A `message_id` the log does not hold is refused as `no message`, which tells an operator whether an identifier exists. **Accepted by the operator in the review, F12,** since identifiers are random and the socket is the operator's own |
| `acknowledge` | Arguments `operator` and `message_id`. Marks a message in the operator's mailbox dealt with, without a reply |
| `sent` | Arguments `operator`, and optionally `unanswered_only`, `offset` and `limit`. Returns `total` and `messages`, the operator's sent mail with `message_id`, `recipient`, `kind`, `delivered`, `read`, `outcome` and `delivery`. `delivery` is `waiting`, `delivered` or `read` for local mail, and `waiting for peer`, `delivered to peer`, `returned` with its code, or `unconfirmed` for mail to a peer, added on 2026-10-28 |
| `read` | Arguments `operator` and `message_id`. Returns the message with its `body` and `choices`, and marks it read the first time |
| `line_view` | Arguments `line_id`, and optionally `limit`, at most 50. Returns `line`, the line's status entry, `tick`, `total`, the count of messages involving the line, `messages`, the newest of them oldest first, each with a body preview of at most 600 characters and `truncated`, kept within a 40,000 byte budget, and `open_questions`, the line's open questions. What an attached client polls |
| `line_activity` | Arguments `line_id`, and optionally `after`, a revision, default 0. Returns `line_id`, `seq`, the daemon's newest revision, `entries`, the line's activity entries changed after `after`, oldest first, the newest kept within a 40,000 byte budget, and `complete`, false when older changed entries were left out. Each entry has `entry_id`, `seq`, `kind` and `text`, as the activity section below specifies. What an attached client polls to follow a turn |
| `settings` | Optionally `line_id`. Returns `project`, the project's settings, `models`, every model a line may be created on, and with `line_id` also `line_id` and `line`, the line's settings, each as the settings section below specifies |
| `set_setting` | Arguments `scope`, `line` with `line_id` or `project` without it, `key`, and optionally `value`. Records the value as an override, or removes the key's override when `value` is omitted. Returns `scope`, `line_id` for a line, `key`, `value` and `source`, the value now in force and its layer, and `shadowed`, true when a value was set but a higher layer still decides. Refused, recording nothing, for an unknown key or a value its key or its meaning does not accept, such as a model or a tool that would let a line that is not a seat of a role write the worktree of a role another seat owns, added in the round 10 review |
| `clear_session` | Argument `line_id`. The line's next turn starts a new engine session, and the leases it holds end. Refused while the line runs a turn |
| `rebind_line` | Arguments `line_id`, `model` and `strategy`. Moves the line to another model and driver strategy, recorded as `line_rebound`. The line keeps its identity, mailbox, configuration and overrides, except a model override, which is dropped with a `line_overridden`. Its engine session is cleared, its engine rebuilt and its leases end. Result `{"line_id": ..., "model": ..., "strategy": ...}`. Refused while the line runs a turn, for a model and strategy with no packaged driver or adapter, for the binding the line already has, for a configuration or override the new engine cannot run, as `create_line` checks one, for a move onto Codex or Grok while the project's own sandbox or settings would reach the line, for a move onto Claude Code without a sandbox the project requires, and for a move onto an engine on which the line could write the worktree of a role another seat owns without being a seat of it, added in the round 10 review. Operator only, and not offered to external agents |
| `reload_project` | No arguments. Reads `.dromon/dromon.toml` again and applies its lines, limits, wake policy, operator, tick cadence, sandbox requirement and disclosure list, which it reads again. Returns `file`, `present`, `lines`, the lines it declares, `roles`, the roles it declares, which it records as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, and `role_notices`, a sentence for each role the file leaves as it is, such as one an operator changed with `role_update`, added in round 10. Refused, applying nothing, for an invalid file, a file that changes a line's model or strategy, one whose roles conflict with the log, one that would leave two roles on one worktree, an owning seat outside its role's worktree, or a line that is not a seat writing an owned role's worktree, as the one-writer rule of [`ROLES.md`](../architecture/ROLES.md) gives, added in the round 10 review, one that changes the sandbox, the engine settings, the teacher or the external agent policy, which apply only when the daemon restarts, one whose disclosure list cannot be read, one that requires a sandbox a line lacks, or from 2026-10-31 one whose file relay's notice file lies in a line's working directory. Its relays and routes apply from the relayer's next pass |
| `roles` | Optionally `role_id`. Returns `roles`, every role by identifier or only the one named, each as the `role_status` tool of [`LINE_CONFIG.md`](LINE_CONFIG.md) gives it without `you_own` and `you_may_own`. Each role also gives `held_by`, `file` or `operator`, `held`, the latest `role_held` as `by` and `at` or `null`, and `order`, its declared failover order, so every term `role_declared` holds is shown. Without `role_id` it also returns `retired`, each retired role with `role_id`, `by` and `retired_at`. Refused for an unknown role, saying when a retired one was retired. The last three fields added in round 10. Each role also gives `in_project_file`, whether the project file has a table for it, `file_differences`, a sentence for each term and seat of a role an operator holds that differs from that table, empty otherwise, and `beside_owner`, the lines that wait because they could write the role's worktree while another seat owns it and are not seats of it, added in the round 10 review |
| `role_entry` | Argument `entry_id`. Returns the journal entry with its `role_id`, `entry_id`, `line_id`, `kind`, `workdir_commit`, `daemon_commit`, `noted_at`, `chars` and `body`, since `roles` gives the body of the newest entry only. Refused for an unknown entry |
| `role_take` | Arguments `role_id`, `seat` and `operator`. Gives the role to the seat, recorded as `ownership_changed` with cause `operator`, registering the operator if new. The seat is awakened and sent a note from the operator, and its next turn leads with the recovery checklist. Returns `role_id`, `owner`, `previous` and, when the former owner's kept engine session runs background tasks, the warning `stop` gives about them. Refused, recording nothing, while the owner has a turn in flight, which an operator interrupts first, for a seat that owns the role already, is not a seat of it, may not own it or is stopped, and for an unknown role. Operator only, and not offered to external agents |
| `role_vacate` | Arguments `role_id` and `operator`. Leaves the role vacant with cause `operator`, as `role_take` does otherwise, and refused as it is, or when the role is vacant already. Operator only |
| `role_create` | Arguments `role_id`, `operator`, `workdir` and `seats`, a list of lines in failover order, and optionally `no_own`, the seats that may not own the role, `handoff_seconds`, `failover` and `handoff_below_percent`, defaulting as the project file's do. Records `role_declared`, a `seat_bound` for each seat and `role_held` with `operator`, registering the operator if new. The role starts vacant. Returns `role`, the role as `roles` gives it, `bound`, the lines made seats, and `unbound`, empty, with the warning `stop` gives when a kept engine session of theirs runs background tasks. Refused, recording nothing, for a role that exists, a retired identifier, which is never declared again, a line that does not exist, since no line is created, a seat that does not run in the worktree, since no line's configuration changes, a line that is a seat of another role, and no seats or more than 16. Operator only, and not offered to lines or external agents. Added in round 10 |
| `role_update` | Arguments `role_id` and `operator`, and any of the arguments of `role_create`, and `may_own`, the seats that may own the role again. A term given replaces the role's, and `seats` replaces the seats and the order, so it adds, removes and reorders them. A seat it keeps keeps its `may_own` unless `no_own` or `may_own` names it. Records a `seat_unbound` for each removed seat, `role_declared` when a term changes, a `seat_bound` for each new seat or changed `may_own`, and `role_held` with `operator` when the file held the role. Returns as `role_create` does. Refused, recording nothing, as `role_create` is for its seats, for a change that changes nothing, for removing the owner's seat or barring the owner from owning, which says to vacate the role or hand it over first, for moving an owned role's worktree, and for removing a seat of an owned role whose line runs in the worktree, could write it and is neither hibernated nor stopped, which says to hibernate it first. Operator only. Added in round 10 |
| `role_delete` | Arguments `role_id` and `operator`. Records a `seat_unbound` for each seat and then `role_retired`. The seats stay lines, with their mail, leases and state, and the journal stays readable with `role_entry`. Returns `role_id`, `unbound`, the seats unbound, `retired_at`, and the warning `stop` gives about their kept sessions' background tasks. Refused, recording nothing, for an owned role, which says to vacate it or hand it over first, and for an unknown or retired role. Operator only. Added in round 10 |
| `role_handoff` | Arguments `role_id`, `to` and `operator`. Asks the role's owner, in priority mail from the operator, to hand the role to `to` with `relinquish_ownership`, and gives the handoff note's template. Changes no owner. Returns `role_id`, `owner`, `to`, `message_id` and `owner_hibernated`, true when the owner reads it only once awakened. Refused, sending nothing, for a vacant role and for a `to` that owns the role, is not a seat of it, may not own it or is stopped. Operator only |
| `run_clear` | Arguments `run_id` and `operator`. Records `run_cleared` with cause `operator` and the operator, registering the operator if new, so the lost run no longer blocks its scope and the scope's holder is sent a note from the harness. The operator inspects what is left of the run's command first, since it may still run, as [`RUNS.md`](../architecture/RUNS.md) says. Returns `run_id`, `operator` and `cleared_at`. Refused, recording nothing, for an unknown run, a run that is not lost and one cleared already. Operator only, and not offered to lines or external agents. Added on 2026-10-31 |
| `budgets` | No arguments. Returns `providers`, the names a budget may be stated for, which until the budget step are the engines, and `budgets`, each provider's latest statement with `provider`, `remaining_percent`, `resets_at`, `source`, `note`, `stated_at` and `spent`, true when it is 0 percent and its reset, if it names one, has not passed |
| `budget_set` | Arguments `provider`, and optionally `remaining_percent`, a whole number from 0 to 100, `resets_at`, a time of the form `YYYY-MM-DDTHH:MM:SSZ`, and `note`, at least one of the first two. Records `budget_stated` with source `operator` and the daemon's time, replacing the provider's earlier statement, and returns it as `budgets` gives one, without `spent`, with `followed`, a list of sentences saying what the statement set off for each role whose owner runs on the provider, which is a failover when it shows the provider spent, or a request to the owner to prepare a handoff when it is below the role's threshold. Refused for a provider no engine is. Operator only |
| `interrupt` | Argument `line_id`. Stops the line's turn in flight, as the interrupt paragraph below specifies. Returns `line_id` and `interrupted`, false when no turn was in flight, in which case nothing changes. Refused for an unknown line |
| `release` | Argument `line_id`. Releases a line held at a conversation limit and resets its counts |
| `hibernate` | Argument `line_id`. Hibernates the line, recorded as `line_hibernated`, so it starts no turn until awakened, as [`PRIORITY.md`](PRIORITY.md) specifies. A turn in flight finishes. Returns `line_id`, `hibernated`, true, `changed`, false when the line was hibernated already, in which case nothing is recorded, and `running`, whether a turn is in flight. Refused for an unknown line. Operator only, and not offered to external agents |
| `awaken` | Argument `line_id`. Ends the hibernation, recorded as `line_awakened`. Returns `line_id`, `hibernated`, false, and `changed`, false when the line was not hibernated. Refused for an unknown line. Operator only |
| `agent_add` | Arguments `agent` and `access`. Registers an external agent, or replaces its access and token. Returns `agent`, `access`, `effective_access` under the policy's ceiling, `token`, shown only here, and `listening` |
| `agent_revoke` | Argument `agent`. Revokes it |
| `agent_list` | No arguments. Returns `policy`, with `listen`, `anonymous` and `max_access`, and `agents`, each with `agent`, `access`, `token` and `revoked` |
| `peer_token` | Argument `peer`, a peer the log holds as declared. Issues the token the peer presents to this daemon, replacing any earlier one, records its digest with `peer_keyed`, and returns `peer` and `token`. The token is returned once. Operator only, and not offered to external agents. Added on 2026-10-28 |
| `peer_revoke` | Argument `peer`. Records `peer_keyed` with an empty digest, so the peer's token no longer works, and returns `peer` and `revoked`. Refused when no token is issued. Operator only, and not offered to external agents. Added on 2026-10-28 |
| `peer_store` | Arguments `peer`, a peer the log holds as declared, and `token`, 16 to 256 letters, digits, `-` or `_`. Stores the token the peer issued for this daemon in `.dromon/peers/PEER.token`, mode 0600 in a directory of mode 0700, replacing any earlier one, and returns `peer` and `stored`, the file's path. A refusal never quotes the token. Operator only, and not offered to external agents. Added on 2026-10-28 |
| `peer_list` | No arguments. Returns `listen` and `peers`, each with `peer`, `transport`, `socket`, null for a `tls` peer, `address`, null for a `unix` peer, `inbound`, `outbound`, `issued`, whether a token is issued for it, and `stored`, whether one from it is stored, or the reason a stored file is refused. Never a token or a digest. Operator only, and not offered to external agents. Added on 2026-10-28 |
| `peer_key` | No arguments. Makes this daemon's Transport Layer Security identity once, an Ed25519 key and a self-signed certificate in `.dromon/peers`, each mode 0600 in a directory of mode 0700, and returns `pin_sha256`, the SHA-256 of its public key, `certificate`, the certificate's path, and `made`, whether it was made by this call. An existing identity is never replaced. A store holding only one of the two files, or a file that is not this user's with mode 0600, is refused. Never returns the private key. Operator only, and not offered to external agents. Added on 2026-10-28 |
| `answer` | Arguments `question_id`, `body` and `operator`. Records the operator's reply, which must come from the operator the question was asked of. Result `{"message_id": ...}`. The answer wakes the asking line in a later tick |
| `send` | Arguments `recipient`, a line, `@operator`, or from 2026-10-28 a remote address such as `keleusma:@ana` that the outbound policy allows, `body`, `operator`, and optionally `reply_to` and `priority`, a boolean, default false. Registers the operator if new and records a note from them. With `priority` true the note is priority mail, as [`PRIORITY.md`](PRIORITY.md) specifies, which must go to a line and which for an operator is unlimited. Mail to a peer is held to the disclosure list, and may be priority mail only to a peer's line and only when the peer's `priority_out` is true, from step 6 of federation phase 1. Result `message_id` and `priority`. A refused priority note records nothing and registers no operator |

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
the line holds, each with `name` and `expires`, its end as a due time, and from
2026-10-30 `unread`, `unanswered` and `own_notes`, the line's mail counts, as
[`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for its summary. From round 8 of
2026-10-30, `own_notes` leaves out an own note once a turn of the line has ended after
its delivery with a report from its engine. The note itself is not
shown. `line_view` returns the same entry as `line`.

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
- On the normal shutdown path, the process and sweep workers finish before the daemon
  returns. Each join is bounded to one second. If an in-flight process-table call
  exceeds that bound, shutdown reports the worker as still running and the daemon may
  return while its daemon thread finishes later.

## Commands

| Command | Behaviour | Exit status |
|---|---|---|
| `dromon start` | Starts a detached `dromond` in a new session and waits for it to answer `ping`, up to 60 seconds or the `--wait` seconds given, above 0 and at most 600. On a loaded host the daemon was observed still starting after 10 seconds. Succeeds without action when a daemon already answers | 0, or 1 on failure |
| `dromon status` | Prints the `status` result, with one row per line giving its condition, its pending mail, and a hibernation or hold its condition does not already name, and from 2026-10-31 a row for the operators' relays with one indented row for each failing notice, printed only when an operator is routed or mail waits unrouted. `--correlation KEY` adds a row with that key's turns, shared turns, known cost and turns of unknown cost | 0, 4 when no daemon answers, or 1 |
| `dromon proposal list` | Lists proposed changes recorded by lines | 0, or 1 on a read error |
| `dromon proposal show PROPOSAL_ID` | Shows a proposal's message, file list and patch as untrusted text | 0, or 1 when missing or invalid |
| `dromon stop` | Sends `stop` and waits up to 10 seconds for the socket to disappear | 0, 4 when no daemon answers, or 1 |
| `dromon restart [--interrupt] [--wait-seconds S] [--end-tasks]` | Reads `status`, then from 2026-10-31 runs the installed daemon's start checks in a process of its own, `dromond --check` with the running daemon's `options`, against the project file and a copy of the event log, as [`RESTART.md`](RESTART.md) specifies. When they fail it prints the new code's own refusal, sends nothing more and stops nothing. Otherwise it sends `restart`, then polls `status` until a daemon with another `pid` answers, and prints its commit and the commit before. Fails if the restart is abandoned or no successor answers within the wait and 70 seconds more | 0, 4, or 1 |
| `dromon land WORKTREE` | From 2026-10-31, needs no daemon. Takes a line's finished worktree to a pull request, with `--message` or `--message-file`, `--body` or `--body-file`, `--title`, default the message's first line, `--base`, default `main`, and `--remote`, default `origin`. It prints the plan with every path it would stage, and refuses on `main`, a detached HEAD, a rebase, an unmerged path, nothing to land, a `Co-Authored-By` trailer, or a path under `docs/process`, `docs/roadmap`, `secret` or `.dromon`, compared without regard to case, except that a merge may carry such a path unchanged from the incoming commit. It screens the diff, the message, the title and the body against `secret/disclosure-terms.txt` under the project, or each `--terms` given, honouring `secret/disclosure-accepted.txt`, and prints only file and line, refusing when no list is found unless `--without-disclosure-list` is given. `--dry-run` stops there. Otherwise it asks, stages exactly those paths, screens the index again, commits, checks the commit carries no trailer, runs `scripts/check.sh` in full, asks, pushes the commit to the branch with `--no-verify` and never by force, asks, and runs `gh pr create`. `--yes` answers every question, and a question with no terminal is answered no. It never merges | 0, 1, or 2 for a text file that cannot be read |
| `dromon create LINE` | Sends `create_line`, with `--model`, default `claude-haiku-5-5`, `--strategy`, default `subscription`, `--session`, an engine session to continue, which must have been started in the line's working directory, and the tool options below | 0, 4, or 1 |
| `dromon configure LINE` | Sends `configure_line` with the tool options below. No option means the default configuration | 0, 4, or 1 |
| `dromon send RECIPIENT BODY` | Sends `send`, with `--operator`, default the login name, `--reply-to`, and `--priority`, which sends priority mail. Prints `message_id` and the identifier, and then `priority mail` on a second line only for priority mail | 0, 4, or 1 |
| `dromon questions` | Sends `questions` for `--operator`, default the login name, or for every operator with `--all`, and prints each question with its choices | 0, 4, or 1 |
| `dromon mail` | Sends `mail`, with `--operator`, `--unread`, `--from ADDRESS`, `--issues`, `--offset N` and `--limit N`, default 20. `--issues`, from 2026-10-31, lists only the open issues. With `--replies-to MESSAGE`, from 2026-10-31, sends `replies` instead, with `--unread`, `--offset` and `--limit`, and prints each reply's header and body, marking none read, then how many did not fit and the `--offset` that gives them. `--from` does not combine with it | 0, 4, or 1, and 2 for `--from` with `--replies-to` |
| `dromon read MESSAGE` | Sends `read` and prints the message | 0, 4, or 1 |
| `dromon ack MESSAGE` | Sends `acknowledge` | 0, 4, or 1 |
| `dromon sent` | Sends `sent`, with `--operator`, `--unanswered`, `--offset N` and `--limit N`, and prints each message's fate | 0, 4, or 1 |
| `dromon attach LINE` | Opens an interactive client attached to the line, in the terminal or in a Tk window with `--gui`, speaking for `--operator` and refreshing every `--poll-seconds`, default 1, and four times as often while the line runs a turn. Text sends a message. The commands, keys and panes are those of the attached client section below. Any number may attach at once. From 2026-10-31, a client whose daemon is gone retries, and detaches by itself once at least 5 polls in a row have found none, over at least 10 seconds with its socket file missing or 60 seconds of refused connections, printing why on standard error. A daemon that is slow or refuses a request is still there, and never detaches a client | 0, 2 on a poll interval that is not positive, or 4 when it detached because the daemon was gone |
| `dromon release LINE` | Sends `release` | 0, 4, or 1 |
| `dromon interrupt LINE` | Sends `interrupt`, as Escape does in an attached client, and prints whether a turn was in flight. A line with none is not a failure, since its turn may have ended as the request was made | 0, 4, or 1 |
| `dromon hibernate LINE` | Sends `hibernate`, and says whether a turn in flight finishes first | 0, 4, or 1 |
| `dromon awaken LINE` | Sends `awaken`, and says whether the line was hibernated | 0, 4, or 1 |
| `dromon rebind LINE` | Sends `rebind_line`, with `--model` and `--strategy`, both required | 0, 2, 4, or 1 |
| `dromon settings [LINE]` | Sends `settings` and prints the project's settings, and the line's when given, each with its layer | 0, 4, or 1 |
| `dromon set KEY [VALUE...]` | Checks the value against `settings` as the attached client does, then sends `set_setting`. A line key needs `--line LINE`, and a project key is written with the `project.` prefix. `--unset` removes the override instead | 0, 2, 4, or 1 |
| `dromon reload` | Sends `reload_project`, prints its result, and prints each of its `role_notices` on a line of its own, beginning `notice:` | 0, 4, or 1 |
| `dromon role [status] [ROLE]` | Sends `roles` and prints each role with its owner, every term, the last change, who holds it and its failover order, each seat with its condition, model, budget and commit, and the latest journal entries, then each way the project file's table differs from a role an operator holds and each line that waits beside the owner, both added in the round 10 review, and then each retired role | 0, 4, or 1 |
| `dromon role entry ENTRY` | Sends `role_entry` and prints the entry's stamp and then its body | 0, 2, 4, or 1 |
| `dromon role take ROLE --seat SEAT`, `dromon role vacate ROLE` | Sends `role_take` or `role_vacate` for `--operator`, default the login name, and prints the change. Without `--seat`, `take` is a usage error | 0, 2, 4, or 1 |
| `dromon role create ROLE --workdir DIR --seats A,B` | Sends `role_create` for `--operator`, with `--no-own SEAT`, repeatable, `--handoff-seconds`, `--failover` and `--handoff-below-percent`, and prints the role and the lines made seats. Without `--workdir` or `--seats` it is a usage error, and so is `--may-own` | 0, 2, 4, or 1 |
| `dromon role update ROLE` | Sends `role_update` for `--operator`, with any option of `create` and `--may-own SEAT`, repeatable. `--seats` replaces the seats and the order. Prints the role, the lines made seats and the seats unbound, which are still lines | 0, 2, 4, or 1 |
| `dromon role delete ROLE` | Sends `role_delete` for `--operator` and prints when the role was retired and the seats unbound, which are still lines | 0, 2, 4, or 1 |
| `dromon role handoff ROLE --to SEAT` | Sends `role_handoff` for `--operator` and prints the request's message identifier, and says so when the owner is hibernated | 0, 2, 4, or 1 |
| `dromon run clear RUN` | Sends `run_clear` for `--operator`, default the login name, and prints when the run was cleared | 0, 2, 4, or 1 |
| `dromon budget [list]`, `dromon budget set PROVIDER` | Sends `budgets` and prints each statement and the providers, or sends `budget_set` with `--percent N`, `--resets-at TIME`, at least one of them, and `--note TEXT`, and prints the statement and each sentence of `followed` | 0, 2, 4, or 1 |
| `dromon answer QUESTION BODY` | Sends `answer`, with `--operator`, default the login name | 0, 4, or 1 |
| `dromon probe` | Runs one short turn without the daemon on the engine of `--model`, and reports the driver, the engine, the credential and the capabilities the engine reports | 0 or 1 |
| `dromon agent add NAME --access LEVEL`, `dromon agent list`, `dromon agent revoke NAME` | Sends `agent_add`, `agent_list` or `agent_revoke`. `add` prints the token once with an MCP configuration for the agent | 0, 2, 4, or 1 |
| `dromon agent send --agent NAME --to PARTY BODY` | Sends `agent_call` for the agent's `send_message` on the agent socket, not the operator socket, reading the token from `DROMON_AGENT_TOKEN`, as [`AGENTS.md`](AGENTS.md) specifies, and prints `sent as` and the message identifier. `--correlation KEY` gives the note a correlation key | 0, 2, 4, or 1 |
| `dromon peer list`, `dromon peer token NAME`, `dromon peer revoke NAME`, `dromon peer store NAME`, `dromon peer key` | Sends `peer_list`, `peer_token`, `peer_revoke`, `peer_store` or `peer_key`. `token` prints the token once. `key` prints the pin and the certificate's path, and takes no name. `store` reads the token from its standard input, never from an argument, so it does not appear in the process list or the shell's history | 0, 2, 4, or 1 |
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

**A daemon that is gone, round 9, 2026-10-31.** A client whose connection to the
daemon's socket fails shows that it is retrying, and detaches by itself once at least
`LOST_POLLS`, 5, polls in a row have failed so and one of two bounds has passed. Both
the count and the bound must pass, so a short poll interval does not detach it at once
and a long one still retries 5 times. The fetches of one poll count once, and any
answer from the daemon starts the count again. A timeout or a request the daemon
refuses is a failure shown as before and never counts, since a daemon slow under load
is still there.

- **The socket file is missing.** The bound is `LOST_SECONDS`, 10 seconds, of polls in
  a row that found it missing, which only a stopped daemon or a deleted project leaves.
- **The connection is refused.** **Round 9 review, F14, decided by the operator.** The
  bound is `REFUSED_SECONDS`, 60 seconds, of failed polls in all, since on Darwin a
  connection to a Unix socket whose listen backlog is full is refused, so a live
  daemon whose event loop is busy may refuse several clients at once. A daemon that
  crashed leaves its socket file behind and refuses every connection, so its clients
  still detach, after a minute. A refusal starts the 10 second bound again, so it runs
  only over polls that all found the file missing.

The reason is
printed on standard error once the screen is restored, and `dromon attach` exits with
4. The terminal's loop also waits out the rest of its 50 millisecond input timeout
whenever curses returns at once without a key, as it does once the terminal is gone, so
that a pass never spins. **Reported by another project's operator.** One client had
polled a deleted project for days at full processor use. **Inference, not
reproduced.** The spin was the loop reading a terminal that was gone without waiting,
since a live terminal's read waits out the timeout, and the missing daemon kept it from
ever ending.

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
existing directory's mode. The socket and the event log are created with mode 0600.

**Peer credentials, since 2026-10-28.** Every socket the daemon serves, this one, the
line socket, the agent socket and the peer socket, reads the user of each connecting process from its
peer credentials, `LOCAL_PEERCRED` on macOS and `SO_PEERCRED` on Linux, and refuses a
process of another user with one `rejected` response whose identifier is empty, then
closes the connection and writes the refusal to standard error. A connection whose
credentials cannot be read is refused in the same way, and a platform that offers
neither interface refuses to start the daemon. The check proves only the user. Any
process running as the same user can still connect, a line's shell included when its
sandbox does not deny the runtime directory, so the protocol treats every request as
untrusted input and checks it strictly. External
agents never use this socket. They reach the separate agent socket, whose requests are
authorized by agent and access level.

## The peer socket

**Added on 2026-10-28, step 4 of federation phase 1.** A project whose
`[federation] listen` is true also serves `peers.sock` in the runtime directory, mode
0600, removed when the daemon stops. Peer daemons deliver mail on it, as
[`FEDERATION.md`](../architecture/FEDERATION.md) designs. It uses the framing above
with a line limit of 8,388,608 bytes, the bound of an encoded event, and serves only
`ping` and the two operations below. This socket, the agent socket and the line socket
answer every other operation `unknown_operation`, and the operator socket answers
`hello` and `deliver` the same way.

| Operation | Behaviour |
|---|---|
| `hello` | Arguments exactly `versions`, a list of 1 to 16 integers, `name`, an identifier the caller gives itself, advisory only, and `token`. Names the connection's peer by its token, the one `peer_token` issued, or, while `[federation] local` is open, without `token`, by the live registry entry of `name`, whose pid must be the connecting process's, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies under open federation, and returns `version`, the federation version chosen, today `1`. An undeclared peer and a wrong or revoked token get the one `rejected` refusal `the peer was refused`, written to standard error with the announced name. No version in common is `unsupported_version`. A second `hello` on one connection is `rejected` |
| `deliver` | Arguments exactly `origin_id`, the sending daemon's identifier for the message, `sender` and `recipient`, each a local address as its own daemon writes it, `kind`, `body`, `reply_to`, an identifier or `null`, and `priority`, a boolean. Needs a `hello` on the connection, and checks again that the peer's token is the one it presented. Returns `outcome` `accepted` with `message_id`, `duplicate` and `priority`, true when the note was recorded as priority mail, which a duplicate answers as it was first recorded, or `refused` with `code` and `retry` false, or `busy` with `retry_seconds`. Only `accepted` without `duplicate` records anything, one `message_received` |

The receiver's checks run in this order. Every `deliver` counts toward the peer's rate
limit of 60 in 600 seconds, past which it is `busy`. An envelope that does not have
exactly its keys, or whose addresses, identifiers or types do not follow the grammar,
is `malformed`. A resend under an origin identifier already recorded is answered
`accepted` with `duplicate` true when it is the same message, or `conflict` when it is
not, before the policy is read, so a policy changed since then cannot refuse mail that
was accepted. Then inbound off, a recipient outside `reach` or missing, and a question
are `refused` alike, a body over `max_body_bytes` in UTF-8 is `too_large`, a `reply_to`
that does not name a message from the recipient to the sender is `unknown_reply`, and
waiting mail at `waiting_per_recipient` or at the peer's `waiting` is `full`.

**Before `hello`.** **Decided by the operator on 2026-10-28, and built in step 5.** A
connection's first line must arrive within 10 seconds and hold at most 65,536 bytes,
and the connection is closed after its answer unless that line was an accepted
`hello`. A line past the bound is answered `too_long` and closed, and a connection that
sends nothing in time is closed without an answer. Only after `hello` may a line reach
the limit of 8,388,608 bytes, so a process that holds no token cannot make the daemon
buffer that much. The connection is also closed once a `deliver` finds its peer's token
revoked or replaced. The listener serves at most 16 connections at once, counting those
not yet past `hello`, and answers a further one `rejected` before closing it.

**The sending end.** **Added on 2026-10-28, step 5 of federation phase 1.** A daemon
sends its peer mail as a client of the peer's `peers.sock`, one `hello` per connection
and then one `deliver` at a time, as
[`FEDERATION.md`](../architecture/FEDERATION.md) specifies under delivery. Its
`hello` gives its project directory's name, lowercased, when that is an identifier the
disclosure list does not cover, and `dromon` otherwise. It records a message as
delivered only from the peer's `accepted` answer, never from a write that succeeded.

## Not yet specified

- Updates pushed to attached clients, which now poll, faster while a turn runs.
- Authentication of a client beyond the directory's permissions.
- Notifications pushed from the daemon to an attached client.
