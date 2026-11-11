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
| Model name | An identifier, or an engine-qualified name, an identifier, `:`, and 1 to 128 ASCII characters from `a` to `z`, `0` to `9`, `.`, `_`, `-` and `/`, starting with a letter or digit, such as `pi:openai/gpt-6-luna`. Compared bytewise. The identifier form alone refused the packaged Pi, Cursor Agent and Antigravity models until 2026-10-31 |
| Line address | A line's identifier, such as `alpha` |
| Operator address | `@` followed by an operator's identifier, such as `@ana`. `@` never appears in an identifier, so the two kinds of address cannot collide |
| Agent address | `&` followed by an external agent's identifier, such as `&claude-main`. `&` never appears in an identifier |
| Remote address | A peer's identifier, `:`, and a line, operator or agent address as that peer writes it, such as `keleusma:kaizen` or `keleusma:@ana`. It begins with an identifier, never with `@` or `&`, and holds exactly one `:`, so no remote party reads as a local operator and no address routes through a second peer. Added on 2026-10-28 for federation, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies |
| Local address | A line address, an operator address or an agent address |
| Address | A local address or a remote address |
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
| Time | Written as a due time is. An event whose time matters carries it, since the model never reads `recorded_at` |
| Workdir | A relative path of at most 1,024 bytes, written with `/`, with no empty, `.` or `..` step, no `\` and no NUL, or `.` for the project root |
| Commit | 40 or 64 lowercase hexadecimal characters, or empty where the daemon could not read one |
| Ownership cause | `declared`, `handoff`, `recovery`, `operator`, `relinquished` or `failover` |
| Failover policy | `automatic` or `ask` |
| Entry kind | `handoff`, `recovery` or `note` |
| Budget source | `operator` or `engine` |
| Lease end | `released` or `undeclared` |
| Run status | `exited`, `stopped` or `lost` |
| Run clearance | `empty` or `operator` |

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

**The harness.** **Decided by the operator on 2026-10-28.** The harness itself sends
the notes it writes on its own account, such as a failover, a handoff it made, or an
outage of a peer, under the sender address `!dromon`. It is not a party. It has no
mailbox, it always exists, and no event creates it. An identifier never begins with
`!`, so no other address reads as it. No client, line, agent or peer input may name
it, since every input is parsed as a party's address, which refuses its text. Only
the decoding of the daemon's own log reads it, and only as the `sender` of a
`message_sent`. **Porting note.** A port keeps the two parsers apart, one for party
addresses that refuses `!dromon` and one for a logged sender that admits it, so that
no input path can reach the second.

## Events

Each event has a kind and fields, all required. An optional value is written as `null`.

| Kind | Fields | Applies when |
|---|---|---|
| `line_created` | `line_id` and `strategy` identifiers, `engine` model name | The line does not exist. The new line is idle. The engine names the model, and the strategy names the driver's credential strategy. **Rollback hazard.** A daemon before 2026-10-31 refuses a log holding a model name with `:`, such as a Pi line's, as corrupt |
| `line_state_changed` | `line_id`, `state` | The line exists and the change is in the transition table |
| `line_configured` | `line_id`, `config` text of at most 262,144 bytes, `sealed` an array of 1 to 4,096 sealed values, each an object of `at`, the steps of its location, and `digest`, a SHA-256 digest, or left out when there are none | The line exists. The text is the canonical JSON of [`LINE_CONFIG.md`](LINE_CONFIG.md), validated by the daemon before it appends, and replaces any earlier configuration. `sealed` names each value the daemon took out of the text, as sealed values below specify. A location is `mcp_servers`, a server, `env` or `headers`, and a key, or `settings`, `env` and a key, each step 1 to 4,096 bytes, and the locations are distinct and in order. **Rollback hazard.** A daemon older than `sealed` refuses an event that gives it. Added on 2026-11-01 |
| `line_session` | `line_id`, `engine_session` text of 1 to 256 bytes | The line exists. Records the engine session the line's next turn resumes, so a restarted daemon continues the line's conversation. Appended after a turn, or right after `line_created` when the line adopts an existing session |
| `line_session_cleared` | `line_id` | The line exists. Its next turn starts a new engine session, as an operator asked with `clear_session`, because the line's `workdir` changed, or at the start of each turn when `fresh_session_each_turn` is set, and from 2026-10-31 also when a configuration turns `fresh_session_each_turn` on for a line that has a recorded session, which from round 9's review, F1, includes a line in a turn, whose turn then records no `line_session` when it ends. The engine keeps the old session. The line is owed a context handoff for `cleared`, which configuration may opt out of. From 2026-10-31 it also ends every lease the line holds, as the lease rules below say. With `fresh_session_each_turn`, the event is appended after that turn's mail is delivered and before its engine starts, so replay and restart cannot resume the preceding session |
| `line_rebound` | `line_id` and `strategy` identifiers, `engine` model name | The line exists and its current engine and strategy are not both these. The line now runs this model with this driver strategy, as an operator asked with `rebind_line`. Its identity, mailbox, configuration and overrides stay, and its next turn starts a new engine session. The project file's check compares with this binding. The line is owed a context handoff for `rebound`. From 2026-10-31 it also ends every lease the line holds |
| `line_overridden` | `line_id`, `overrides` text of at most 262,144 bytes | The line exists. The text is the canonical JSON of the line's override document, as [`CLIENT.md`](CLIENT.md) specifies under settings, validated by the daemon before it appends, and replaces any earlier overrides of the line |
| `project_overridden` | `overrides` text of at most 262,144 bytes | Always. The project's override document, replacing any earlier one |
| `line_held` | `line_id`, `question_id`, and from 2026-10-31 an optional `cause` | The line exists and is not held, and the question is open and from the line. The daemon wakes the line no more until it is released. `cause` is one of `autonomous_turns`, `cost`, `disclosure_checks`, `git_ssh_changed`, `plan_limit`, `inferred_limit`, `turn_failed` and `stopped`, the short cause other lines are shown, as [`LINE_CONFIG.md`](LINE_CONFIG.md) gives it. It is left out of the wire when absent, so an event written before it decodes with no cause. **Rollback hazard.** A daemon before 2026-10-31 refuses a log holding a `line_held` that names a cause, as corrupt |
| `line_released` | `line_id` | The line is held |
| `line_hibernated` | `line_id` | The line exists and is not hibernated. The daemon starts no turn of the line until it is awakened, and its mail, session and pending scheduled wake are kept, as [`PRIORITY.md`](PRIORITY.md) specifies. Only an operator's `hibernate` records it. Added on 2026-10-27. A log written before the rename of the same day holds it as `line_muted`, which still decodes as `line_hibernated` |
| `line_awakened` | `line_id` | The line exists and is hibernated. Ends the hibernation. Only an operator's `awaken` records it. Added on 2026-10-27. A log written before the rename holds it as `line_unmuted`, which still decodes as `line_awakened` |
| `operator_registered` | `operator_id` | The operator does not exist |
| `agent_registered` | `agent_id`, `access`, `token_sha256` | Always. Registers the agent, or replaces its access and digest and restores it if revoked. A token itself is never stored |
| `agent_revoked` | `agent_id` | The agent exists and is not revoked |
| `message_sent` | `message_id`, `sender` and `recipient` addresses, `kind`, `body` text, `choices`, `reply_to` message identifier or `null`, `request`, present only for a keyed call, and `asks_reply` boolean, present only when the sender said, and only on a note | See the message rules below. A `request` is an object of `key` and `digest`, the request key of the `send_message` or `ask_operator` call that sent it and the SHA-256 digest of the call's other arguments. The harness and a firing wake give none, and the key must not be one its party already keeps for that tool. It is kept as the key's act, at most 256 per party and tool, the oldest forgotten first |
| `message_received` | `message_id`, `sender` remote address, `recipient` local address, `body` text, `reply_to` message identifier or `null`, `origin_id` identifier, `received_at` time, `priority_asked` boolean | The message does not exist and no pending scheduled wake reserves its identifier, the sender's peer is declared and not removed, no earlier `message_received` has this peer and `origin_id` among the keys kept, and the recipient exists. A `reply_to` names an existing message from the recipient to the sender. Records a note from a peer's party, the peer's own identifier for it, and the idempotency key of the peer and that identifier in one event, so a crash cannot record one without the other. Until it is delivered it counts toward the waiting mail of its peer and of its recipient. Applying it first drops the keys received more than 2,184 hours before its `received_at`, the longest expiry of 90 days and a day for the difference between clocks, oldest first and stopping at the first younger key, so the keys are bounded by the mail of that window. `priority_asked` records whether the peer's envelope asked for priority, which a `message_prioritized` follows when the receiver honours it. Added on 2026-10-28, `received_at` in step 5 and `priority_asked` in step 6 |
| `message_read` | `message_id` | The message was delivered and not yet read. Its recipient opened it, which obliges nothing. A line opening its own sent mail does not record this event, so the recipient's read state is unchanged |
| `message_acknowledged` | `message_id`, and `reason` text of at most 500 bytes, present only when given | The message was delivered and not yet acknowledged. Its recipient marked it dealt with without a reply. Its sender sees the acknowledgement as distinct from a reply. A question stays open |
| `message_prioritized` | `message_id` | The message exists, is a note to a line or to a peer's line, is not delivered, returned or unconfirmed, and is not priority mail. Marks it priority mail, as [`PRIORITY.md`](PRIORITY.md) specifies. The daemon appends it right after the note's `message_sent` or `message_received`. For mail to a peer it makes the envelope ask for priority. Added on 2026-10-27, and for peers on 2026-10-28 |
| `message_correlated` | `message_id`, `key` identifier | The message exists, is a note from an external agent to a local line, is not delivered, and has no key. Gives it a correlation key, which names the agent's own task, as [`AGENTS.md`](AGENTS.md) specifies. The daemon appends it right after the note's `message_sent`. Added on 2026-10-28 |
| `turn_correlated` | `line_id`, `keys` array of identifiers, `key_count` whole number, `cost_micro_usd` whole number from 0 to 10^15 or `null` | The line exists. `keys` holds from 1 to 64 distinct keys in sorted order, and `key_count` is at least their number, and equals it unless 64 are listed. Records that a turn of the line ended which delivered notes with these keys, however it ended, with the turn's cost in millionths of a dollar or `null` when the engine reported none. `key_count` is how many distinct keys the turn carried, so a reader sees that a turn counted under several keys cost its amount once. Applying it adds the turn to the totals of each listed key, the whole cost under each, and counts it as shared when `key_count` exceeds 1. The state keeps the totals of at most 4,096 keys, and drops the key used least recently to make room. Added on 2026-10-28 |
| `task_recorded` | `line_id`, `task_id` of at most 128 bytes, `command` of at most 2,048 bytes, `status` of at most 32 bytes, `exit_code` 32-bit whole number or `null`, `output_file` of at most 1,024 bytes or `null`, `output_digest` SHA-256 in lowercase hexadecimal or `null`, `output_bytes` whole number or `null`, `workdir_commit` commit or empty, `ended_at` time | The line exists, and `output_digest` and `output_bytes` are both given or both `null`. Records that a background task of the line's engine ended between turns, for every end the engine reports, whether or not a `task_noted` wakes the line, and never wakes it. The command and output file name are recorded as the engine reported them, and `role_status` screens them against the disclosure list for every seat but the line itself. The digest covers the output the engine reported inline, or else the output file read as a task note reads it, up to 16 MiB. `workdir_commit` is `HEAD` where the line works, empty when none was read, and says nothing of uncommitted changes. Applying it keeps the line's latest 10 records, which `role_status` shows its role's seats. Added on 2026-10-28 |
| `disclosure_checked` | `line_id`, `checked_at` time, `matched_lines` whole number from 0 to 1,000,000, `checked_bytes` whole number from 0 to 67,108,864 or left out when null | The line exists. Records that the line checked text, one file, or a list of files with `check_disclosure`, or that a line tool refused text for disclosure, or that a run's output was withheld for disclosure, and how many lines held a term of the project's disclosure list. The text, its name and any term are never recorded. Applying it keeps, for the limits [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, the line's latest 10 failure times when `matched_lines` is above 0, and its latest 60 pass times otherwise, and, when `checked_bytes` is given, its latest 70 check times with their bytes. A refusal is always recorded with at least 1, and with no bytes except from `write_scratch`. `checked_bytes` is how many bytes a `check_disclosure` call read, added in the round 15 review, or since the round 15 re-review how many bytes of text and path a screened `write_scratch` gave, on a pass and on a refusal. A log from before the task of 2026-11-05 replays with its passes counted under the pass limit, and one from before `checked_bytes` with no bytes, which only widens the windows. **Rollback hazard.** A daemon from before `checked_bytes` refuses a log that gives one. Added on 2026-10-28 |
| `message_delivered` | `message_id` | The message exists and was not delivered, returned or left unconfirmed. For a line, delivery is into a turn. For an operator, delivery is acceptance by the operator's relay or display in a client. For a party of a peer, it is the peer's `accepted`, and the message leaves the peer's outbox |
| `message_returned` | `message_id`, `code` one of the codes of returned mail in [`FEDERATION.md`](../architecture/FEDERATION.md), `note_id` message identifier or `null` | The message exists, is to a party of a peer, and was not delivered, returned or left unconfirmed. When its sender is a line, `note_id` is a new identifier that no message, withdrawn wake or pending scheduled wake holds, and it is `null` otherwise. The message leaves its peer's outbox undelivered, and a line sender receives a note from itself under `note_id` whose text the model derives from the code. Added on 2026-10-28 |
| `message_unconfirmed` | `message_id`, `note_id` message identifier or `null` | As for `message_returned`. The message expired after a copy of it may have reached the peer without an answer, so whether it was delivered is unknown, and a line sender's note says so. Added on 2026-10-28 |
| `wake_requested` | `wake_id`, `line_id`, `message_id` | The wake does not exist, the line exists and is not stopped, and the message exists with the line as its recipient |
| `wake_issued` | `wake_id` | The wake exists and was not issued, and its line is idle |
| `wake_scheduled` | `line_id`, `message_id`, `due` due time, `note` wake note, `task_id` task identifier or `null` | The line exists, no message has the identifier, and no other line's pending scheduled wake reserves it. Becomes the line's one pending scheduled wake, replacing any earlier one, and reserves `message_id` for its note. When `task_id` names a running background task, applying the matching `task_noted` event withdraws the wake atomically. Events written before this field was added decode with `null`. A `request`, present only for a keyed `wake_after` call, is as for `message_sent` |
| `wake_withdrawn` | `line_id`, `message_id` | The line exists and its pending scheduled wake reserves `message_id`. Removes that wake, so it never fires, and its identifier is kept, so a later `message_sent`, `task_noted` or `wake_scheduled` that takes it is refused. Only the daemon records it, when the engine reports any compaction while the wake `compact_session` scheduled remains pending, or, from 2026-10-26, when an operator's interrupt stops the line's turn, or, from the audit of round 14, when an operator retires the line |
| `restart_noted` | `line_id`, `message_id`, `body` note of at most 2,048 bytes | Checked as `task_noted` is. Records a note from the line to itself, already sent, saying that a daemon restart ended its turn at a safe point, as [`RESTART.md`](RESTART.md) specifies, so the restarted daemon wakes the line. Only the daemon records it, as the turn ends. Decided by the operator on 2026-10-27 |
| `task_noted` | `line_id`, `message_id`, `task_id` of at most 128 bytes, `status` of at most 32 bytes, `body` note of at most 8,192 bytes | The line exists, no message has the identifier, and the note identifier is not reserved by a scheduled wake. Records a note from the line to itself saying that a background task ended. If the line's pending scheduled wake names this `task_id`, the event atomically removes it, keeps its identifier among the withdrawn wakes, and its body names the cancelled wake and says the task ended the wait. This task note is not suppressed by the ordinary per-line task-note limit. An end between turns wakes the line, unless its engine has already begun an admitted turn on it, in which case that turn consumes the note. Ends during a turn or for a task the line stopped are recorded as delivered without a wake. Only the daemon records it, from the engine's own report |
| `line_cost_marked` | `line_id`, `engine_session` of at most 256 bytes, `total_micro_usd` a whole number from 0 to 10^15 | The line exists and the session is not empty. Records the running cost total the line's engine reported for its session after a turn, in millionths of a dollar, as the line's cost mark, from which the next turn's cost is measured |
| `line_cost_recorded` | `line_id`, `cost_micro_usd` whole number from 0 to 10^15 or `null`, optional `estimated` `true`, optional `between` `true` | The line exists. `estimated` and `between` are `true` or absent, never `false`, and `estimated` needs a cost. Records the cost of one turn, or with `between` of one spend outside a turn such as a compaction, in millionths of a dollar, or `null` when unknown. `estimated` says Dromon estimated it from the engine's token counts and its price table. Applying it adds to the line's whole cost in `State.line_costs`, the reported and estimated sums apart, the turns and the spends between turns apart, and the records of unknown cost, each sum held at 10^15. No release resets it. **Rollback hazard.** A daemon older than the kind refuses a log that holds one. Added in round 15, item 1 |
| `line_turn_ended` | `line_id`, `workdir_commit` commit or `null`, `unreported`, present only as `true`, and `failed`, present only as `true`, from round 14, when the turn failed, which keeps the line failed in the state until a turn ends without it, new mail reaches the line, `line_released` or a restart note. **Rollback hazard.** A daemon older than `failed` refuses an event that gives it | The line exists. Records the commit checked out in its worktree as its latest turn ended, or `null` when it could not be read, so a moved-worktree notice can compare against the last turn after a daemon restart. Only the daemon records it, however the turn ended. `unreported` is `true` when the line's engine returned no report for the turn, as when its prompt could not be composed, a check before the engine failed, the engine raised or the turn was cancelled, and is omitted otherwise. Such a turn retires none of the line's own notes, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, since its prompt may never have reached the engine. `false` is refused, and an absent field decodes as `null`, so a log written before the field reads every turn end as reported. **Rollback hazard.** A daemon that predates the field rejects an event that holds it, so once a turn has ended unreported, an older daemon cannot read the log, as for a request key above. `unreported` added on 2026-10-30 in the round 8 review. `failure`, present only with `failed`, one fixed word for the cause as [`LINE_CONFIG.md`](LINE_CONFIG.md) lists them under `last_failure`, and `failure_class`, present only with `failure` and only where one exists, the daemon's own exception type or a listed result subtype, at most 64 characters, a letter then letters, digits or underscores, added in the round 15 follow-up of 2026-11-05. Neither carries an engine's text. A failed turn logged before them has neither, and reads as of unknown cause. **Rollback hazard.** A daemon older than `failure` refuses an event that gives it |
| `processes_left` | `line_id`, `cause` one of `exited`, `stopped`, `orphaned`, `detached` and `swept`, `pids` an array of 1 to 64 whole numbers above 1 and below 2^31, `commands` an array of as many texts of at most 1,024 bytes, `more` a whole number from 0 to 1,000,000 | The line exists. Records processes the line's engine left running, with the pid and command of each and how many more were left than are named, as the line's latest record. The first three causes are those of a launcher under the `leave_background` stop policy, when the harness exited, was stopped or found the daemon gone. `detached` is for processes found by the line's mark after its engine ended. `swept` is for processes the daemon's sweep of a launcher that exited before it finished found still running. Each command is a summary, the executable's name without its directory and its arguments with credentials masked, as `dromon.redact` describes. A record written before 2026-10-27 may hold a whole command. The whole command is kept only in the launcher's report file, readable by its owner alone. Only the daemon records it, and it makes no process Dromon's to end. Added on 2026-10-26 |
| `stop_unverified` | `line_id`, `note` of 1 to 1,024 bytes | The line exists. Records a stop of the line's engine that Dromon could not verify was complete, as the line's latest, which its status shows as `processes.stop_unverified`. Recorded when the launcher could not read the process table at its stop, or when it exited before it finished its stop and the daemon swept its harness's group. Only the daemon records it. Added on 2026-10-27 |
| `walk_limited` | `note` of at most 1,024 bytes, possibly empty | Always applies. Records what the daemon's platform keeps the launcher's walk of the process table from reaching, which every line's status shows as `processes.walk_limit`, or with empty text that nothing is. Only the daemon records it, as it starts, and only when it differs from the latest, so a platform's gap is recorded once. Added on 2026-10-26 |
| `question_withdrawn` | `message_id` | The message is a question and is open. Recorded by the asking line's own tool, by a start for a stale ceiling question, and, from the audit of round 14, by an operator's retirement of the asking line |
| `question_superseded` | `message_id`, `superseded_by` | Both messages are open questions, they differ, and they come from the same line |
| `context_handed_off` | `line_id`, `cause` of `rebound` or `cleared`, `outcome` of `given` or `opted_out`, `message_ids`, at most 64 identifiers, `chars`, a whole number from 0 to 1,048,576, and `sha256`, a lowercase hexadecimal SHA-256 digest when given and empty otherwise | The line exists and is owed a handoff for this cause, which `line_rebound` and `line_session_cleared` set, as their `rebound` and `cleared`. An `opted_out` handoff selects nothing, with no identifiers, zero `chars` and an empty digest. Ends the handoff. Only the daemon records it, once the engine ran the turn that carried it, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. Added on 2026-10-27 |
| `role_declared` | `role_id`, `workdir`, `handoff_seconds` a whole number from 60 to 86,400, `seats` an array of at most 16 distinct line identifiers, `failover` failover policy, `handoff_below_percent` a whole number from 0 to 100, optional `reviews` a non-empty array of at most 16 distinct role identifiers other than `role_id` | The role was never retired, and does not exist, or exists and this changes any of its fields, and its workdir changes only while no seat owns it. The handoff bound is how long an owner asked to hand the role over has before the operator is asked to recover it instead, an hour by default. `seats` is the declared order a failover follows, and may name a line before it is bound. `failover` says whether the harness passes the role on itself when its owner cannot act. The owner is asked to prepare a handoff when its provider's stated budget is above 0 and below `handoff_below_percent`, and never when it is 0. A record written before step 3 of roles has only the first three fields, and decodes with an empty order, `automatic` and 10. `reviews` names the roles whose work the role reviews. It is left out when empty, never written as an empty array, and read as empty when absent. The model does not require the named roles to exist, since one may be retired later, and the file and the role commands check that each exists when they write it. **Rollback hazard.** A daemon older than the field refuses a `role_declared` that names one. Added on 2026-10-27, the last three fields the same day, before any release, and `reviews` in round 15, item 5 |
| `seat_bound` | `line_id`, `role_id`, `may_own` boolean | The line and the role exist, the line is a seat of no other role, and this is a new seat or changes the seat's `may_own`. A seat whose `may_own` is false never owns its role, and an owner cannot be made one. Added on 2026-10-27 |
| `seat_unbound` | `line_id`, `role_id` | The role exists, the line is its seat, and the line does not own it. Ends the binding, and the seat's place in the role's notice counts. The line stays a line with its configuration, mail, leases and state. Only an operator's `role_update` or `role_delete` records it, as [`CLIENT.md`](CLIENT.md) specifies. **Rollback hazard.** A daemon older than round 10 does not know the kind, so once one is recorded, it cannot read the log, as for `unreported` above. Added on 2026-10-31 in round 10 |
| `role_retired` | `role_id`, `by` operator address, `retired_at` time | The role exists and is vacant, no line is its seat, and `by` is a registered operator. Removes the role, and its identifier is never declared again, since an identifier is never reused within its kind. Its journal entries stay. Only an operator's `role_delete` records it, after a `seat_unbound` for each seat. **Rollback hazard.** As for `seat_unbound`. Added on 2026-10-31 in round 10 |
| `line_retired` | `line_id`, `by` operator address, `retired_at` time | The line exists, is hibernated, is not running, is a seat of no role and is not retired, and `by` is a registered operator. The line stays, with its mail. From then on `message_sent` to it, `line_awakened`, `line_state_changed` to `running` and `seat_bound` of it are rejected, and `line_created` with its identifier is rejected as for any line. Only an operator's `retire_line` records it, after `question_withdrawn` for each of the line's open questions and `wake_withdrawn` for its pending wake in the same checked batch, from the audit of round 14. **Rollback hazard.** A daemon from before round 14 cannot decode it, and refuses the log. Added in round 14 |
| `role_held` | `role_id`, `holder` of `file` or `operator`, `by` operator address, `held_at` time | The role exists, `by` is a registered operator, and this changes the role's holder. A role with none is held by the file. A role command records `operator`, and a start or a reload records `file`, by the project's operator, once the file's table agrees with the role, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. **Rollback hazard.** As for `seat_unbound`. Added on 2026-10-31 in round 10 |
| `ownership_changed` | `role_id`, `owner` and `previous` line identifiers or `null`, `cause` ownership cause, `by` address, `entry_id` or `null`, `changed_at` time, `back_to` line identifier or `null` | See the ownership rules below. Added on 2026-10-27, and `back_to` the same day, before any release |
| `role_noted` | `role_id`, `entry_id`, `line_id`, `kind` entry kind, `body` text of 1 to 65,536 bytes, `workdir_commit` and `daemon_commit` commits, `noted_at` time | No entry has the identifier, the role exists, so a retired role takes no entry, and the line is its seat. A `handoff` or `recovery` entry is by the role's current owner, and a `note` by any seat. The daemon stamps both commits itself from `HEAD`. Added on 2026-10-27 |
| `brief_set` | `line_id`, `task` text of 1 to 4,096 bytes, `decisions` and `next_action` text of at most 4,096 bytes, `artifacts` an array of at most 32 paths, each of 1 to 1,024 bytes on one line, `workdir_commit` and `daemon_commit` commits, `set_at` time, `workdir_dirty` a SHA-256 in lowercase hex or `null`, omitted when `null` | The line exists. The text fields and paths together are at most 16,384 bytes. Replaces the line's previous standing brief, which only `set_brief` records, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. The text is the line's own and untrusted. The daemon stamps both commits and the time itself, and leaves a commit empty when it cannot read it. `workdir_dirty` is the digest of the worktree's `git status` entries when the brief was set, which a turn compares to flag the brief as stale, and `null` where git could not read them or for a brief recorded before the field. Added on 2026-10-29, and `workdir_dirty` in the review of `49236ad`. **Rollback hazard.** A daemon older than the field refuses the event when it carries the field |
| `budget_stated` | `provider` identifier, `remaining_percent` a whole number from 0 to 100 or `null`, `resets_at` time or `null`, not both `null`, `source` budget source, `note` text of at most 1,024 bytes, `stated_at` time, and from 2026-10-31 optional `window`, an identifier | Always. An event without `window` replaces the provider's earlier statement. An engine event with a window updates that window's snapshot; the state keeps the lowest remaining percentage among unexpired engine windows. Older window snapshots are discarded when their reset passes. It is a snapshot and later turns do not adjust it. **Rollback hazard.** A daemon predating `window` refuses a log after one is recorded. Added on 2026-10-27; `window` added on 2026-10-31 |
| `budget_handoff_requested` | `provider` identifier, `role_id` identifier, `remaining_percent` a whole number from 1 to 100, `threshold` a whole number from 0 to 100, `requested_at` time | The role exists and has an owner, its threshold matches, its current provider budget matches `remaining_percent` and is below `threshold`, and this provider-role pair has not been asked since its budget last rose above the threshold. Records one planned handoff request, which is re-armed only above threshold. **Rollback hazard.** A daemon predating round 11 does not know this kind and cannot read a log after one is recorded, as for `seat_unbound` above. Added on 2026-10-31 |
| `lease_acquired` | `name` and `line_id` identifiers, `acquired_at` and `expires` times, `expires` the later, and `note` text of at most 500 bytes, present only when given, from round 14. **Rollback hazard.** A daemon older than `note` refuses an event that gives it | The line exists, and no lease is recorded under the name or the recorded one's `expires` is not after this `acquired_at`. Records the line as the lease's holder until `expires`, replacing an expired one. See the lease rules below. **Rollback hazard.** A daemon older than 2026-10-31 does not know the kind, so once one is recorded, it cannot read the log. Added on 2026-10-31 |
| `lease_released` | `name` and `line_id` identifiers, `cause` lease end | The line holds the lease under the name, expired or not. Ends it. `released` is the holder's own `release_lease`, and `undeclared` is recorded by a reload or a start whose project file no longer declares the name. **Rollback hazard.** As for `lease_acquired`. Added on 2026-10-31 |
| `run_started` | `run_id` and `line_id` identifiers, `role_id` and `previous` identifiers or `null`, `command` identifier, `argv` an array of 1 to 64 texts of at most 1,024 bytes each, the first not empty, `workdir` an absolute path and `output_file` a path relative to it, each of at most 1,024 bytes, `pid` a whole number above 1 and below 2^31, `process_started` and `started_at` times | No run has the identifier and the line is not stopped. `role_id` is the role the line is a seat of, or `null` when it is none, and a seat owns its role. The run's scope is its role, or its line when it has none. No run of the scope is open, the scope's latest run is not `lost` without a `run_cleared`, and `previous` is the scope's latest run, or `null` when it has none. `pid` and `process_started` name the run's supervisor, as [`RUNS.md`](../architecture/RUNS.md) specifies. **Rollback hazard.** As for `lease_acquired`. Added on 2026-10-31. From round 14 a commit run also has `commit`, a full lowercase hexadecimal commit of 40 or 64 characters, and `checkout`, the absolute directory of at most 1,024 bytes its command ran in, each given exactly when the other is, and both left out of the wire when null, so `workdir` then holds only the output. **Rollback hazard.** A daemon that predates them refuses a log that holds a commit run, and reads every other `run_started` |
| `run_ended` | `run_id` identifier, `status` run status, `exit_code` a 32-bit whole number or `null`, `null` for `lost`, `output_digest` a SHA-256 digest or `null`, `output_bytes` a whole number or `null`, given whenever the digest is, `note` text of at most 1,024 bytes, `ended_at` time, `output_tail` text of at most 2,048 bytes of printable characters and newlines, left out when null | The run exists and has not ended. Ends it with what its receipt or the process table showed. `output_tail` is the last lines of a failed run's output, untrusted, as [`RUNS.md`](../architecture/RUNS.md) specifies, added in the task of 2026-11-05. **Rollback hazard.** As for `lease_acquired`, and a daemon from before `output_tail` refuses a log that gives one. Added on 2026-10-31 |
| `commit_run_refused` | `line_id` identifier, `role_id` identifier or `null`, `command` identifier, `commit` a full lowercase hexadecimal commit of 40 or 64 characters, `reason` text of at most 1,024 bytes, `refused_at` time | The line is not stopped, and `role_id` is the role the line is a seat of, or `null` when it is none. It replaces the scope's latest refusal, and the scope starts no commit run for 120 seconds after `refused_at`, a cooldown the reader applies, as [`RUNS.md`](../architecture/RUNS.md) specifies. Recorded when a commit run's start fails in its checks or its checkout, which hold the commit worker. **Rollback hazard.** A daemon from before it cannot decode it, and refuses the log. Added in the review of 1449c5c, round 14 |
| `run_cleared` | `run_id` identifier, `cause` run clearance, `operator_id` identifier or `null`, given exactly when `cause` is `operator`, `cleared_at` time | The run exists, has ended as `lost` and is not cleared, and `operator_id`, when given, is a registered operator. A lost run's command may still run, so its scope starts no other run until this is recorded, with `empty` when reconciliation found no process in the supervisor's process group or session, and `operator` when an operator cleared it with `run_clear` after inspection, as [`RUNS.md`](../architecture/RUNS.md) specifies. **Rollback hazard.** As for `lease_acquired`. A daemon older than this review also lets a lost run's scope start another run while its command may still run. Added on 2026-10-31, in the review of that day |
| `peer_declared` | `peer_id` identifier, `transport` of `unix` or `tls`, `policy` text of at most 65,536 bytes | The peer is new, was removed, or this changes its transport or policy. Recorded at start from the project file's `[peers]` table, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies. The policy is canonical JSON of every key of the peer's table but `transport`, and never holds a token. A peer declared again after its removal starts with no token. Added on 2026-10-28 |
| `peer_removed` | `peer_id` | The peer is declared and not removed. Recorded at start when the project file no longer declares it. Clears its token digest. Added on 2026-10-28 |
| `peer_keyed` | `peer_id`, `token_sha256` a SHA-256 digest or empty | The peer is declared and not removed, and an empty digest, which revokes the token, requires a token to revoke. The digest is of the token this daemon issued for the peer to present. Only the digest is ever stored. Added on 2026-10-28 |
| `peer_opened` | `peer_id` identifier, `scope` of `local` or `remote`, `opened_at` time | No peer of the name exists, or it is removed. Records an open peer when it is first seen, a project of the per-user registry by its federation name, or a key on another machine that no declared peer pins, by its pin, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies under open federation. Its policy is the project file's `[federation.open]`, read at each use, so none is recorded. A later `peer_declared` of the name replaces it, so a declared table always governs its peer, and the project file never removes an open peer. `peer_keyed` is refused for an open peer, which has no token. Added on 2026-10-31. **Rollback hazard.** A daemon before this change cannot read a log holding it |

**Message rules.** A `message_sent` applies when every one of these holds.

1. The message does not exist, and the sender and recipient both exist. The harness
   always exists. Neither is a
   remote address, since a peer's mail is recorded by `message_received`. The recipient
   may be a remote address whose peer is declared and not removed, when the message is
   a note and fewer than 1,000 messages wait in that peer's outbox. Such a message
   joins the outbox in the order sent. The daemon's outbound policy is checked before
   the append, since it is configuration. They differ,
   unless the sender is a line, which may send itself a note, as
   [`PRIORITY.md`](PRIORITY.md) specifies since 2026-10-27. An operator or an agent
   may not. The firing of a scheduled wake, below, is one such note, and a
   `task_noted` or `restart_noted` event is another way a line receives a note from
   itself, which is not a `message_sent`. A message whose identifier a pending scheduled wake reserves is
   refused unless it is that firing.
2. A note has no choices.
3. A question comes from a line that is not stopped and goes to an operator. A new
   question is open.
4. A reply, which has a `reply_to`, names an existing message and reverses its route.
   The reply's sender is the earlier message's recipient, and its recipient is the
   earlier message's sender. An answer from any other operator, or to any other line,
   is refused. **Round 14 task of 2026-11-01.** A note may instead keep the route of
   the note it names, from the same sender to the same recipient, as a correction of
   it. Only a note corrects, and only a note is corrected. Applying a correction sets
   the earlier note's `corrected_by` once, and a correction asks what the note it
   corrects asked, or more when its sender says `asks_reply` true. **Round 14 review,
   M1.** It never asks less, so a sender cannot correct an ask away, and a sender's
   `asks_reply` false is honoured only for a note to a line. Both are rules of the
   derived count and change no event. **Rollback hazard.** A daemon older than this
   rule refuses a log that holds a correction, and one older than `asks_reply` or
   `reason` refuses an event that gives either, as the rollback floor below states.
5. A reply to a question requires the question to be open, must be a note, and marks
   the question answered by this reply.
6. A message from the harness is a note to a line or an operator of this daemon, never
   to an agent or a peer, and is not a reply. Nothing replies to it, since by rule 4 a
   reply would go to the harness, which is never a recipient.


**The rollback floor.** **Required by the operator on 2026-11-01, from the round 14
review, R1, and made the one place for every one-way change on the same day.** Each
change below is one way. Once the log holds one such event, field or value, no daemon
older than the change's floor can open it, since a daemon refuses as corrupt a kind, a
field, a value or a rule it does not know. A commit can open a log only if it descends
from the floor of every change the log holds, so the floor of a log is the newest floor
among the changes it holds. Each other document that names a rollback hazard refers
here. A row names the first commit to write and read the change, or the first of each where it names several changes, and the merge that
brought it into `main`, which is the oldest commit of `main` that opens it.
A pending row is on a branch not yet merged, and its floor on `main` will be that
branch's merge.

| Change | First commit | Floor on `main` | Branch |
|---|---|---|---|
| `operator_registered`, operator addresses, `note` and `question` kinds | `4d9b3ff` | `621d6db` | `feat/operator-addresses`, #5 |
| `line_configured` | `7040c16` | `2d04603` | `feat/ticks-and-line-tools`, #11 |
| `line_session` | `885540c` | `2e413c8` | `feat/operator-questions`, #12 |
| `message_read`, `message_acknowledged` | `3435363` | `4db8750` | `feat/acknowledgements`, #15 |
| `line_held`, `line_released` | `5443902` | `c494576` | `feat/conversation-control`, #16 |
| `agent_registered`, `agent_revoked`, agent addresses | `2354eca` | `c711727` | `feat/external-agents`, #26 |
| `line_session_cleared`, `line_overridden`, `project_overridden` | `3565e10` | `b04276e` | `feat/settings-layers`, #32 |
| `wake_scheduled` | `be9138d` | `cca21c4` | `feat/wake-and-guards`, #37 |
| `line_rebound` | `b04b07a` | `521090e` | `feat/rebind-settings-leases`, #38 |
| `task_noted` | `551538c` | `ea0bee1` | `feat/round3`, #45 |
| `line_cost_marked` | `970af75` | `d31c7cd` | `feat/round4`, #46 |
| `wake_withdrawn` | `6b22d69` | `39d7d10` | `feat/round7`, #49 |
| `processes_left`, `stop_unverified`, `walk_limited` | `8d5b34a` | `7bb46de` | `feat/stop-policy`, #54 |
| `context_handed_off` | `b5e5beb` | `c22843a` | `feat/remaining-mining`, #56 |
| `message_prioritized`, `restart_noted` | `b2431bb` | `05c16de` | `feat/restart-and-seats`, #58 |
| `role_declared`, `seat_bound`, `ownership_changed`, `role_noted`, `budget_stated`, and the kinds `line_hibernated` and `line_awakened` that replaced `line_muted` and `line_unmuted` | `4d65469`, `dc768e1` | `d8673c7` | `feat/roles`, #60 |
| `role_declared` seats, failover and `handoff_below_percent`, and `ownership_changed.back_to` | `dc768e1`, `7041905` | `d8673c7` | `feat/roles`, #60 |
| `message_received`, `message_returned`, `message_unconfirmed`, `message_correlated`, `turn_correlated`, `disclosure_checked`, `task_recorded`, `peer_declared`, `peer_removed`, `peer_keyed`, remote addresses and the `!dromon` sender | `d767e49` to `26bd06f` | `1f98cab` | `feat/federation`, #67 |
| `line_turn_ended`, `wake_scheduled.task_id` | `eba7e37` | `e11064d` | `feat/continuity-fixes`, #71 |
| `brief_set` | `b467231` | `2b0ac5c` | `feat/standing-brief`, #72 |
| `request` on `message_sent` and `wake_scheduled` | `7543a57` | `6d1e098` | `feat/durable-request-keys`, #76 |
| `line_turn_ended.unreported` | `14993cb` | `2caeb4f` | `fix/own-notes-shown`, #92 |
| `seat_unbound`, `role_retired`, `role_held` | `99184f7` | `81f9153` | `feat/role-crud`, #104 |
| The `issue` message kind | `1ede2db` | `2acce91` | `feat/operator-push`, #105 |
| An engine-qualified model name with `:` in `line_created` or `line_rebound`, such as a Pi line's | `cd245c3` | `70abab2` | `fix/role-scratch`, #106 |
| `lease_acquired`, `lease_released`, `run_started`, `run_ended`, `run_cleared` | `09003fa`, `14d605e` | `047fb45` | `feat/durable-ownership`, #108 |
| `budget_handoff_requested`, `budget_stated.window` | `1a1532d` | `33ac280` | `fix/grok-quota`, #109 |
| `peer_opened` | `499bd55` | `f5f8da1` | `feat/federation-modes`, #114 |
| `line_held.cause` and its hold causes | `f893e9a` | `f34a620` | `feat/peer-status`, #119 |
| `message_sent.asks_reply`, written when a line's `send_message` or an operator's `send` says whether a note asks a reply | `4330502` | `ba90b4d` | `feat/mail-and-roles`, #125 |
| A correction, a note naming a note of the same sender to the same recipient | `4330502` | `ba90b4d` | `feat/mail-and-roles`, #125 |
| `message_acknowledged.reason` | `4330502` | `ba90b4d` | `feat/mail-and-roles`, #125 |
| `line_turn_ended.failed`, written with every failed turn | `4330502` | `ba90b4d` | `feat/mail-and-roles`, #125 |
| `lease_acquired.note` | `4330502` | `ba90b4d` | `feat/mail-and-roles`, #125 |
| `line_retired` | `625f3de` | `8b2c508` | `feat/operator-r14`, #129 |
| A commit run, a `run_started` with `commit` and `checkout` | `1449c5c` | `8b2c508` | `feat/operator-r14`, #129 |
| `commit_run_refused` | `522c146` | `8b2c508` | `feat/operator-r14`, #129 |
| `line_configured.sealed`, written when a configuration holds a literal value | `025149b` | `8ca4ead` | `feat/eventlog-r14`, #134 |
| `line_cost_recorded`, written after every turn and every spend between turns | `49236ad` | Pending | `feat/r15-mail-cost-log` |
| `role_declared.reviews`, written when a role names a role it reviews | `49236ad` | Pending | `feat/r15-mail-cost-log` |
| `line_turn_ended.failure` and `failure_class`, written with every failed turn | Uncommitted | Pending | `feat/r15-mail-cost-log` |
| `brief_set.workdir_dirty`, written whenever git can read the worktree as a brief is set | Uncommitted | Pending | `feat/r15-mail-cost-log` |
| `run_ended.output_tail`, written when a failed run's output has a tail | `172f9b9` | Pending | `feat/r15-runs-disclosure` |
| `disclosure_checked.checked_bytes`, written with every `check_disclosure` call and screened `write_scratch` | `da29483` | Pending | `feat/r15-runs-disclosure` |

**Fact, from the history.** The table was built from the history of `HEAD` on
`feat/mail-and-roles` at `5a1603f`. A kind's first commit is the oldest whose
`dromon.codec` names it, a field's is the oldest that adds it to its kind's fields or
to the fields omitted when null, and a value's is the oldest that adds it to its
enumeration in `dromon.model`. The floor on `main` is the oldest merge on the first
parent line of `HEAD` that contains the first commit. The rows of `feat/operator-r14`
come from that branch at `1449c5c`. Every enumeration of `dromon.model` gained no
value after it was added except `MessageKind`, which gained `issue`. Commit `1361146`
and `5a1603f` on `feat/mail-and-roles` add no kind, field or value, and `main` at
`af316a0` changes neither `dromon.codec` nor `dromon.model` since `47c58d5`, so none of
them moves a floor. The kinds of the first event log, merged in `4f0ee19`, are the
format itself and are not listed. **Updated on 2026-11-01 on `feat/eventlog-r14`.**
`main` at `ba90b4d` merged `feat/mail-and-roles` in #125 and `feat/processes-r14` in
#124, and `dromon.codec` and `dromon.model` are the same at `ba90b4d` as at `5a1603f`,
so #124 moves no floor. **Updated again on 2026-11-01, in the merge of `main` at
`8b2c508` into `feat/eventlog-r14`.** Of the merges on `main` after `ba90b4d`, only
#129, `feat/operator-r14`, changes `dromon.codec` or `dromon.model`, so its rows now
have their floor and the others move none. Its `commit_run_refused`, first written in
`522c146`, is added, and no enumeration of `dromon.model` gained a value in it.
**Updated on 2026-11-05 on `feat/r15-mail-cost-log`, from main at `9296036`.**
`feat/eventlog-r14` merged in `8ca4ead`, #134, which gives the sealed field its
floor. **Fact, from `git log --first-parent`.** `de680f5`, #133, is the merge before
it and does not contain `025149b`. `line_cost_recorded` is added as pending.
**Inference.** It is written after every turn of every line, so the first turn a
daemon from this branch runs passes its floor, and from then a rollback to older code
refuses the log until the operator restores it from a copy taken before. The round's
other changes, the thread rule for unanswered mail and the stale brief lead, add no
kind, field or value, since each is derived as events apply or composed in the
prompt.

**Inference.** The search reads the codec and the model, not every rule of the
applying code. A rule that a newer daemon applies and an older one refuses, such as a
correction, is listed only where its commit or a document names it, so a rule changed
without either may be missing. `failed` is written with every failed turn, so a log a
daemon built from `feat/mail-and-roles` has run for any while has most likely passed
that floor. Most other changes appear only when a party uses the feature.
`dromond` checks a log only forward, so a rollback past a floor is found when the
older daemon refuses the log at its start, not before.

**Scheduled wakes.** **Decided by the operator on 2026-10-26.** A line schedules a wake
for itself with the `wake_after` tool, as [`LINE_CONFIG.md`](LINE_CONFIG.md)
specifies. A line has at most one pending scheduled wake. When its due time has passed,
the daemon fires it by appending one `message_sent` whose `message_id` is the reserved
identifier, whose sender and recipient are both the line, whose kind is `note`, whose
body is the note or the fixed withheld text below, and which has no choices and no
`reply_to`. Applying it clears the
pending wake. It is the only message to itself that clears a scheduled wake.

- **Sender.** The line scheduled the wake, so the note is its own, not the harness's.
  An operator sender would make the note operator mail, which carries authority and resets a line's autonomous-turn
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
- **After firing.** The state keeps each fired wake's due time under its note's
  identifier, so a later turn can say when the wake was due, also after a restart.
- **Due time.** The model checks only the form. The daemon compares the due time with
  its wall clock in Coordinated Universal Time.

**Ownership rules.** Roles are specified in
[`ROLES.md`](../architecture/ROLES.md), approved by the operator on 2026-10-27. An
`ownership_changed` applies when every one of these holds.

1. `previous` is the role's current owner, both `null` for a vacant role, and `owner`
   differs from it. Two changes that race name the same `previous`, so only the first
   applies.
2. `owner`, unless `null`, is a seat of the role whose `may_own` is true, and its line
   is not stopped. `owner` is `null` only for `relinquished` or `operator`.
3. `by` exists. A line `by` is a seat of the role.
4. `declared` replaces no owner and `declared` and `operator` are made by an operator.
   `handoff` and `relinquished` are made by the previous owner, and `relinquished`
   leaves the role vacant. `recovery` names an owner and is made by an operator or by
   the new owner itself, never by an external agent. `failover` names the previous
   owner and an entry, and is recorded by the new owner's address. The harness that
   makes it sends notes but is not a party, so `by` never names it.
5. `entry_id`, unless `null`, names an entry of the role. A `handoff` requires one, and
   it is the role's latest entry, a `handoff` entry by the previous owner.
6. `back_to`, unless `null`, is on a `handoff`, differs from `owner`, and is a seat of
   the role whose `may_own` is true. It names the seat the role returns to when
   `owner` next relinquishes without naming a successor.

The model checks that a change is consistent. The daemon checks that its maker had the
authority, such as the harness-observed exhaustion that a seat's own recovery needs. Only
the daemon records a `failover`, on the same grounds, under the role's `automatic`
policy.

**Lease rules.** **Task of 2026-10-31.** A lease is in the log from its
`lease_acquired`, so a restart keeps its holder, its acquisition and its expiry. Expiry
is its bound and needs no event. The daemon compares `expires` with its wall clock, and
the next `lease_acquired` under the name replaces an expired lease. Three other events
change leases as they apply, with no event of their own, so a replay agrees with the
daemon that recorded them.

1. `line_session_cleared` and `line_rebound` end every lease of their line, as the
   daemon ended them in its memory before this date.
2. `ownership_changed` with both `previous` and `owner` passes every lease that
   `previous` holds to `owner`, keeping `acquired_at` and `expires`. **Decided on
   2026-10-31,** as [`ROLES.md`](../architecture/ROLES.md) records.
3. `ownership_changed` that leaves the role vacant, or names no `previous`, changes no
   lease.

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

Addresses are written as their text forms, such as `"beta"`, `"@ana"`, `"&claude"` and
`"keleusma:kaizen"`. The harness, as a sender only, is written `"!dromon"`.

- `v` is the format version, the integer `1`.
- `kind` is the event kind and `data` holds exactly its fields, except that the
  `request` field of `message_sent` and `wake_scheduled` is left out when it is null.
  **Task of 2026-10-29, round 5.** An event without a request key is therefore written
  as before the field existed, so a daemon that predates it still reads the log, and a
  decoder reads an absent `request` as null. **Porting hazard.** A port must omit the
  field when null rather than write it as null, or an older reader rejects the event
  for its extra key.
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

**Cost of a start, measured.** **Fact, observed on 2026-11-01** by the operator, reading
this project's own log without writing it. The log was 4,390,912 bytes and held 1,882
messages. Importing the daemon's modules took 0.048 seconds, replaying the log took
0.139 seconds, and the peak memory was 32.4 MiB. The measuring script was the core
line's. The daemon commit was not recorded. One log is no curve, so the figures give no
growth rate. Bounded start below measures synthetic logs and the snapshot built for
them in round 15.

**Writers.** One process writes a log. Before each append, the writer checks inside an
immediate transaction that the newest `seq` is the one it last read, and refuses to
append otherwise.

## Sealed values

**Required by the operator on 2026-11-01, the design decided by core for review.** A
line's configuration may hold credentials in an MCP server's `env` or `headers` or in
a settings document's `env`, and until this change `line_configured` stored them as
written. The daemon now seals each such value before it appends the event, as
`dromon.sealed` implements.

- **What is sealed.** Every non-empty value of an MCP server's `env` or `headers` that
  holds no `${NAME}` reference, and every non-empty value of a settings document's
  `env`. A value that holds a reference is kept as written, since a reference is how
  an operator already keeps a token in the daemon's environment, and its literal text,
  such as `Bearer`, is not the secret. The project file's `secrets` list, which grants
  each line the variables its references may name, is unchanged.
- **What is not sealed.** A credential written into an MCP server's `command`, `args`
  or `url` is not sealed and reaches the log as written, as does any value elsewhere in
  the configuration. Those fields name a program or an address, and sealing them would
  hide from every view what a line runs and where it connects. A credential belongs in
  `env` or `headers`, or in the daemon's environment behind a `${NAME}` reference.
  Audit's review of 2026-11-01 confirmed this residual, and it stays recorded rather
  than closed.
- **Errors.** A configuration refused before sealing names the location of the refused
  value, such as `server search env TOKEN`, and a character position within it, and
  never any part of the value, since the error reaches the client and, for a project
  file, the daemon's output. Audit's finding V1 found a malformed reference echoing
  the 16 characters after its `${`, which is no longer so. A reserved variable that a
  reference names is not echoed either, since a reserved prefix matches names with
  arbitrary text after it.
- **The event.** The configuration text holds `<sealed>` at each sealed location, and
  `sealed` names the location and the HMAC-SHA256 of the value under a random 32 byte
  key drawn for that value. A configuration with no literal value is written exactly
  as before, with no `sealed`.
- **The store.** The values and their keys are kept in a JSON file beside the log,
  named for it with `.sealed` added, such as `events.sqlite.sealed`. It is replaced
  whole through an owner-only temporary file in the same directory, flushed, renamed
  and its directory flushed, so a reader sees the old store or the new one. The store
  is written before the event is appended, and after the append every value no line's
  current configuration names is removed, so a crash between the two leaves values no
  event names, which the next configuration removes.
- **Replay.** Replay never reads the store. The state keeps each line's text and its
  `sealed`, so a log builds the same state with or without its store, as every
  client, check and port requires. The daemon reads the store only when it builds a
  line's engine, and when it compares a stored configuration with the project file's
  at start, so a sealed line is not configured again at every start.
- **Failure modes.** A store that is missing, malformed, larger than 64 MiB, not a
  regular file, or readable by anyone but its owner gives no value. A value whose
  digest does not verify under its key is refused, so a store edited by hand is never
  used in part. The engine is then not built, and the turn fails with the location of
  each missing value, never a value, until an operator configures the line again. At
  start, a line whose project file holds the values is sealed afresh, since its stored
  configuration then differs from the file's. A line configured through a client
  needs the operator. A store that is a symbolic link is not followed, and a named
  pipe in its place is refused without waiting for a writer.
- **The check.** `dromond --check` reads the live store and writes none, so what its
  copy of the log seals is kept nowhere. This is load-bearing and not only tidy. The
  copy may reconfigure a line, and a writing store would then keep the new value and
  remove, as no configuration in the copy names it, the value the live log still
  names, so the running daemon would fail that line's next turn. A test holds the
  live store byte for byte across a check that reseals a line, after Audit's finding
  G1c that nothing held it.
- **No silent migration.** A `line_configured` appended before this change keeps its
  literal values, since the log is append-only and no event is rewritten. A line whose
  current configuration still holds literal values is named at every start, on the
  daemon's standard error, and in the daemon status as `event_log.unsealed_lines`,
  with how many values each holds. Nothing reseals it on its own. Configuring the line
  again, through the project file or `dromon configure`, appends a sealed event, and
  the earlier events still hold the old values. A line the log holds sealed is
  therefore not a line whose history is clean. Removing the old values needs a new
  log, which is not built here, so the remedy for a credential an older event holds is
  to rotate it, after which the value the log keeps is no longer valid.
- **Rollback.** A daemon older than `sealed` refuses as corrupt a log holding any
  `line_configured` that gives it, and so cannot start on it, as the rollback floor
  above records. A log whose every configuration has no literal value stays readable,
  since its events carry no `sealed`. An older daemon also does not read the store, so
  going back past the floor needs a log from before the first sealed configuration,
  and the operator gives the values again in the older form.

**Trust argument.** The log is owner only, but it is not the only copy of what it
holds. `dromond --check` copies it, a backup copies it, and the operator's client shows
and exports what it holds, so a credential in it is disclosed to every reader of any
copy and of any view. The store narrows that set. Fact, from the code. It holds only the
sealed values and their keys, it is created owner only and refused when its mode admits
anyone else, no snapshot or check copies it, and no view or tool reads it. That holds
for a copy of the log file and for the check's copy. A copy of the whole state
directory, such as a backup of it, carries the store with the log, and gains nothing
from sealing. The keyed digest gives integrity and not secrecy once the store is read,
since each key and its value sit together in it, protected only by its mode. Inference.
A line can never read it, since it lies beside the log in the daemon's state
directory, which every line's sandbox denies as it denies the log. A line whose sandbox
is disabled runs as the operator's user and could read both files, as it could read
the log before this change, so the store does not protect against such a line. The
keyed digest lets the event bind the store's value without revealing it, so a reader
of the log alone cannot test a guess, which a plain hash of a short secret would
allow. A reader of both files learns the values, as a reader of the log did before.
The values still reach the engine's process at each turn, as before, so a line sees the
values configured for it, as a settings `env` value is given to every command the line
runs. Sealing keeps the values out of the log, its copies and the views, and not out of
the line they are configured for. A credential written into a server's `command`,
`args` or `url` is not sealed and still reaches the log, and older events keep their
literals, both of which remain residuals as stated above.

## Credential masking

**Round 15, item 1.** Another project saw a temporary credential from a hosting service
printed in tool output. Command lines were already masked by `dromon.redact`. The free
text that enters the log is now masked as well, by `dromon.credentials`, in
`EventLog.append`, which every event passes through. The text is masked before the
model checks the event and before it is stored, so the stored event, the state and every
view, status, export and backup built from either hold the masked text. A keyed call's
request digest is taken over the arguments as given and is unchanged, so a retry is
unaffected.

- **Covered fields.** `body` of `message_sent`, `message_received`, `task_noted`,
  `restart_noted` and `role_noted`, and `choices` of `message_sent`. `reason` of
  `message_acknowledged` and `commit_run_refused`. `note` of `wake_scheduled`,
  `stop_unverified`, `walk_limited`, `budget_stated`, `lease_acquired` and `run_ended`.
  `task`, `decisions`, `artifacts` and `next_action` of `brief_set`. `command` of
  `task_recorded`, `commands` of `processes_left` and, from the review of 3344ca6,
  `argv` of `run_started`, a copy of the project file's argument vector, since the run is
  launched from the project file and not from the event. A test fails when an event class is
  neither covered nor listed as holding no free text, so a new event is classified when
  it is added.
- **What is masked, by pattern.** Values with a known prefix and length, namely GitHub,
  GitLab, Slack, Stripe, SendGrid, npm, PyPI, Hugging Face, DigitalOcean, Google and
  Anthropic or OpenAI keys, Amazon access key identifiers and the session token of an
  Amazon temporary credential, JSON web tokens, and a private key block. The credential
  after `Bearer` or `Basic`. The user and password of a URL, and from the review of
  3344ca6 those before a host with no scheme, apart from an image digest. The value of a
  `NAME=value` in any case, such as `github_token=...` or `--api-key=...`, whose name says token, secret, password, credential, access key, private
  key, API key or session. The value of a JSON member whose name says the same.
- **What is masked, by name.** The literal value of every environment variable that the
  project grants a line in a `secrets` list, wherever it appears, when it has at least
  eight characters. The daemon reads the values from its own environment at each append,
  so a reload of the project file takes effect at once.
- **Not covered, stated plainly.** The engine's own context, which Dromon does not hold,
  so a line that reads a credential in its own tool output still holds it and may write
  it into a file or a mail, where only the patterns above stand between it and the log.
  The raw output file of a supervised run, files a line writes, the launcher's report
  file and the engine's transcript, since Dromon does not author them and the log holds
  only a digest of the first. A credential with no known prefix, no `NAME=value` shape and
  no listed name. A listed secret shorter than eight characters. A value split across two
  texts. **Left unmasked on purpose, review of 3344ca6.** `line_configured`,
  `line_overridden` and `project_overridden` are canonical JSON that the daemon decodes
  again to build an engine, so a mask inside would change the configuration a line runs
  under, and the credentials a configuration carries in an MCP server's environment and
  headers are sealed before the event is written. A credential written into another
  part, such as the instructions, is recorded as written. `peer_declared.policy` never
  holds a token, and the command names of `run_started` and `commit_run_refused` are
  identifiers. **Choices.** A question whose choices differ only by a credential is
  refused, not stored, since an answer names a choice by its text and two equal choices
  cannot be told apart. The refusal says the text repeated once masked, and echoes no
  value. The operator settings in `line_overridden` and `project_overridden`, and an
  operator's own free text in a client, which is masked like any message body. A text
  that really holds such a value as an example is masked too, which fails safe.
- **Failure modes.** A secret source that raises fails closed. An event with a covered
  text field is refused as rejected, naming the kind of failure and no value, and
  nothing is stored, because pattern masking alone would record a literal value it
  cannot know. An event with no covered field does not ask the source. A password with
  `@` is masked whole, and in a URL one with `/`, and a scheme-less password with `/`
  is not masked, since it cannot be told from a path. Masking is by pattern and is not
  the disclosure scan.
- **Callers that can meet the refusal. Review of 151cbcd.** A tool a line or an
  operator calls returns the refusal as an error that names the kind of failure and
  stores nothing. An event the daemon makes for itself, which is the walk gap, the
  restart note, the engine stop note, a budget stated from an engine's report, the
  compaction wake, a run's note to its holder and a task end's note, is dropped with a
  line on the standard error that names what was lost and not its text, and the flow
  it belonged to goes on. A task end is noted when it is reported again. Records that
  already caught a rejection keep doing so. **Review of 7744806.** About seventy
  appends in the scheduler can meet the refusal, so one guard stands at the tick. Each
  due wake and each line's update is an action, and an action that meets the refusal
  ends alone. Its event was never stored, so its state is as it was and it is tried
  again at the next tick, the other actions of the tick go on, and the loop rests one
  second, which is not a fault and is not doubled. The standard error says so once for
  each episode, naming the action and never any text. An append made in a turn's own
  task is not inside the guard, and still reaches the fault handler, which pauses and
  tries again. The forwarder's outage note leaves its state unchanged, so it is tried
  again as well.

## Bounded start

**Required by the operator on 2026-11-01, measured and designed by core for review.
Built in round 15, item 4, in `dromon.statesnapshot` and `EventLog`.** A daemon opens
its log by replaying every event into a state held in
memory, so the time to start and the memory held both grow with the log.

**Measurements. Fact, from runs on 2026-11-01 on the operator's macOS machine.** The
script `tmp/r22/measure.py` in the core worktree, which is not tracked, wrote synthetic
logs whose events all apply. Twelve lines take turns, and each turn records a state
change to running, a note of 900 bytes to the operator or to the next line, its
delivery and reading when sent to a line, a session, a turn end and a state change to
idle. Each log was opened twice with `EventLog`, as `dromond` opens it, in a fresh
process, after the import of `dromon.eventlog`, which took about 0.05 seconds and left
a peak resident set of about 25.5 MiB.

| Log | Events | Messages kept | Open, seconds | Peak resident set, MiB |
|---|---|---|---|---|
| 3.3 MiB, 3,506,176 bytes | 10,779 | 1,700 | 0.116 and 0.116 | 31.4 and 31.5 |
| 33 MiB, 34,611,200 bytes | 106,729 | 16,850 | 1.136 and 1.152 | 56.1 and 55.9 |
| 330 MiB, 346,071,040 bytes | 1,067,495 | 168,550 | 11.716 and 11.966 | 305.1 and 305.9 |

**Inference.** Opening costs about 11 microseconds an event, and the state holds about
0.85 MiB of memory for each MiB of log, mostly message bodies, since `State.messages`
keeps every message's body for as long as the daemon runs. Both are linear, so a log a
hundred times this project's is still opened in about 12 seconds. This project's own
log, about 3.3 MiB, was not measured, since `.dromon/` is closed to lines. Its mix of
events differs from the synthetic one, so its numbers may differ by a constant factor.
The operator can measure it with `uv run python -I tmp/worktrees/core/tmp/r22/measure.py
replay .dromon/events.sqlite` from the main checkout, which replays it read only and
holds the log's read lock while it does, about a tenth of a second at that size, in
which a running daemon's next commit waits. The
start of `dromond` does more than open the log, such as applying the project file,
which these numbers leave out.

**Other whole reads. Fact, from the code.** Two paths read every entry of the log into
a list after the start. `Daemon.forwarder` does so once when mail waits for a peer, and
`Scheduler._recorded_time` does so once a line after a restart, for the time of the
operator mail since its brief. Each costs about what the open costs, for each line, and
holds the whole log in memory while it runs. They are debt that a bounded start must
also remove, by a time index kept beside the state.

**Design, as built.** A snapshot of the state at a sequence number, from which replay
resumes.

1. **The file.** `events.sqlite.snapshot` beside the log, owner only, mode 0600,
   written to a file beside it and moved into place with one rename, so a reader sees
   the old file or the new one whole. It is opened without following a link, and a
   file that is not regular, that a group or other may read or write, or that is over
   4 GiB is refused. After the move the directory is synchronized, so the new name is
   durable. **Review of `49236ad`, item 2.** A crash before the move leaves the earlier
   snapshot or none, and may leave the `.writing` file, which no start reads. A crash
   after the move and before the directory is synchronized may lose the new name on
   some file systems, which leaves the earlier snapshot or none. Either way a start
   finds a whole snapshot of the state at its own `N`, or none, and replays the events
   after it or the whole log. A directory that cannot be synchronized is recorded as a
   failed write. It holds a format version, the log's schema version, a state
   version that the code raises whenever the meaning of applying an event changes,
   the SHA-256 of the shape of the state's types, the sequence number `N` it was taken
   at, the SHA-256 of the stored rows 1 and `N`, each its `recorded_at` and `payload`,
   the state and the recorded times as canonical JSON, and the SHA-256 of all of the
   above.
2. **The encoding.** Reflective over the frozen records of `dromon.model.State`. A
   record is an object of its fields, a map is an array of pairs in its order, since
   the order of several maps is meaningful, a set is a sorted array, and a value of a
   union of records is tagged with the record's name. The shape digest is taken over
   the tree of field names and types, so any change to a field of the state changes
   it, and a snapshot of another shape is ignored without a version raised by hand.
   The decoder is compiled once for each type and as strict as the event codec, so a
   value of the wrong type is refused and not coerced.
3. **Taking one.** The daemon writes one at a clean stop when the log grew since the
   last, and within the append that brings the growth to `SNAPSHOT_EVERY`, 10,000
   events. A snapshot that fails to write is recorded in the status and never raised,
   since the append it follows is stored already. `dromond --check` never writes one.
4. **Starting.** The daemon reads the snapshot inside one read transaction of the log,
   checks the file, its digest, its versions and its shape, reads rows 1 and `N` and
   checks their digests, decodes the state and the recorded times, and applies the
   events from `N + 1` as replay does.
5. **Falling back.** A snapshot that is missing, unreadable, refused as a file, of
   another version or shape, whose digest does not verify, whose `N` lies past the
   log's end, whose rows 1 and `N` differ, or that does not decode is ignored, and the
   daemon replays the whole log. The reason is printed on standard error at the start
   and given as `event_log.start` in the daemon status, with `snapshot` one of `used`,
   `ignored` or `off`, `snapshot_seq`, `reason`, `replayed`, `open_milliseconds` and
   `write_failure`. A snapshot is a cache and never the record, so ignoring one loses
   nothing.
6. **Checking.** `dromond --check` replays its copy of the log in full through `N`,
   encodes that state and the recorded times, and compares them byte for byte with the
   snapshot's. A difference means the code applies events differently from the code
   that took the snapshot without a new state version, and fails the check. No
   snapshot, or one a start would ignore, passes with its reason, since a start would
   then replay the whole log.
   - **After every start from a snapshot.** **Review of `49236ad`, item 1, required by
     the operator.** A snapshot whose digests are valid and whose state is not a replay
     of the log passed every check of item 4 and was restored. A start that restored
     from a snapshot now copies the log in the bounded ranges a check's copy uses, off
     the event loop, replays the copy through `N`, and compares the digest of that
     state and those times with the digest the snapshot named. A match sets
     `verified` to `passed`. A mismatch is named by one fixed word, `state_differs`,
     `log_corrupt`, or `check_failed` with the exception's type, and never its text.
     Every line is then held in memory, the snapshot is deleted, the daemon log says
     so, `event_log.start` gives `verified` `failed`, `mismatch`, `mismatch_class` and
     `restart_required` true, and `dromon status` prints a `RESTART REQUIRED.` row.
     Only a restart lifts the hold, and it replays the whole log, since the snapshot is
     gone. No snapshot is written while the check is pending or after it failed, so a
     stop before the check ends leaves the earlier file, which the next start checks
     again.
   - **Decided by core, for review.** The hold appends no `line_held`. The state is the
     one a replay did not confirm, so an event checked against it may be one the
     replayed state refuses, and the log would then fail to replay at the next start.
   - **Trust.** The snapshot sits in the state directory beside the log, which
     `protected_dirs` denies to confined lines, so it is on the same protected boundary
     as the log, and the mode check does not stop the owner. Whoever can forge the
     snapshot can also alter the log. The check detects corruption, a forgotten state
     version, and a file changed by a process that could write the snapshot but not
     the log. It does not detect a determined owner of the files.
   - **No turn starts until the check ends.** **Re-review of `2cede6e`, required by
     the operator.** A line could start a turn while the check ran and append events
     checked only against the restored state, and a turn running when the check
     failed was not cancelled. No line now starts a turn while the check is pending,
     and mail that arrives meanwhile waits, so no turn appends against the
     unconfirmed state and none is running when the check fails. The scheduler wakes
     when the check ends, and a waiting turn starts at once after a pass.
   - **What the wait costs.** **Fact, from runs on 2026-11-05 on the operator's
     macOS machine, with this branch's uncommitted code.** The script
     `tmp/r26/measure_check.py` in the core worktree, which is not tracked, restored
     each synthetic log above from its snapshot and ran the check three times. The
     check took 0.258 seconds at best and 0.700 at worst for the 3.3 MiB log of
     10,779 events, and 2.438 and 3.518 seconds for the 33 MiB log of 106,729 events.
     **Inference.** It costs about twice a full replay, since it copies the log
     before replaying it, so a start from a snapshot is again slower to its first
     turn than a start without one, though it serves clients sooner. This project's
     log was about 4.4 MB on 2026-11-01, so its first turn waits about a third of a
     second, and a log ten times larger waits about three seconds. The wait was not
     measured on this project's own log, which `.dromon/` closes to lines.
   - **Residual.** The events the daemon appends without a turn while the check runs
     were checked against the restored state. They are the recovery before the
     scheduler starts, an operator's command, mail from a peer and a scheduled wake
     that falls due. A stop waits for a running check, since a thread cannot be
     interrupted. A check that ended with an error outside its own handling would
     leave every line waiting until a restart, which fails closed, and the daemon log
     names it only at the stop. A state that decodes to the replay but was not written
     in the canonical form also differs, which fails closed.
   - **The state version is pinned.** **Review of `49236ad`, item 3.** A change of
     meaning that keeps the shape and does not raise the state version is invisible to
     a start. `tests/test_state_version.py` pins each state version with the digest of
     the types of the state and of every event, written the same way on each Python
     version the gate runs, and with the digest of the state the codec scenario
     replays to. A change to either fails the test until the version is raised and a
     pin appended, and a pin under a version already pinned is refused. Version 2 is
     the round 15 follow-up. Version 3 is the merge of `feat/r15-runs-disclosure`,
     whose disclosure passes, bytes and run tails failed the pin of version 2. **Fact, observed on 2026-11-05.** The shape digest of item
     2 differs between Python 3.12 and 3.15, since it uses Python's own text of a type,
     which is why the pin uses another digest.
7. **What it does not bound.** Memory. A snapshot bounds the start time by the events
   since `N`, but the state it restores is as large as the state replay builds.
   Bounding memory needs the state to keep only the bodies of messages still open, and
   to read the others from the log by sequence number when a view asks, which is a
   separate change. The log itself is never compacted, since it is the record.
8. **Rollback and porting.** The snapshot is a cache beside the log, so an older daemon
   ignores the file and no floor moves. A daemon that returns to this code after an
   older one appended finds the rows past `N` and replays them, and one whose row `N`
   differs ignores the snapshot. A port need not read the snapshot format and may
   rebuild from the log, as replay always can. **Porting hazard.** The encoding reads
   the Python types of the state, so a port that wants to read the file must carry the
   same field names and orders, and its own shape digest would differ in any case.

**Measurements, before and after. Fact, from runs on 2026-11-05 on the operator's macOS
machine, with this branch's uncommitted code.** The script `tmp/r25/measure_start.py`
in the core worktree, which is not tracked, opened a copy of each synthetic log above
three times without a snapshot, once to write one, and three times with it, and kept
the best of each three. The 330 MiB log was not measured again.

| Log | Without, seconds | First start, replay and write, seconds | With, seconds | Snapshot, bytes |
|---|---|---|---|---|
| 3.3 MiB, 10,779 events | 0.120 | 0.202 | 0.039 | 2,632,326 |
| 33 MiB, 106,729 events | 1.226 | 1.987 | 0.402 | 26,046,124 |

**Inference.** A start from a snapshot takes about a third of a full replay at both
sizes, and still grows with the state, since decoding the state is linear in its size.
Writing one took about 0.08 and 0.76 seconds, the difference between the first start
and the replay, and an append that reaches `SNAPSHOT_EVERY` waits that long, about
once in 10,000 events. A first decoder, which resolved the type hints at each value,
restored more slowly than a full replay, 0.190 against 0.124 seconds at 3.3 MiB, which
is why the decoder is compiled once for each type.

**Residuals.**

- The snapshot holds every message body the state holds, as the log does, so it is as
  sensitive as the log and is kept owner only beside it.
- An append that reaches `SNAPSHOT_EVERY` blocks for the encoding and the write, as
  measured above, rather than writing between turns, which would need the scheduler
  to ask for it. **Hypothesis.** At this project's rate of events this is rare enough
  not to matter, and it is not measured under load.
- A daemon killed between appends writes no snapshot at stop, so its next start
  replays from the last snapshot taken, which is correct and slower.
- The two whole reads above are not changed by the snapshot.
- A reason for ignoring a snapshot names the type that failed to decode and never the
  stored value, since the reason reaches the daemon status, which lines read, and a
  stored value may be any text the log holds.

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
- Compaction of long logs. Bounded start above builds a snapshot, which bounds the
  start's replay and not the memory the state holds, and the log is never compacted.
- Migration between schema versions. Version 1 refuses every other version.

## Round 16 closing diagnostics

Fact. `line_turn_ended` may carry `closing_reason` beside its fixed failure cause
and exception class. It is absent on older events and on non-exception failures.
A failed event alone may carry it. Its maximum length is 240 characters with no
ASCII control characters. Replay retains it in the latest failure status.

Fact. The scheduler takes only the first exception line, masks credential patterns
before truncating, replaces nonprinting characters, and applies the line disclosure
screen and its budget before appending the event. A screening error, refusal or
spent budget withholds the diagnostic and exception class. Ordinary and self-started exception paths
use the same screen. Their activity and stderr failure notices no longer interpolate
the original exception. The exception class retains its existing identifier bound and is screened together
with the diagnostic. Snapshot state version 5 invalidates older snapshot caches.

Limit. Credential masking recognizes documented patterns in `redact.py`. An unknown
bare credential needs the project disclosure list to be withheld. The diagnostic
is engine-originated data and grants no authority. Only the retained first line is
screened. No later exception line or suffix beyond the bound is persisted in this
field. Existing historical events and separate engine evidence files are unchanged.

Rollback hazard. A daemon before this change refuses events carrying
`closing_reason`. The first compatible commit is pending on
`fix/r16-grok-continue`. The wire format version remains unchanged because this is
an optional event field. A port must preserve the character bound and screening
order rather than rely on Python exception formatting.
