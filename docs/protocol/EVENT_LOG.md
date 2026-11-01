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
| `line_configured` | `line_id`, `config` text of at most 262,144 bytes | The line exists. The text is the canonical JSON of [`LINE_CONFIG.md`](LINE_CONFIG.md), validated by the daemon before it appends, and replaces any earlier configuration |
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
| `disclosure_checked` | `line_id`, `checked_at` time, `matched_lines` whole number from 0 to 1,000,000 | The line exists. Records that the line checked text, one file, or a list of files with `check_disclosure`, or that a line tool refused text for disclosure, and how many lines held a term of the project's disclosure list. The text, its name and any term are never recorded. Applying it keeps the line's latest 10 check times, for the rate limit [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. Added on 2026-10-28 |
| `message_delivered` | `message_id` | The message exists and was not delivered, returned or left unconfirmed. For a line, delivery is into a turn. For an operator, delivery is acceptance by the operator's relay or display in a client. For a party of a peer, it is the peer's `accepted`, and the message leaves the peer's outbox |
| `message_returned` | `message_id`, `code` one of the codes of returned mail in [`FEDERATION.md`](../architecture/FEDERATION.md), `note_id` message identifier or `null` | The message exists, is to a party of a peer, and was not delivered, returned or left unconfirmed. When its sender is a line, `note_id` is a new identifier that no message, withdrawn wake or pending scheduled wake holds, and it is `null` otherwise. The message leaves its peer's outbox undelivered, and a line sender receives a note from itself under `note_id` whose text the model derives from the code. Added on 2026-10-28 |
| `message_unconfirmed` | `message_id`, `note_id` message identifier or `null` | As for `message_returned`. The message expired after a copy of it may have reached the peer without an answer, so whether it was delivered is unknown, and a line sender's note says so. Added on 2026-10-28 |
| `wake_requested` | `wake_id`, `line_id`, `message_id` | The wake does not exist, the line exists and is not stopped, and the message exists with the line as its recipient |
| `wake_issued` | `wake_id` | The wake exists and was not issued, and its line is idle |
| `wake_scheduled` | `line_id`, `message_id`, `due` due time, `note` wake note, `task_id` task identifier or `null` | The line exists, no message has the identifier, and no other line's pending scheduled wake reserves it. Becomes the line's one pending scheduled wake, replacing any earlier one, and reserves `message_id` for its note. When `task_id` names a running background task, applying the matching `task_noted` event withdraws the wake atomically. Events written before this field was added decode with `null`. A `request`, present only for a keyed `wake_after` call, is as for `message_sent` |
| `wake_withdrawn` | `line_id`, `message_id` | The line exists and its pending scheduled wake reserves `message_id`. Removes that wake, so it never fires, and its identifier is kept, so a later `message_sent`, `task_noted` or `wake_scheduled` that takes it is refused. Only the daemon records it, when the engine reports any compaction while the wake `compact_session` scheduled remains pending, or, from 2026-10-26, when an operator's interrupt stops the line's turn |
| `restart_noted` | `line_id`, `message_id`, `body` note of at most 2,048 bytes | Checked as `task_noted` is. Records a note from the line to itself, already sent, saying that a daemon restart ended its turn at a safe point, as [`RESTART.md`](RESTART.md) specifies, so the restarted daemon wakes the line. Only the daemon records it, as the turn ends. Decided by the operator on 2026-10-27 |
| `task_noted` | `line_id`, `message_id`, `task_id` of at most 128 bytes, `status` of at most 32 bytes, `body` note of at most 8,192 bytes | The line exists, no message has the identifier, and the note identifier is not reserved by a scheduled wake. Records a note from the line to itself saying that a background task ended. If the line's pending scheduled wake names this `task_id`, the event atomically removes it, keeps its identifier among the withdrawn wakes, and its body names the cancelled wake and says the task ended the wait. This task note is not suppressed by the ordinary per-line task-note limit. An end between turns wakes the line, unless its engine has already begun an admitted turn on it, in which case that turn consumes the note. Ends during a turn or for a task the line stopped are recorded as delivered without a wake. Only the daemon records it, from the engine's own report |
| `line_cost_marked` | `line_id`, `engine_session` of at most 256 bytes, `total_micro_usd` a whole number from 0 to 10^15 | The line exists and the session is not empty. Records the running cost total the line's engine reported for its session after a turn, in millionths of a dollar, as the line's cost mark, from which the next turn's cost is measured |
| `line_turn_ended` | `line_id`, `workdir_commit` commit or `null`, `unreported`, present only as `true`, and `failed`, present only as `true`, from round 14, when the turn failed, which keeps the line failed in the state until a turn ends without it, new mail reaches the line, `line_released` or a restart note. **Rollback hazard.** A daemon older than `failed` refuses an event that gives it | The line exists. Records the commit checked out in its worktree as its latest turn ended, or `null` when it could not be read, so a moved-worktree notice can compare against the last turn after a daemon restart. Only the daemon records it, however the turn ended. `unreported` is `true` when the line's engine returned no report for the turn, as when its prompt could not be composed, a check before the engine failed, the engine raised or the turn was cancelled, and is omitted otherwise. Such a turn retires none of the line's own notes, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, since its prompt may never have reached the engine. `false` is refused, and an absent field decodes as `null`, so a log written before the field reads every turn end as reported. **Rollback hazard.** A daemon that predates the field rejects an event that holds it, so once a turn has ended unreported, an older daemon cannot read the log, as for a request key above. `unreported` added on 2026-10-30 in the round 8 review |
| `processes_left` | `line_id`, `cause` one of `exited`, `stopped`, `orphaned`, `detached` and `swept`, `pids` an array of 1 to 64 whole numbers above 1 and below 2^31, `commands` an array of as many texts of at most 1,024 bytes, `more` a whole number from 0 to 1,000,000 | The line exists. Records processes the line's engine left running, with the pid and command of each and how many more were left than are named, as the line's latest record. The first three causes are those of a launcher under the `leave_background` stop policy, when the harness exited, was stopped or found the daemon gone. `detached` is for processes found by the line's mark after its engine ended. `swept` is for processes the daemon's sweep of a launcher that exited before it finished found still running. Each command is a summary, the executable's name without its directory and its arguments with credentials masked, as `dromon.redact` describes. A record written before 2026-10-27 may hold a whole command. The whole command is kept only in the launcher's report file, readable by its owner alone. Only the daemon records it, and it makes no process Dromon's to end. Added on 2026-10-26 |
| `stop_unverified` | `line_id`, `note` of 1 to 1,024 bytes | The line exists. Records a stop of the line's engine that Dromon could not verify was complete, as the line's latest, which its status shows as `processes.stop_unverified`. Recorded when the launcher could not read the process table at its stop, or when it exited before it finished its stop and the daemon swept its harness's group. Only the daemon records it. Added on 2026-10-27 |
| `walk_limited` | `note` of at most 1,024 bytes, possibly empty | Always applies. Records what the daemon's platform keeps the launcher's walk of the process table from reaching, which every line's status shows as `processes.walk_limit`, or with empty text that nothing is. Only the daemon records it, as it starts, and only when it differs from the latest, so a platform's gap is recorded once. Added on 2026-10-26 |
| `question_withdrawn` | `message_id` | The message is a question and is open |
| `question_superseded` | `message_id`, `superseded_by` | Both messages are open questions, they differ, and they come from the same line |
| `context_handed_off` | `line_id`, `cause` of `rebound` or `cleared`, `outcome` of `given` or `opted_out`, `message_ids`, at most 64 identifiers, `chars`, a whole number from 0 to 1,048,576, and `sha256`, a lowercase hexadecimal SHA-256 digest when given and empty otherwise | The line exists and is owed a handoff for this cause, which `line_rebound` and `line_session_cleared` set, as their `rebound` and `cleared`. An `opted_out` handoff selects nothing, with no identifiers, zero `chars` and an empty digest. Ends the handoff. Only the daemon records it, once the engine ran the turn that carried it, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. Added on 2026-10-27 |
| `role_declared` | `role_id`, `workdir`, `handoff_seconds` a whole number from 60 to 86,400, `seats` an array of at most 16 distinct line identifiers, `failover` failover policy, `handoff_below_percent` a whole number from 0 to 100 | The role was never retired, and does not exist, or exists and this changes any of its fields, and its workdir changes only while no seat owns it. The handoff bound is how long an owner asked to hand the role over has before the operator is asked to recover it instead, an hour by default. `seats` is the declared order a failover follows, and may name a line before it is bound. `failover` says whether the harness passes the role on itself when its owner cannot act. The owner is asked to prepare a handoff when its provider's stated budget is above 0 and below `handoff_below_percent`, and never when it is 0. A record written before step 3 of roles has only the first three fields, and decodes with an empty order, `automatic` and 10. Added on 2026-10-27, and the last three fields the same day, before any release |
| `seat_bound` | `line_id`, `role_id`, `may_own` boolean | The line and the role exist, the line is a seat of no other role, and this is a new seat or changes the seat's `may_own`. A seat whose `may_own` is false never owns its role, and an owner cannot be made one. Added on 2026-10-27 |
| `seat_unbound` | `line_id`, `role_id` | The role exists, the line is its seat, and the line does not own it. Ends the binding, and the seat's place in the role's notice counts. The line stays a line with its configuration, mail, leases and state. Only an operator's `role_update` or `role_delete` records it, as [`CLIENT.md`](CLIENT.md) specifies. **Rollback hazard.** A daemon older than round 10 does not know the kind, so once one is recorded, it cannot read the log, as for `unreported` above. Added on 2026-10-31 in round 10 |
| `role_retired` | `role_id`, `by` operator address, `retired_at` time | The role exists and is vacant, no line is its seat, and `by` is a registered operator. Removes the role, and its identifier is never declared again, since an identifier is never reused within its kind. Its journal entries stay. Only an operator's `role_delete` records it, after a `seat_unbound` for each seat. **Rollback hazard.** As for `seat_unbound`. Added on 2026-10-31 in round 10 |
| `role_held` | `role_id`, `holder` of `file` or `operator`, `by` operator address, `held_at` time | The role exists, `by` is a registered operator, and this changes the role's holder. A role with none is held by the file. A role command records `operator`, and a start or a reload records `file`, by the project's operator, once the file's table agrees with the role, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. **Rollback hazard.** As for `seat_unbound`. Added on 2026-10-31 in round 10 |
| `ownership_changed` | `role_id`, `owner` and `previous` line identifiers or `null`, `cause` ownership cause, `by` address, `entry_id` or `null`, `changed_at` time, `back_to` line identifier or `null` | See the ownership rules below. Added on 2026-10-27, and `back_to` the same day, before any release |
| `role_noted` | `role_id`, `entry_id`, `line_id`, `kind` entry kind, `body` text of 1 to 65,536 bytes, `workdir_commit` and `daemon_commit` commits, `noted_at` time | No entry has the identifier, the role exists, so a retired role takes no entry, and the line is its seat. A `handoff` or `recovery` entry is by the role's current owner, and a `note` by any seat. The daemon stamps both commits itself from `HEAD`. Added on 2026-10-27 |
| `brief_set` | `line_id`, `task` text of 1 to 4,096 bytes, `decisions` and `next_action` text of at most 4,096 bytes, `artifacts` an array of at most 32 paths, each of 1 to 1,024 bytes on one line, `workdir_commit` and `daemon_commit` commits, `set_at` time | The line exists. The text fields and paths together are at most 16,384 bytes. Replaces the line's previous standing brief, which only `set_brief` records, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. The text is the line's own and untrusted. The daemon stamps both commits and the time itself, and leaves a commit empty when it cannot read it. Added on 2026-10-29 |
| `budget_stated` | `provider` identifier, `remaining_percent` a whole number from 0 to 100 or `null`, `resets_at` time or `null`, not both `null`, `source` budget source, `note` text of at most 1,024 bytes, `stated_at` time, and from 2026-10-31 optional `window`, an identifier | Always. An event without `window` replaces the provider's earlier statement. An engine event with a window updates that window's snapshot; the state keeps the lowest remaining percentage among unexpired engine windows. Older window snapshots are discarded when their reset passes. It is a snapshot and later turns do not adjust it. **Rollback hazard.** A daemon predating `window` refuses a log after one is recorded. Added on 2026-10-27; `window` added on 2026-10-31 |
| `budget_handoff_requested` | `provider` identifier, `role_id` identifier, `remaining_percent` a whole number from 1 to 100, `threshold` a whole number from 0 to 100, `requested_at` time | The role exists and has an owner, its threshold matches, its current provider budget matches `remaining_percent` and is below `threshold`, and this provider-role pair has not been asked since its budget last rose above the threshold. Records one planned handoff request, which is re-armed only above threshold. **Rollback hazard.** A daemon predating round 11 does not know this kind and cannot read a log after one is recorded, as for `seat_unbound` above. Added on 2026-10-31 |
| `lease_acquired` | `name` and `line_id` identifiers, `acquired_at` and `expires` times, `expires` the later, and `note` text of at most 500 bytes, present only when given, from round 14. **Rollback hazard.** A daemon older than `note` refuses an event that gives it | The line exists, and no lease is recorded under the name or the recorded one's `expires` is not after this `acquired_at`. Records the line as the lease's holder until `expires`, replacing an expired one. See the lease rules below. **Rollback hazard.** A daemon older than 2026-10-31 does not know the kind, so once one is recorded, it cannot read the log. Added on 2026-10-31 |
| `lease_released` | `name` and `line_id` identifiers, `cause` lease end | The line holds the lease under the name, expired or not. Ends it. `released` is the holder's own `release_lease`, and `undeclared` is recorded by a reload or a start whose project file no longer declares the name. **Rollback hazard.** As for `lease_acquired`. Added on 2026-10-31 |
| `run_started` | `run_id` and `line_id` identifiers, `role_id` and `previous` identifiers or `null`, `command` identifier, `argv` an array of 1 to 64 texts of at most 1,024 bytes each, the first not empty, `workdir` an absolute path and `output_file` a path relative to it, each of at most 1,024 bytes, `pid` a whole number above 1 and below 2^31, `process_started` and `started_at` times | No run has the identifier and the line is not stopped. `role_id` is the role the line is a seat of, or `null` when it is none, and a seat owns its role. The run's scope is its role, or its line when it has none. No run of the scope is open, the scope's latest run is not `lost` without a `run_cleared`, and `previous` is the scope's latest run, or `null` when it has none. `pid` and `process_started` name the run's supervisor, as [`RUNS.md`](../architecture/RUNS.md) specifies. **Rollback hazard.** As for `lease_acquired`. Added on 2026-10-31 |
| `run_ended` | `run_id` identifier, `status` run status, `exit_code` a 32-bit whole number or `null`, `null` for `lost`, `output_digest` a SHA-256 digest or `null`, `output_bytes` a whole number or `null`, given whenever the digest is, `note` text of at most 1,024 bytes, `ended_at` time | The run exists and has not ended. Ends it with what its receipt or the process table showed. **Rollback hazard.** As for `lease_acquired`. Added on 2026-10-31 |
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
| `message_sent.asks_reply`, written when a line's `send_message` or an operator's `send` says whether a note asks a reply | `4330502` | Pending | `feat/mail-and-roles` |
| A correction, a note naming a note of the same sender to the same recipient | `4330502` | Pending | `feat/mail-and-roles` |
| `message_acknowledged.reason` | `4330502` | Pending | `feat/mail-and-roles` |
| `line_turn_ended.failed`, written with every failed turn | `4330502` | Pending | `feat/mail-and-roles` |
| `lease_acquired.note` | `4330502` | Pending | `feat/mail-and-roles` |
| `line_retired` | `625f3de` | Pending | `feat/operator-r14` |
| A commit run, a `run_started` with `commit` and `checkout` | `1449c5c` | Pending | `feat/operator-r14` |

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
format itself and are not listed.

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
