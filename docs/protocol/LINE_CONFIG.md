# Line Configuration

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, configuration version 1, written on 2026-10-25. Normative. The Python
module `dromon.lineconfig` implements it.

## Purpose

**Decided by the operator on 2026-10-25.** It must be possible to expose arbitrary MCP
servers to a session line, under either an allow-all or a whitelist model, with the
settings held per line. A line's configuration names its built-in Claude Code tools, the
MCP servers it connects to, and which tool calls are allowed.

## Document

```json
{
  "builtin_tools": ["Read", "Edit"],
  "mcp_servers": {
    "github": {"type": "stdio", "command": "gh-mcp", "args": ["--stdio"], "env": {"TOKEN": "..."}},
    "docs": {"type": "http", "url": "https://docs.example/mcp", "headers": {"Authorization": "..."}}
  },
  "tool_policy": "allowlist",
  "allowed_tools": ["mcp__github__*", "mcp__docs__search"],
  "inherit_account_mcp": false
}
```

Every key is optional, and an omitted key takes the default shown in the table. Unknown
keys, duplicate keys and numbers are refused.

| Key | Value | Default |
|---|---|---|
| `builtin_tools` | Claude Code tool names, each a capital letter followed by letters, digits or `_`. `ViewImage` is Dromon's own name, see [Viewing images](#viewing-images) | none |
| `mcp_servers` | Server name to server, or a Claude Code file of the shape `{"mcpServers": {...}}` | none |
| `tool_policy` | `allowlist` or `allow_all` | `allowlist` |
| `allowed_tools` | Exact tool names, or `mcp__server__*` for every tool of one server | none |
| `inherit_account_mcp` | Also load the MCP servers of the operator's user settings and claude.ai account | `false` |
| `sandbox` | The line's own sandbox document, as [`SANDBOX.md`](SANDBOX.md) specifies, before the project's is merged in. Omitted from the stored JSON when unset | none |
| `settings` | The line's own engine settings, as [`SETTINGS.md`](SETTINGS.md) specifies, before the project's are merged in. Omitted when unset | none |
| `effort` | `low`, `medium`, `high`, `xhigh` or `max`, replacing the driver's effort. Omitted when unset | the driver's |
| `claude_code_prompt` | Run a Claude Code line on Claude Code's own system prompt rather than an empty one. Omitted when false | `false` |
| `project_settings` | Also load Claude Code's project setting source in the line's working directory, as [`SETTINGS.md`](SETTINGS.md) specifies. Refused on Codex and Grok lines. Omitted when false | `false` |
| `plugins` | At most 16 distinct local plugin directories, each absolute or starting with `~/`, as the plugins section below specifies. Refused on Codex and Grok lines. Omitted when empty | none |
| `instructions` | At most 16 distinct files, such as `CLAUDE.md`, whose text is appended to the system prompt at each turn. A relative path is resolved against the project root, whatever the line's `workdir`, and `~` against the home directory. A line whose `workdir` is a worktree of another branch reads the root's file for a relative path, so it names its own file by absolute path. Omitted when empty | none |
| `workdir` | The directory the line's engine runs in, as the working directory section below specifies. Omitted when unset | the project root |
| `workdir_access` | `write` or `read`; read confines the working directory to reads, with the scratch exception below | `write` |
| `writable_paths` | Existing absolute directories a Codex line may write when given `Edit` or `Write`. Refused on other engines | none |
| `keep_session` | Keep a Claude Code or Codex line's engine connected between turns, as the kept sessions section below specifies. Refused on Grok lines. Omitted when false | `false` |
| `fresh_session_each_turn` | Start each turn in a new Claude Code or Codex engine session, without a context handoff or standing brief. Refused with `keep_session`, with explicit `context_handoff = true`, and on adapters that cannot start a new session each turn. Omitted when false | `false` |
| `keep_session_idle_seconds` | Seconds a kept connection waits for the next turn before it closes, a whole number from 60 to 86,400. Omitted at its default | `1800` |
| `context_warn_tokens` | The estimated context, in tokens, past which the line's prompt warns, a whole number from 1,000 to 10,000,000, as the context section below specifies. Omitted at its default | `150000` |
| `allow_compact` | Give a Claude Code or Codex line `compact_session`, to compact its own session. Refused on Grok lines. Omitted when false | `false` |
| `auto_compact_tokens` | An optional earlier context threshold for compaction, a whole number from 1,000 to 10,000,000, as the context section below specifies. Needs `allow_compact`. Omitted when unset | unset |
| `keep_session_max_seconds` | Seconds after the last turn a kept connection may stay open while the engine reports background tasks running, before `keep_session_at_max` applies, a whole number from 60 to 86,400 and not less than `keep_session_idle_seconds`. Omitted at its default | `14400` |
| `keep_session_at_max` | What a kept connection does at `keep_session_max_seconds` with background tasks still running, `close`, `ask` or `keep`, as the ceiling section below specifies. Omitted at its default | `close` |
| `keep_session_hard_seconds` | Seconds after the last turn past which no choice keeps a connection open, a whole number from 60 to 604,800 and not less than `keep_session_max_seconds`. Omitted at its default | `86400` |
| `context_handoff` | Begin the line's first turn in a new engine session, after a rebind or a cleared session, with a selection from its mail, as the context handoff section below specifies. Implied false by `fresh_session_each_turn` when omitted | `true` |
| `recipients` | At most 256 addresses the line may send mail to, local or `peer:party`, each stored canonically, distinct and sorted. The daemon refuses `send_message` to any other party, and records nothing. A line may always send itself a note, and `ask_operator` is not limited. An empty list leaves only notes to itself. Omitted when unset, which allows every party. **Added on 2026-10-28**, so that an operator can seat several lines at an external task with no private channel between them | every party |
| `stop_policy` | `kill_all` or `leave_background`, whether stopping the line's engine, or the daemon, ends everything the engine started or leaves its background descendants running, as the stop policy section below specifies. Omitted at its default | `kill_all` |

A server name is an identifier and may not be `dromon`, which Dromon's own tools use. A
`stdio` server has `command` and optionally `args` and `env`. An `http` or `sse` server
has `url` and optionally `headers`. A URL must use `https`, or `http` on `127.0.0.1` or
`localhost`. At most 256 entries are accepted in any list or mapping, and each text is
at most 4,096 characters.

## Variable references

Any text of an MCP server, its command, arguments, `env` values, url and header values,
may hold `${NAME}`, or `${NAME:-default}` for a default used when `NAME` is unset, as
Claude Code's `.mcp.json` does. The project file must grant each referenced name to the
line through its `secrets` list. At the start of a turn, Dromon resolves references only
from those names, and only into the MCP configuration. The values never enter the
engine process environment or the event log.

- A `${` that does not begin a well-formed reference is refused when the configuration
  is accepted. So is a reference to a variable `SETTINGS.md` reserves for the engine's
  credential, because an MCP server could otherwise carry that credential elsewhere.
- A url is checked against the transport rule when it is accepted, unless it starts with
  a reference, and again after expansion at every turn.
- A turn whose server refers to an unset variable without a default, or whose expanded
  url breaks the transport rule, is refused before the engine starts.

## Policy

- `allowlist` allows the listed built-in tools, Dromon's own tools such as
  `send_message`, and every tool matching `allowed_tools`, and denies everything else.
- `allow_all` allows every tool the engine offers.
- Dromon approves no tool in advance, and every call the engine would ask about
  reaches the line's permission gate. A line's own `allow` rules and hooks may approve
  calls before the gate, as [`SETTINGS.md`](SETTINGS.md) specifies.
- Unless `inherit_account_mcp` is set, the engine runs with strict MCP configuration,
  and a turn is refused when the engine offers a built-in tool not in `builtin_tools`
  or a tool of a server not in `mcp_servers`.

## Viewing images

`ViewImage` in `builtin_tools` lets a line look at a local image. It is Dromon's name,
not a Claude Code tool, and it is the only way a line gets the ability, so a line
without it cannot view an image on any engine. Image generation and editing are never
granted.

- Claude Code offers `Read` to the engine for it, and the gate allows `Read` only for
  a file that passes the image check, whatever the tool policy. A line that also has `Read` is not narrowed.
- Codex enables `view_image`, and Dromon checks each image it views.
- Grok permits `read_file`, narrowed to files that pass the image check for a line
  without `Read`. **Do not enable `ViewImage` on a Grok line until the verify-first item
  6 of [`GROK.md`](GROK.md) has shown that Grok's first `read_file` notice carries its
  input.** Until then the line fails closed and cannot view any image.
- Pi, Cursor and Antigravity refuse the name, as they refuse every built-in tool.

An image must be a png, jpg, jpeg, gif or webp file by name and wholly one complete
container of that type, inside the line's working directory and outside `.dromon`,
`.git` and `secret`, and at most 5 MiB.

**Limit, a path swapped after the check.** Dromon checks the real path, and the engine
then opens the path the line gave. A link or file swapped in between is not caught, and
on Claude Code the engine reads the swapped path. The working directory's own
confinement, the engine's sandbox, is what bounds that read, and Dromon does not add to
it.

**What the check does not bound.** Dromon proves that a file is one complete container
of the type its name claims, and nothing about what the container holds. It does not
decode the image and does not prove that its data is a picture. A well-formed container
can carry up to 5 MiB of chosen bytes with correct checksums, in text and comment parts
such as a png `tEXt` chunk, a jpeg comment, a gif comment extension and webp metadata,
and in its compressed or entropy-coded data. A line given `ViewImage` can therefore
have such a file delivered to its engine, if the file is inside the working directory
and passes the other checks. What the engine and the model make of those bytes is
outside Dromon's control. The capability bounds which files are delivered and how large
they are, not what they contain. `test_a_complete_container_may_still_carry_arbitrary_bytes`
pins this, so a later change that refuses such files is deliberate. **Review of d00da44,
I1.**

[`ENGINES.md`](ENGINES.md) gives the rationale, the limits that only an engine holds,
and the steps the operator runs before relying on it. Nothing was run live when this
was written.

## Dromon's own tools

Every line has Dromon's own tools, whatever its configuration, and each fixes its
sender to the line. External agents have a subset, as [`AGENTS.md`](AGENTS.md)
specifies. Their arguments are untrusted input and are checked strictly, and an
argument outside a tool's schema is refused.

| Tool | Effect |
|---|---|
| `send_message` | Sends a note to a line, an operator or the line itself, delivered in a later tick. With `priority` true, a note to another line is priority mail, rate limited, as [`PRIORITY.md`](PRIORITY.md) specifies. With `issue` true, from 2026-10-31, it raises an issue with an operator, as below |
| `ask_operator` | Asks an operator a question, which never pauses the line |
| `wake_after` | Schedules one wake for the line, optionally tied to a running background task, as below |
| `list_mail`, `read_mail`, `acknowledge_mail` | Page through, open and acknowledge the line's mailbox |
| `list_sent` | Lists what became of the line's sent mail |
| `acquire_lease`, `release_lease` | Take and give up a lease the project declares, as below |
| `start_run`, `run_status`, `stop_run` | Offered while the project declares a run command, or while the line's scope has a run to read. Start a declared command as a supervised run that outlives the turn, the seat and the daemon, show the scope's latest runs with their receipts, and stop its open run, as [`RUNS.md`](../architecture/RUNS.md) specifies. A seat must own its role, and every line needs write access to its working directory. A lost run blocks its scope until no process is left in its supervisor's group or session, or an operator clears it. Added on 2026-10-31, and the last two sentences in the review of that day |
| `line_status` | Shows a compact read-only status by default, with diagnostic detail and long lists available by argument, as below |
| `role_status` | Offered only to a seat of a role. Shows the role's worktree, whether the project file or an operator holds it, its declared failover order, failover policy, handoff threshold, owner, the seat a failover would pass it to now and the latest change of owner, its seats in the failover order with whether each is usable for a failover, their condition, worktree access, whether each waits because its engine cannot confine it, model, provider and stated budget, each seat's latest three background task ends with the digest of their output, and its latest five journal entries, the newest with its body, read only, as [`ROLES.md`](../architecture/ROLES.md) describes. Added on 2026-10-27, the task ends on 2026-10-28, the holder and the order on 2026-10-31 in round 10, and whether the project file declares the role, how a role an operator holds differs from it, and the lines that wait beside the owner, in the round 10 review |
| `record_role_note` | Offered only to a seat. Records a journal entry of `kind` `note`, by any seat, or `recovery`, by the owner, which the daemon stamps with the role worktree's commit, its own commit and the time. A handoff entry is written only by `relinquish_ownership`. Added on 2026-10-27 |
| `relinquish_ownership` | Offered only to a seat, and refused unless it owns its role. Records the handoff `note` as a handoff entry and gives the role to the seat `to`, or with no `to` to the seat the handoff that gave it the role named as `back_to`, or else leaves it vacant. A return to a seat that can no longer own the role leaves it vacant and says so. Called inside the owner's own turn, which is how a seat calls it, the request is checked at once and the change is made when that turn ends, with the entry stamped then, and the tool says so. A second call in the same turn is refused. A change that no longer applies when the turn ends is not made, and the operator is told. A successor is awakened and sent a note from the harness, `!dromon`, saying the former owner handed it the role, and the former owner's engine is dropped, so its next turn is built read only. `back_to`, which needs `to`, is the seat the role returns to when the successor next relinquishes without naming one. Added on 2026-10-27 |
| `take_ownership` | Offered only to a seat that may own its role. Takes the role, with cause `recovery`, only on a ground the harness observes, which is a vacant role, an owner held at a plan limit its engine reported in this daemon run from a shape observed live or typed by the Agent SDK, never from inferred wording, or an owner whose provider budget is recorded as 0 percent with no reset time passed, and never while the owner has a turn in flight. The `reason` of 1 to 4,096 bytes and the ground are recorded as a note entry that the change rests on. Added on 2026-10-27 |
| `set_brief` | Records the line's standing brief, its `task`, its open `decisions`, the `artifacts` its task concerns and its `next_action`, as one `brief_set` event that replaces its previous brief and that the daemon stamps with the time, the worktree's commit and its own. Only `task` is required. Each text field is at most 4,096 bytes, at most 32 paths are named, each on one line and at most 1,024 bytes, and the whole is at most 16,384 bytes. An unknown key is refused, and the text is checked against the disclosure list as `record_role_note` is. The brief leads the line's first turn after its engine session is new, is shown by `line_status`, and fills the role transition package where a journal entry is silent, as below. Added on 2026-10-29 |
| `propose_change` | Records a proposed patch, file list and message for operator review; it does not change project files |
| `read_instructions` | Shows the instructions the line's next turn receives, as below |
| `write_scratch` | Writes a file under the line's scratch in its working directory, `tmp/` or `tmp/lines/LINE`, as below |
| `check_disclosure` | Checks text, one file, or a list of up to eight files in the line's working directory against the project's configured disclosure list, answering pass or fail with the sum of matching lines in each file and never a term or the file that matched. Files are checked independently, so a match cannot span files. The combined content is at most 1 MiB. One call, including a list, spends one recorded check, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. Added on 2026-10-28 |
| `list_processes`, `stop_process` | List and stop the processes the line started, as below |

**Issues.** **Round 11, item 3, decided by core for review on 2026-10-31.** A line
raises a harness or operator-experience issue unasked with `send_message` and `issue`
true, addressed to an operator. The daemon records it as a message of kind `issue`,
which is open until the operator replies to it or acknowledges it. The operator lists
the open issues with `dromon mail --issues`, closes one with `dromon ack` or with a
reply, and receives each through the operator's relay like any other mail, as
[`RELAY.md`](RELAY.md) specifies.

- **Why a kind of mail.** An issue then has what mail already has, an identifier, a
  sender, durable storage, delivery through the relay, the disclosure check, request
  keys, and a closing act in acknowledgement or reply, so the change is one value of
  the message kind, one tool argument and one listing filter. A separate tracker
  would need its own events, states, listing and relay path for the same lifecycle.
- **Why not a field on `ask_operator`.** A question asks for an answer, holds a place
  in the operator's question queue and may supersede an earlier question, and an issue
  asks for nothing. Raising one as a question would put reports in the queue of
  decisions the operator owes.
- **Why not a convention in a note's text.** A prefix cannot be listed reliably, and a
  line that forgot it would raise nothing.
- **Limits.** Only a line raises an issue, only with an operator, and never as a reply,
  since an issue opens its own thread. An external agent's `send_message` has no
  `issue` argument. An issue has no severity, label or state beyond open and closed,
  and no event marks it reopened. A further issue is a new message.
- **Rate.** A line may have at most five open issues and at most three issues in its
  last twenty sent messages. Both bounds are enforced by replay, so acknowledging an
  issue frees an open slot but does not reset the rate limit. When a bound is reached,
  the line is told to wait for an operator reply or acknowledgment. **Decided on
  2026-10-31, from the relay re-review.** The open cap is the real bound, since a slot
  frees only by an operator's reply or acknowledgment, so the issues an operator is
  sent are bounded by the operator's own actions. The window counts messages, not time,
  so a line that sends ordinary messages between issues is not slowed by it. A rule by
  time would need a time on each message event, which the model does not record, and
  was not added.
- **Rollback hazard.** A daemon older than this kind cannot read an event log that
  holds an issue, since its codec refuses an unknown message kind, so a rollback past
  it needs the log from before the first issue.

**Decided by the operator on 2026-10-26**, from the lines' own reports, for what follows
in this section.

**`line_status`.** It takes optional `detail` and `lists` booleans, both false by
default. The compact JSON answer includes the line's `line_id`, `state`, `condition`,
`held_by`, autonomous turn and cost summaries, `scheduled_wake`, the mail counts
`unread`, `unanswered` and, from 2026-10-30, `own_notes`, the pending message count, `model`, `strategy`, a short context summary and daemon version and
commit. It also includes a `commits` object with the worktree commit, the daemon commit
and a sentence citing both. It includes counts of leases, background tasks, recent turns and other lines,
not their contents. The default answer and every response that includes lists are
bounded to 8,192 UTF-8 bytes. If a response exceeds that bound, it returns a small
object with `status_truncated`. A truncated list response also sets `lists_truncated`
and suggests `detail: true, lists: false` to omit the lists.

Set `detail` to true for diagnostic fields, including the resolved `workdir`,
`engine_session`, kept-session state, process status, event-log status and full context
details. It also gives `sandbox`, the built engine's full effective Claude Code sandbox
document, including resolved write paths and Dromon's added write denials. Its
`shared_git_directory_write_allowed` says whether the shared Git directory itself is
writable through that sandbox, and `shared_git_denied_paths` lists denied paths inside
or around it. A denied descendant can coexist with permission to write elsewhere in
that directory. For an engine without a Claude Code sandbox document, `sandbox` states
why those details are unavailable. The detailed response also reports whether the line
is hibernated and gives the worktree and daemon commits with a sentence that cites both.
Detail mode keeps arrays summarized by counts. Set `lists` to true to receive the long
lists of leases, recent turns, other lines, kept-session tasks and left processes under
a `lists` object. With both arguments true, the tool returns the full diagnostic object
and those lists. No mode returns a configuration document or message body.

**Other lines.** From 2026-10-31, asked by the operator after a line held back work it
had planned for a peer, because its status gave it no reliable view of that peer. Each
entry of `other_lines` is a read-only view of one other line of the daemon, never of
the line asking, in the order of their identifiers. From 2026-11-01, decided by the
operator on review r13, an entry is compact. It always gives `line_id`, `state` and
`condition`, and gives every other field only when it says something, so a field left
out reads as the value in the last column.

| Field | Meaning | Left out |
|---|---|---|
| `line_id` | The other line | Never |
| `state` | `idle`, `running` or `stopped`, as the event log records it | Never |
| `condition` | One word for where the line stands, by the precedence `line_status` gives the line's own: `running`, `hibernated`, `held`, `failed`, `waiting` on a question it asked an operator, or `idle` | Never |
| `hold_cause` | The short cause of the hold: `autonomous_turns`, `cost`, `disclosure_checks`, `git_ssh_changed`, `plan_limit`, `inferred_limit`, `turn_failed` or `stopped`. A hold recorded before causes were gives `unrecorded` | Not held |
| `hibernated` | `true` when an operator hibernated the line | `false` |
| `since` | When the line last was created, started or ended a turn, was held or released, or was hibernated or awakened, to the second in Coordinated Universal Time | The log's time does not parse |
| `unread` | How many messages to the line it has not opened, its own notes left out | 0 |
| `oldest_unread_age_seconds` | Whole seconds since the oldest of those was sent | No unread mail, or its time does not parse |
| `workdir_commit` | The commit at `HEAD` of the line's working directory | Not allowed or not in a checkout |
| `leases` | The names of the leases the line holds | None held |

The entry gave `held` until 2026-11-01. It is no longer given, since `hold_cause` is
given exactly when the line is held.

- **The response bound.** **Decided by the operator on review r13, T1.** When the
  whole response would pass the bound of 8,192 bytes, it keeps as many other lines as
  fit, the first in order, and `other_lines_omitted` beside `other_lines` counts the
  rest. It is 0 when every line is shown, and `other_line_count` gives them all. When
  the other lists alone pass the bound, the response is truncated as before, and
  still carries `other_lines` and `other_lines_omitted` at its top. An entry whose
  lease names alone would pass the bound gives `lease_count` in place of `leases`, so
  at least one entry is given whenever another line exists. No argument pages past
  the lines left out.
- **The cost.** **Decided by the operator on review r13, C1.** The state keeps each
  line's unread mail and its open questions to an operator as indexes, updated as
  events apply and rebuilt by replay, so a line's `condition`, its `unread` and its
  oldest unread mail are read without a scan of the messages. The view is built only
  when `lists` is true. The caller's own counts are still found by a scan of its
  mailbox, as before, and reading each other line's `workdir_commit` reads a file.

- **Never the mail.** The view gives a count and an age, and never a message's body,
  sender, identifier or subject. A hold gives its closed cause and never the hold
  question, whose text is mail to an operator and may carry an engine's error.
- **Mail still waiting.** **Decided by core, for review.** `unread` counts mail not yet
  delivered with mail delivered and unopened, since a held or hibernated line's mail
  waits undelivered, and leaving it out would show such a line with nothing waiting.
  It therefore differs from the line's own `unread`, which counts delivered mail only.
- **The cause is recorded.** The cause is written in the `line_held` event, as
  [`EVENT_LOG.md`](EVENT_LOG.md) gives it, so it survives a restart.
- **The times are the log's.** `since` and the age are read from the `recorded_at` of
  the event log's entries, which the daemon keeps beside its state as it replays and
  appends, so they also survive a restart and need no scan of the log. A message's
  time is kept only while it is unread mail to a line, so the times grow with the
  unread mail and not with the log. **Limits.**
  `since` does not move when a line comes to wait on a question or when its turn
  fails, since neither changes its state, hold or hibernation, so for `waiting` and
  `failed` it names the latest change before. The times follow the daemon's clock, so
  a clock that stepped gives a wrong age, and an age that would be negative is given
  as 0.

When Dromon refuses a tool call, the error names the permission layer or setting that
refused it, such as the line's `tool_policy` or
`sandbox.allowUnsandboxedCommands=false`. When Claude Code reports a sandbox refusal,
its tool error is passed through by the engine so the line can use the reported reason.

**`propose_change`.** It requires a non-empty message, one to 64 unique relative file
paths, and a non-empty Git unified patch whose file headers match the list. Paths cannot
escape the line's workdir or include `.git`, `.dromon` or `secret` components. The
message is limited to 8 KiB and the patch to 192 KiB. The daemon records the proposal in
private project state with the submitting line and its resolved workdir. The operator
can inspect proposals with `dromon proposal list` and `dromon proposal show
PROPOSAL_ID`. Proposal text and patches are untrusted data. Dromon does not apply them,
and lines have no commit operation.

**Row 14 of the T3 Code review, 2026-10-27.** The `event_log` object holds `events`,
the newest sequence number, `bytes`, the size of the log's database file with any
SQLite write ahead log or journal beside it, and `since_start`, with the `events` and
`bytes` added and the whole `seconds` passed since this daemon opened the log. It
gives `events_per_hour` and `bytes_per_hour`, each rounded to a whole number since the
client protocol carries no floats, once the daemon has had the log for 60 seconds, and
`null` before. A size that cannot be read is `null`, never zero, and so
is any growth derived from it. `compaction` is `none`, since nothing compacts the log
yet, so it only grows. The daemon's `status` carries the same fields in its
`event_log`, as [`CLIENT.md`](CLIENT.md) specifies.

**Row 8 of the T3 Code review, 2026-10-27.** `cost_usd` keeps an unknown cost apart
from zero. It holds `estimate`, the sum of the costs turns reported as a decimal string,
or `null` when none reported one and some reported none, `basis`, which is `exact` when
every turn since the release reported a cost, `lower_bound` when some did not, and
`unknown` when none did, `unknown_turns`, how many reported none, `limit`, and a `note`
that says, when any reported none, that the budget holds the line only on what the
others reported. A turn that ended without a report counts as unknown, since it may
have cost something. The budget line at the head of each turn's prompt names the turns
of unknown cost too. Each `recent_turns` entry for a turn also gives `credential`, the
credential the engine reported, `credential_source`, Claude Code's own account of it,
`sandbox`, the sandbox the adapter ran the turn in, `enabled` or `disabled` for Claude
Code and `none` for Pi, and `isolation`, `full` where the engine's table says every
built-in tool is denied before it runs, `partial` where it says not, and `unknown`
where the table does not know, as [`ENGINES.md`](ENGINES.md) specifies. Each is `null`
where the engine did not say, never a default. The Codex, Grok and Antigravity adapters
do not yet report a sandbox. Their tables were filled on 2026-10-27, so a Codex turn's
isolation is `full`, a Grok turn's `unknown` and an Antigravity turn's `partial`.

**Row 5 of the T3 Code review, 2026-10-26.** `condition` names where the line stands,
in this order of precedence, `running`, `held` for an operator, `failed` by its last
turn, `waiting` on a question it asked an operator, or `idle`. A turn that fails, by an
error or a refusal before or during it, or by an engine's report of any subtype but
`success`, holds its line with an operator question that quotes how the turn ended, up
to 600 characters, unless the line sent an operator mail during that turn, which may
explain it. Answering the question releases the line. `failed` therefore remains only
for a line whose own mail accompanied its failure. **Luna's turn of 2026-10-27.** A
turn that failed before it began was recorded only in the line's activity log, and no
operator learned of it for hours. Each entry of
`recent_turns` for a turn gives its `outcome`, `completed` only for the subtype
`success`, `interrupted` when its driver interrupted it, and `failed` for every other
subtype, with the `subtype` itself. `interrupted_for` gives why the driver interrupted
it, such as `priority mail m-1 from @ana` or `a daemon restart`, and is `null` for a
turn it did not interrupt, since the engine's subtype for such a turn, such as
`error_during_execution`, reads as an error. `processes` adds the line's `stop_policy`, as
`left` the latest record of processes left running, as `stop_unverified` the note of
the latest stop Dromon could not verify was complete, and as `walk_limit` what the
daemon's platform keeps the launcher's walk from reaching, each or `null`, as the stop
policy section below specifies. A `recent_turns` entry for a turn outside Dromon's own
gives its `kind`, `interrupted` for a turn an operator stopped, with the cost its
engine reported for it, which counts toward the line's budget.

The `daemon` object holds the following, decided by the operator on 2026-10-26 in round
2.

- **`version`.** The package version. **`version_note`** says that `0.0.0` is the version
  of every unreleased build, so that the commit identifies it, and is `null` for any
  other version.
- **`commit`.** The commit the daemon started from, read once when `dromond` starts, from
  the `HEAD` of the checkout its own code runs from, following loose refs, `packed-refs`
  and a linked worktree's pointers without running git. It is `null` when the daemon
  does not run from a checkout or the commit cannot be read.
- **`loaded_at`.** From 2026-10-29, the time the daemon's scheduler was built, which is
  when the running code was loaded, written as a due time in Coordinated Universal
  Time.
  **Asked by the review role on 2026-10-28,** so that a report citing a stale daemon
  can say since when it has run behind its checkout. The stale note and the `cite`
  sentence of `commits` name it.
- **`checkout_commit`.** The same checkout's `HEAD`, read again at every call, which costs
  a few reads of small files and no process.
- **`stale`.** From 2026-10-31, `true` when the commit checked out in the daemon's own
  checkout holds code under the daemon's package directory that differs from the code
  of the commit it loaded, `false` when the code is the same, and `null` when either
  commit is unknown. **Round 9, decided by core for review.** Staleness is about the
  daemon's own checkout and never about the worktree of the line that asks. A move that
  changes only documents is not stale, and a checkout that returns to the loaded code is
  not stale either, so the warning clears once it no longer holds. Before, any move of
  `HEAD` was stale, and a line in a worktree of its own saw the warning all session
  after a commit of documents. **`stale_note`** then says that the running daemon does
  not have the newer code until an operator restarts it, and names the daemon's own
  checkout.
- **`code_compared`** and **`package`.** Whether the code was compared, and the
  package directory compared, such as `src/dromon`. The comparison runs git, through
  `dromon.codetree`, only for a pair of commits it has not compared before, and caches
  the answer by both commits, so a status call while `HEAD` stays runs no process.
  Where git cannot compare the code, as outside a checkout or without git, any move of
  `HEAD` is stale, as it was before, and `code_compared` is `false`.
- **No wait on git.** **Round 9 review, F4 and F6, decided by the operator.** A status
  or a prompt never waits on git. Each answer comes from the cache, and a question not
  answered yet is answered as unknown at once and queued for one worker thread, which
  runs git off the daemon's event loop, one command at a time, at most 16 questions
  queued. Until git answers for a new pair of commits, `stale` is therefore `true` with
  `code_compared` `false`, as without git, and the next call after the answer says
  what the code shows. Local git answers within milliseconds, so the window is short.
  A definite answer is kept until 256 newer answers push it out. An answer git could
  not give, a command past its 5 second timeout, a git that could not be started, or
  an error status, is kept for 60 seconds and then asked again. **Why 60 seconds.** A
  git that hangs, as on a stalled volume, holds the worker for 5 seconds at each try,
  so one try a minute for each question keeps at most one such git running and the
  worker idle most of the time, while a git that recovers is seen within a minute.
  Before, a timeout was never kept, so every status poll ran git again on the event
  loop and stalled every client for up to 5 seconds, and a git that could not be
  started was kept until a restart.

The `commits` object, decided by the operator on 2026-10-27 with restart to upgrade,
gives what a review or report names, read at every call in the way `checkout_commit`
is read.

- **`workdir`.** The full commit at `HEAD` of the checkout holding the line's working
  directory, or `null` when the directory is not allowed or not in a checkout. Each
  entry of `other_lines` gives the same for its line as `workdir_commit`.
- **`daemon`**, **`daemon_checkout`**, **`stale`**, **`code_compared`** and
  **`package`.** The `daemon` object's `commit`, `checkout_commit`, `stale`,
  `code_compared` and `package`, repeated so the object stands alone.
- **`daemon_behind_worktree`.** From 2026-10-31, asked by review in round 8. `true`
  when the daemon runs code older than the line's worktree, which is when the commit the
  daemon loaded is an ancestor of the worktree's `HEAD` and the two differ under the
  daemon's package directory. `false` when the worktree's `HEAD` is the daemon's commit
  or descends from it with the same code there. `null` when it cannot be said, as for
  a worktree of another repository, which does not hold the daemon's commit, and while
  git has not yet answered. **Round 9 review, F5.** It is also `null` for a worktree
  whose `HEAD` does not descend from the daemon's commit, as on an unrelated branch or
  at an older commit, whose code the daemon may hold more of, or other code than, the
  worktree. It was `false` there before, which said the daemon was not behind a
  worktree whose code it may not contain. It is cached by both commits, as `stale` is.
  The compact view carries it too.
- **`cite`.** One sentence naming both full commits, such as `line core read its
  worktree at commit C, under the daemon at commit D.`, with the commit the daemon's
  own checkout has moved to when it is stale, and a clause saying that the daemon's code
  is older than the worktree's when it is, so a report can quote it whole.
- **`note`.** That both commits are read from `HEAD`. Uncommitted changes in the
  worktree are part of neither, and only `git status` shows them. It also says that
  `stale` is about the daemon's own checkout.

**The prompt's notice of older daemon code.** From 2026-10-31, when
`daemon_behind_worktree` is `true`, the line's next turn leads with a notice that the
daemon runs code from its loaded commit, older than the code at the worktree's commit,
so what the line observes of Dromon's own behaviour is that older code until an
operator restarts the daemon. **Decided by core, for review.** The notice is given once
for each commit of the worktree, not at every turn, so that it does not become noise,
and is given again once the worktree's `HEAD` moves to another commit that is still
ahead of the daemon. `line_status` gives the answer at every call. A turn the engine
started by itself has no Dromon prompt and so no notice. **Round 9 review, F9.** The
record of what was told is held in the daemon's memory, not in the event log, so a
daemon restarted while the worktree is still ahead tells the notice once again, at the
line's first turn after the restart. Since a prompt does not wait on git, the first
turn after the worktree's `HEAD` moves may find git's answer not yet given, and the
notice then leads the turn after.

**Trust and cost of the comparison.** Git runs with the daemon's authority in the
line's working directory, which the line may write. It is given only `rev-parse` and
`merge-base`, which read objects and refs and neither the index nor a hook, with the
`GIT_` variables of the daemon's environment cleared and optional locks off, and with
commits that are object names and a checked path, so no argument can be read as an
option. **Assumption.** A line that could write its checkout's git configuration could
still steer git, as it could any git the operator runs, which is why every enabled
sandbox denies that file, as the Git safety entry of
[Working directory](#working-directory) says. Git runs on the worker thread, never on
the daemon's event loop, so a git that hangs delays only its own answer, by at most 5
seconds a try and one try a minute, as the entry on waiting above says. **Failure
mode.** The worker is a daemon thread, so it never holds the daemon's exit, and a git
it is running when the daemon exits is left to end by itself, which a command that
only reads does without harm.

**`read_instructions`.** One optional argument, `full`, a boolean. The instructions are
those the line's next turn receives as its engine adapter composes them from
`instructions`, the system prompt text of a Claude Code line, marked as following
Claude Code's own prompt when `claude_code_prompt` is set, and the developer
instructions of a Codex line or the rules of a Grok line. Files the engine loads itself,
such as a project's `CLAUDE.md` under `project_settings`, are named but not shown. An
unreadable instruction file is reported as the turn would refuse it.

- **Summary.** Without `full`, or with it `false`, the tool returns JSON with the
  composed text's size in `bytes` of UTF-8, its `sha256` digest in hexadecimal, and its
  `sources` in order, each a `source` and its `bytes`. Claude Code's own prompt and the
  project files the engine loads itself are listed with `bytes` of `null`, since Dromon
  does not hold their text. A line compares the digest with an earlier one to tell
  whether its instructions changed without spending its context on the whole text.
  **Decided by the operator on 2026-10-26** in round 2.
- **Whole text.** With `full` set to `true` the tool returns the composed text itself. A
  `full` that is not a boolean is refused.

**`write_scratch`.** Arguments `path`, relative to the line's scratch in its working
directory, and `text`, at most 262,144 bytes of UTF-8, which replaces the file whole.
The daemon writes the file, so a line without `Write` can keep its own notes and
reports. It creates `tmp/` and the directories under it. It refuses an absolute path,
a path starting with `~`, any `..`, a target that is a symbolic link, which is never
overwritten, and a target that is a directory. The daemon walks the path from the
working directory one component at a time, creating each missing directory and opening
it relative to its parent's open descriptor without following a link, and creates the
file the same way. So a `tmp/` or a directory under it that is a link, or that a racing
process swaps for a link at any moment, refuses the write instead of redirecting it,
as does a file in a directory's place. Text or a path that the project's disclosure
list covers is refused, and nothing is written.

- **Whose scratch.** **Decided by the operator on 2026-10-31, round 10 re-review, N1.**
  A line's scratch is `tmp/` while it owns its role, or is no role's seat and could
  write its working directory, as `lineconfig.can_write_workdir` judges. Every other
  line, a seat that does not own its role among them, has a scratch of its own,
  `tmp/lines/LINE`, so it never writes a file of the owner, whose scratch is `tmp/`
  and which it may read. The tool's description states the rule, and each result names
  the path written. **Decided by core, for review.** An owner keeps `tmp/` even with no
  writing tool, so a role's reports stay where they were. A line's scratch moves when
  it gains or loses its role or a writing tool, and what it wrote before stays where
  it was written.

**Processes.** **Decided by the operator on 2026-10-26.** Claude Code, Codex and Grok
Build each scope their own process controls to the agent's own session, and these
tools do the same for a line.

- **Mark.** `DROMON_LINE_MARK` holds the line's identifier and a random token issued
  for that line once per daemon run, such as `alpha.` followed by 32 hexadecimal
  digits. Each line's token is its own and held only in the daemon, so a line, which
  sees only its own mark, cannot present another line's. Every command a line runs
  carries it, including one started with `nohup` that outlives its turn. A restart
  issues new tokens, so a process from an earlier run is no longer the line's. `DROMON_LINE_MARK` is a reserved variable that a settings document's `env`
  may not set, as [`SETTINGS.md`](SETTINGS.md) specifies.
- **How each engine passes it on.** Codex receives it only for the commands it runs,
  through its `shell_environment_policy.set`, as [`CODEX.md`](CODEX.md) specifies, so
  Codex's own processes never carry it. **Fact, observed live on 2026-10-26.** Put in
  the app-server's environment instead, the mark was filtered from a Codex line's shell
  commands and carried by Codex's own helper processes, so this was changed. Claude
  Code and Grok Build receive it in their environment, and the engine, a direct child
  of the daemon, is left out of the listing. **Inference, not tested live.** What the
  engine starts carries the mark too: on Claude Code a line's configured stdio MCP
  servers and short-lived search processes, and on Grok the Dromon bridge that serves
  the line's own tools. These are the line's own session, and stopping one affects
  only that line.
- **`list_processes`.** No arguments. It returns, as JSON, every process of the
  daemon's user whose environment holds exactly the calling line's mark, with `pid`,
  `elapsed` as `ps` reports it, and `command` cut to 200 characters. The daemon and the
  engines it started directly are left out.
- **`stop_process`.** Arguments `pid` and optionally `force`, a boolean. It sends
  `SIGTERM`, or `SIGKILL` with `force`, only to a process of the daemon's user that is
  neither the daemon nor an engine it started and whose environment holds the line's
  mark, read again at the moment of stopping. Every other pid is refused, as are a pid
  of 1 or less, a process that has already ended and one that cannot be signalled.
- **Process workers.** Process inspections and launch-record sweeps use separate worker
  threads, each with at most eight queued calls behind its active call. Sweeps wait for
  signalled processes, so isolating them keeps a long sweep from occupying the inspection
  queue. When an inspection queue is full, the request is refused. A queued call whose
  caller is cancelled is skipped if it has not started. A call already running cannot be
  stopped, so a cancelled stop request may still take effect. Both queues are bounded,
  and sweeps report a repeated queue refusal once until a sweep succeeds.
- **Detached-process inspection.** At the end of a turn, the scan runs on the process
  worker because its process-table reads can block. The worker receives only the process
  table and scan inputs, then returns process identifiers and command strings. Its result
  holds no scheduler, engine or driver reference. When an engine retires, `Driver.close`
  releases its Keleusma virtual machine on the thread that created it, so collection
  during the scan cannot finalize that driver through the result. A virtual machine not
  closed before it becomes cyclic garbage may still be finalized on any thread. On macOS
  the scan may make two `ps` calls, each with the 10-second process-table timeout, so a
  slow table can delay other requests for up to 20 seconds.
- **Failing closed.** A process whose environment cannot be read is not the line's.
  Reading another process's environment is specific to each operating system, through
  `ps -E` on macOS and `/proc/PID/environ` on Linux, a porting hazard that
  `dromon.processes` keeps in one function. On macOS the environment arrives on one line
  after the command's arguments, so an argument written exactly as the assignment also
  counts, which a line can only arrange for a process it started itself.
- **Self-test.** When `dromond` starts, it starts a short child with a known variable and
  checks that both readers, the listing's and the stop's, see it within about a second,
  then stops the child. Where they do not, `list_processes` and `stop_process` refuse
  with a fixed explanation that the platform does not let the daemon identify a line's
  processes, rather than returning an empty listing, and `line_status` reports
  `processes` as not identifiable with the same note.
- **Fact, observed live on 2026-10-26 on macOS, Darwin 27.** Neither `ps -wwE` nor
  `sysctl` with `KERN_PROCARGS2` returned any environment for another process, even an
  ordinary one of the same user, and a line's `nohup` process was reparented to
  `launchd` and led its own process group. So `list_processes` returned an empty
  listing and `stop_process` refused on a Claude Code line and on a Codex line. On such
  a platform the mark cannot identify a line's processes, and the self-test above
  makes the tools say so. The Linux path through `/proc` is unaffected. A Claude Code
  line may instead keep its engine connected between turns, so that the engine's own
  background task tools work, as the kept sessions section below specifies.
- **Limits.** A pid reused by another process between the check and the signal is the
  race every signal by pid has, narrowed by checking immediately before. External
  agents have neither tool.

## The wake prompt

Each turn's prompt opens with one line giving the line's autonomous turns in a row
without operator mail, of those allowed, and its estimated cost since its last release
of its limit, so the line sees how close a hold is without calling `line_status`. The
counts are those before the turn, which is counted when it ends, so the line also says
whether the turn is autonomous and what the count becomes, or that an operator's mail
started it and the count starts again from 0. A turn whose only purpose is compaction does not increase
the autonomous turn count, though its reported or unknown cost still applies.
**Found in round 4.** A reviewer read 0 in the prompt of a
turn a task-end note started and took it for a missed count. The turn was counted
when it ended, which the worker line observed, so this was a gap in what the prompt
said, not in the count. The mail summary and the
headers of the newly delivered mail follow, and bodies stay in the mailbox.

The prompt names the operator who most recently messaged the line at the start of the
turn. The omitted `to` recipient can change during the turn if another operator
messages the line. At the time of a call, `ask_operator` without `to` uses the operator
who most recently messaged the line. When no operator has messaged the line at the
start of a turn, the prompt says that `to` is required unless an operator messages it
during that turn.

- **Repeats.** A message whose sender, recipient and body match a message sent at most
  15 minutes before it is still delivered, and its header ends with
  `a repeat of` and the earlier message's identifier. The daemon remembers messages to
  lines by sender, recipient and a digest of the body, and forgets each once it is more
  than 15 minutes old, so the memory stays bounded and the check takes constant time.
  The memory does not survive a restart, so a message sent before one is never marked.
- **Scheduled wakes.** A fired wake's header says it is the line's scheduled wake and
  gives the time it was due and the time it is delivered. The due time is kept in the
  state the event log builds, so it survives a restart. A delivery time is shown
  beside it in the line's delivered mail and in `last_fired_wake` while the daemon has
  that delivery in memory.
- **`list_mail`.** **Decided by the operator on 2026-10-26** in round 2. A listed entry
  ends with `repeat_of` and the earlier message's identifier when its message repeated
  one, and a fired wake's entry ends with `scheduled wake due` and its due time, then
  `delivered` and the time it was delivered. The repeat marks and delivery times live
  in the daemon's memory, at most 4,096 of each with the oldest forgotten first, so
  after a restart a wake's entry gives its due time only and no entry is a repeat.
- **`read_mail`.** A line may open a message it received or one it sent. Opening
  received mail records delivery and reading as before. Opening its own sent mail
  returns the body without changing delivery or read state, which belongs to the
  recipient. A line cannot open mail between other parties.
- **Unanswered.** The summary's unanswered count leaves out a reply to a question Dromon
  asked on the line's behalf when it held the line, such as an operator's `resume`,
  and, from round 4, a note from the line to itself, a fired wake or a task-end note,
  and, from 2026-10-28, a note from the harness, which nothing can reply to, because
  none asks a reply of the line. The state the event log builds keeps
  every such question's identifier from its `line_held` event, so the rule survives a
  restart. **Decided by the operator on 2026-10-26** in round 2.
- **Own notes.** **Required by the operator on 2026-10-30, the rule decided by core
  for review.** A note from the line to itself, such as a fired wake, a task-end note
  or a compaction request, is never counted in `unread`, and is counted in
  `own_notes` instead, so nothing is hidden. One rule, `dromon.mailbox.summary`,
  gives the counts to every view, the wake prompt's sentence, `line_status`, the
  first line of `list_mail`, and the client's `mail` and line entries, so all give
  the same figures for one mailbox. Mail from an operator, another line, an agent, a
  remote party or the harness is always counted in `unread`. A prompt still
  announces an own note it delivers, with its header as for every message.
  **Review r6, finding 1.** `list_mail` with `unread_only` lists exactly what `unread`
  counts, so it leaves out own notes, and with `own_notes_only` it lists exactly
  what `own_notes` counts. The two select different messages, so giving both is
  refused. The client's `mail` request has no `own_notes_only`, and its `unread_only`
  follows the same rule. **Review r6, finding 6.** An own note leaves `own_notes`
  once the line opens it, acknowledges it or replies to it. Lines already acknowledge
  fired wakes without opening them, so the count stays near the notes still waiting
  rather than growing with every wake. From 2026-10-28 until
  this rule, only the wake prompt left such notes out, and only those delivered to an
  earlier turn, so `line_status` counted them and the two disagreed. **Decided by core
  for review.** A prompt that delivers an own note does not mark it read, since the
  prompt shows its header and never its body, and marking it would change no count.
  **Porting note.** The rule is a pure function of the state and adds no event, so a
  port carries it unchanged. The sentence's wording changed, and so did the
  `MailSummary` field, from `collapsed` to `own_notes`.
- **Own notes shown to a turn.** **Required by the operator on 2026-10-30 in round 8,
  the rule decided by core for review.** An own note also leaves `own_notes` once a
  turn of the line has ended after the note was delivered, whatever the turn's
  outcome, so long as its engine reported the turn. A worker line in another project counted 62 own notes, and 89 of its 90
  task notes were never read or acknowledged, so the count grew without bound and
  told the line nothing. The rule changes the one function above, so every view
  keeps giving one figure, and mail from any other sender is unchanged.
  - **What shown means.** **Decided by the operator in the round 8 review.** Shown
    means delivered to the line, whether in its prompt, inside a running turn or for
    the record between turns. It does not mean that a prompt carried the note. A
    note that leaves `own_notes` this way is in no count, and `list_mail` without a
    filter still lists it, which the prompt's sentence and the counts line of
    `list_mail` both say.
  - **From the event log alone.** The state counts each line's `line_turn_ended`
    events that are not `unreported`, and each message delivered to a line keeps the
    count as it stood at its `message_delivered`. A larger count now means a turn
    with a report ended after the delivery. The daemon records `line_turn_ended`
    however the turn ended, a failed or held turn included, so the rule holds across
    a restart, and nothing of it is kept in the scheduler's memory.
  - **A turn with no report.** **Required by the operator in the round 8 review,
    ON1.** When the line's engine returns no report for a turn, the daemon cannot say
    that its prompt reached the engine. The prompt may not have been composed, a
    check before the engine may have failed, the engine may have raised, or the turn
    may have been cancelled. The daemon then records `line_turn_ended` with
    `unreported`, in an ordinary turn and in a turn the engine started on a task's
    end alike, and the turn retires no note. Its notes count until a later turn of
    the line ends with a report. A failed or cancelled engine may have read the
    prompt, so the rule can keep a note counted one turn too long, and never retires
    one unseen. An event written before the field counts as reported.
  - **The current prompt.** A note the current prompt delivered still counts, since
    its turn has not ended, so the turn shown it sees it counted.
  - **A task note recorded during a running turn.** No prompt delivers it. It counts
    while that turn runs and leaves when it ends. **Decided by core for review**,
    since the line's engine reported the task's end inside that turn, and since a
    rule without an exception for it needs no record of which deliveries a prompt
    made. Never counting it would hide it from that turn's own `line_status`.
  - **Delivery between turns without a wake.** The end of a task the line stopped is
    delivered for the record only, and so is a context note withdrawn after its
    compaction, when the compaction is reported outside a turn of Dromon's. Each
    counts until the next turn of the line ends, so that turn sees it counted.
    **Decided by core for review**, over counting it until dealt with, which would
    bring the unbounded count back for notes that ask nothing. Delivered inside a
    turn, either leaves when that turn ends, as a task note does.
  - **A turn cut short by a restart** records no turn end, so its notes count until
    the next turn of the line ends after the restart.
  - **Prompts mark nothing read.** The round 6 decision stands. Leaving the count no
    longer needs `read`, so marking a prompt's notes read would change no figure and
    would only add events to the log.
  - **Mail to other parties.** Mail to an operator, an agent or a peer keeps no turn
    count, so no line's turn retires it. An operator cannot send itself a note, so
    its `own_notes` is 0.
  - **Porting note.** The rule depends on applying events in log order, which a port
    keeps. The sentence in the wake prompt and the counts line of `list_mail` changed
    wording, the state gained `reported_turn_ends` and the message field
    `reported_ends_at_delivery`, which no event carries, and `line_turn_ended`
    gained the optional `unreported`, which a port omits when null.
- **A moved worktree.** **Requested by the operator on 2026-10-28.** When the commit
  checked out in the line's working directory differs from the one it held when the
  line's last turn ended, as after an operator commits or merges the line's work, the
  prompt opens by naming both commits and the `git log` range that lists the commits
  between. **Decided by core for review.** The daemon reads `HEAD` from a few small
  files and never runs git or reads its objects, so it names the two ends and not
  each commit. A move during a turn, such as the line's own commit, is not told. The
  turn-end commit is recorded in the event log, so the notice survives a daemon restart.
  **Task of 2026-10-29.** Where no turn-end commit is recorded for the line, as when its
  last turn ended under a daemon that did not record one or with its worktree
  unreadable, the check is skipped and the prompt says so, with the commit checked out
  now. The turn's end records the commit, so this is said once. A worktree whose commit
  cannot be read now is told nothing, since nothing could be compared.

**Leases.** **Decided by the operator on 2026-10-26.** A project declares named leases
in its `[leases]` table, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, so its
lines can take turns at a shared resource, such as a machine that runs one full gate at
a time.

- **`acquire_lease`.** Argument `name`. It succeeds when no line holds the lease, and
  answers with the lease's end, the wall clock in Coordinated Universal Time plus the
  project's time to live, rounded up to the whole second. It is refused while any line
  holds it, naming the holder and when that lease ends, including the line itself, so
  one acquisition lasts at most one time to live. The holder may acquire it again once
  it has expired. Expiry follows the daemon's wall clock, so a change of that clock
  moves it. A name the project does not declare is refused, listing the declared
  names.
- **`release_lease`.** Argument `name`. It ends the line's lease, and is refused for a
  lease the line does not hold.
- **Ends.** A lease ends when released, when its time to live passes, when its holder's
  engine session is cleared, whether by `clear_session`, by a change of its `workdir`
  or at the start of each turn under `fresh_session_each_turn`, when its holder is
  rebound, and when a reload or a start stops declaring its name.
- **A change of owner.** **Decided on 2026-10-31.** When a role passes from one seat to
  another, every lease the previous owner holds passes to the new owner with the same
  acquisition and expiry, as [`ROLES.md`](../architecture/ROLES.md) explains. A role
  left vacant leaves its former owner's leases with it.
- **Durable.** **Task of 2026-10-31.** A lease is an event, `lease_acquired`, and ends
  with `lease_released` or with the events [`EVENT_LOG.md`](EVENT_LOG.md) lists, so a
  restart keeps its holder, its acquisition and its expiry, and expiry stays its bound.
  A resource a stopped daemon's lines were using therefore stays guarded until the
  lease expires, which suits a long build or a device run that outlives the daemon, as
  a supervised run does. Before this date leases lived in memory and a restart dropped
  every one.
- **Visibility.** `status` and `line_view` show the leases each line holds.
- **Agents.** External agents have no lease tools.

**`wake_after`.** **Decided by the operator on 2026-10-26.** A line that starts a long
command in the background, which survives the end of its turn, needs a way to be woken
to read the result.

- **Arguments.** `seconds`, a whole number from 60 to 86,400, and `note`, text of at
  most 2,000 bytes. Optional `task_id` names one of the line's currently running
  background tasks. Naming a task that is not running is refused. A number with a
  fraction, a boolean or text is refused.
- **Effect.** The daemon appends `wake_scheduled` with the due time, the wall clock in
  Coordinated Universal Time plus `seconds`, rounded up to the whole second so a wake
  never fires early. A line has at most one pending scheduled wake, and a new call
  replaces it, which the tool's answer reports with the replaced due time.
- **Task-bound fallback.** When the named task's end is recorded as a note for the line,
  applying its `task_noted` event atomically withdraws the wake if it is still scheduled.
  This is a wait operation. The line ends its turn after scheduling it, then the task
  end wakes the line with a note that names the cancelled wake. This note is emitted
  even when the ordinary task-note limit has been reached. If the task outlives the
  timeout, the wake fires instead. If the wake has fired already, its message cannot
  be withdrawn, and it has already provided the fallback turn. An unbound wake behaves
  as before.
- **For review.** The wait uses a scheduled wake and ends the turn. It does not block
  inside a tool call, because a blocked turn would hold the line open while preventing
  it from receiving mail and other wake notices.
- **Firing.** At the first tick at or after the due time, the note is recorded as a
  message from the line to itself, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies under
  scheduled wakes. It wakes the line through the ordinary path in a later tick and
  counts toward the autonomous-turn limit, except when it is solely a compaction
  wake. It waits like other mail while the line is held. A wake that fell due while the
  daemon was down fires at its first tick. A note that the disclosure list in force at
  firing covers is replaced by a fixed notice, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies, so a list changed by a reload or a restart still applies.
- **Rest.** While nothing else is in flight, the daemon's loop rests until the next
  wake falls due, for at most 60 seconds at a time, so a change of the wall clock
  delays a wake by at most that much.
- **Visibility.** `status` and `line_view` show the pending wake's due time and the
  identifier its note will carry, as [`CLIENT.md`](CLIENT.md) specifies.

**Disclosure guard.** When the project names a disclosure list, a refused tool call
names the field that contains a listed term, such as `body` or `note`, without naming
the term or its position. If several fields contain a listed term, all such field names
are reported. The refusal records nothing and counts against the same hourly limit as
`check_disclosure`, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies. A wake note
is checked again when it fires.

**Request keys.** **Row 11 of the T3 Code review, 2026-10-27.** `send_message`,
`ask_operator` and `wake_after` each take an optional `request_key`, text of 1 to 128
printable characters chosen by the caller, so a retried call does not act twice.

- **A repeat.** A call whose key the same party used before on the same tool, with the
  same other arguments in any order, records nothing and answers with the first call's
  answer, followed by a sentence saying it was a repeat. A repeated `wake_after` does
  not move the pending wake.
- **A conflict.** The same key with other arguments is refused, since Dromon cannot tell
  whether the caller meant the first request or a new one.
- **A refusal.** Only a call that acted is remembered, so a refused call may be retried
  with its key and acts once its cause has gone.
- **Scope.** Keys belong to one party and one tool, so two lines, or one line's two
  tools, may use the same key.
- **Bounds.** At most 256 keys are kept for each party and tool, the oldest forgotten
  first, after a replay as before it. **Porting hazard.** The check and the append run
  with no suspension point between, which a single asyncio loop makes atomic. A port
  that serves tools from several threads holds a lock across the two.
- **Durable.** **Task of 2026-10-29, round 5.** A keyed call records its key, with the
  SHA-256 digest of its other arguments, as the `request` field of the event of its
  act, a `message_sent` or a `wake_scheduled`, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies. One append records both, so no crash leaves the act without its key, and
  a retry that crosses a daemon restart repeats the first answer. Keys were once kept
  in the daemon's memory only, and a retry across a restart acted again.
- **The repeated answer.** It is rebuilt from the event of the act and the state, so
  it does not depend on what the daemon held in memory. For `send_message` and
  `ask_operator` it is the first answer's text. **Decided by core for review.** A
  repeated `wake_after` leaves out the sentence naming the wake the first call
  replaced, since `wake_scheduled` does not record it. A repeated priority note says
  `with priority` only when its `message_prioritized` was recorded, which a crash
  between the two appends may have prevented.
- **Agents.** An external agent's `send_message` is the same tool, and takes a key
  in the same way.

**Mail during a turn.** A turn announces the mail that woke it, and marks it delivered,
as it starts. Mail that arrives while the turn runs waits, undelivered, and the next
turn announces it, in the order sent, once each. Only the line settles mail, by reading,
acknowledging or answering it. A turn that fails leaves its mail unread and
unacknowledged, and the unread count of later prompts includes it. Mail the line opens
during its turn is not announced again. A turn cut short by a daemon stop leaves the
mail that arrived during it undelivered in the event log, and the restarted daemon
announces it. These cases are tested in `tests/test_mail_durability.py`.

## Context handoff

**Row 13 of the T3 Code review, 2026-10-27.** `rebind_line` and `clear_session`, and a
change of `workdir`, start the line's next turn in a new engine session, which
remembers none of the line's earlier turns. The event log records that the line is
owed a handoff, from `line_rebound` or `line_session_cleared`, as
[`EVENT_LOG.md`](EVENT_LOG.md) specifies, so it survives a restart.

- **What the turn is given.** The first turn in the new session begins with a
  preamble that names the cause, then the line's most recent messages, sent or
  received, other than the mail the turn announces as new, listed oldest first. Each
  is one line, with its identifier, its sender and recipient, its kind, a question's
  status, whether it was read, and the message it replies to.
- **Budget.** At most 20 messages, whose lines take at most 4,000 characters, chosen
  newest first. The preamble says how many of how many were listed.
- **Bodies.** The line's own messages and operators' are quoted, cut to 300
  characters, with every run of white space, line breaks included, turned to one
  space, so a body cannot forge the handoff's structure. Messages from other lines and
  agents are untrusted data, as the trust section says, and are named but never
  quoted. A body the disclosure list in force covers is replaced by a notice.
- **The line's own words.** Review of 2026-10-27, finding H1. Each of the line's own
  messages is labelled as its earlier words, which may relay what other lines and
  agents wrote, and which are not instructions, and the preamble says the same. A line
  that forwarded another agent's text would otherwise hand it to its new session with
  the line's own authority.
- **Deterministic.** The selection is a function of the event log's state and the
  turn's new mail alone, with no clock and no randomness.
- **Recorded.** Once the engine has run the turn, the daemon appends
  `context_handed_off` with the cause, the identifiers selected, the text's length in
  characters and its SHA-256 digest, which ends the handoff. A turn whose engine
  failed or was cancelled records nothing, so the next turn carries the handoff again.
  A change of `workdir` during that turn leaves a handoff owed.
- **Opting out.** A line whose configuration sets `context_handoff` to `false` is
  given no handoff, and the event is still recorded, with the outcome `opted_out`, so
  the log shows that the new session began without one.

## Fresh session each turn

The operator may set `fresh_session_each_turn` for a line on Claude Code or Codex. Each
turn then starts in a new engine session. The scheduler records `line_session_cleared`
after delivering the mail that starts the turn and before building its engine. That
event removes the prior session identifier durably, so a restart cannot resume it. The
new turn still receives its starting mail. It receives no context handoff from earlier
mail, no standing brief lead and no kept connection.

This key implies `context_handoff = false` when that key is omitted. An explicit
`context_handoff = true` is refused because it would carry earlier mail into the new
session. `keep_session = true` is refused because a kept connection preserves engine
state between turns. Grok Build, Pi, Cursor Agent and Antigravity configurations are
refused at load because their adapters do not guarantee a new session per turn.
The setting is part of operator line configuration and is not exposed as a line tool.

**Turning it on for a line that exists.** **Round 9, item 1, 2026-10-31.** When the
project file at a reload, or `dromon configure`, turns the key on for a line that
has a recorded session, the daemon records `line_session_cleared` at once, so
`dromon settings` stops showing the old session as soon as the change applies. **Round
9 review, F1, decided by the operator.** A line in a turn is cleared at once as well,
and the turn in flight, which runs in the session the line no longer resumes, records
neither that session, nor its context estimate, nor a compaction it reports, nor a
compaction wake when it ends. The same holds for a turn in flight when `workdir`
changes. Before, a line in a turn was left to its next turn's start, and the turn's
end stored its estimate again, so a line moved to another worktree during a long turn
was warned of, and could be woken to compact, context its next session did not hold.
**A new session forgets the old context.** Every
`line_session_cleared` and `line_rebound` also forgets the daemon's context estimate of
the old session, with the automatic compaction and the context-warning wake it would
start. The estimate outlived a cleared session before, so the first turn after the
reload warned of the old session's context, could begin with a compaction, and the
line took the warning to mean that the old session had resumed. **Inference, not
observed live.** That warning is what the report of the old session resuming
described, since the scheduler built the engine with no session to resume on that
turn, as a test with a scripted engine shows. The estimate returns with the new
session's first report.

## Standing brief

**Task of 2026-10-29**, from the round 3 reports, three of which asked that a line's
current task survive an engine reset or a handoff. A context handoff carries the
line's mail, from which a line had to reconstruct its intent. A standing brief carries
the intent itself, in the line's own words, set with `set_brief` and recorded as
`brief_set`, so it survives a replay and a restart.

- **When it leads.** A turn whose engine session is new leads with the brief, before
  any other notice. The session is new when the daemon holds no engine for the line,
  which is so after a restart, a rebind, a cleared session, a change of configuration
  and a change of the role's owner, when the turn asks for a compaction, and when the
  engine compacted the session by itself since the last turn. A line with
  `fresh_session_each_turn` suppresses this lead. **Inference.** An engine
  rebuilt with the line's recorded session may resume that session with its context,
  in which case the brief is repeated, which costs only its length. A compaction the
  engine makes by itself during a turn is told at the next turn, one turn late.
- **What it says.** The lead gives the time the brief was set and the worktree's
  commit then, says that it is the line's own earlier text and may be stale, and
  delimits the brief between `BEGIN STANDING BRIEF DATA` and `END STANDING BRIEF DATA`
  as JSON.
- **Mail since the brief.** **Required by the operator on 2026-10-30.** The review
  line's brief was stale after a compaction, since its task had changed in mail since
  the brief was set. When an operator has mailed the line since its latest
  `brief_set`, the lead adds one sentence that says so, naming the time the brief was
  set and the time of the newest such mail, and that the task may have changed in
  that mail. The brief itself is unchanged. Which mail is newer comes from the order
  of events, which the state keeps as `mail_since_brief`, so replay agrees. The time
  comes from the daemon's own record of the mail it sent, or after a restart from
  that message's `recorded_at` in the event log, and where neither gives one the
  sentence says so. Mail from other lines, agents, peers and the harness does not
  count. **Porting note.** `recorded_at` is still never read by the model, and the
  scheduler reads it from the log at most once for each such message after a
  restart, keeping a time the log does not give as well, from review r6 finding 7.
  **Review r6, finding 8.** `line_status` gives the same fact under its `brief` as
  `operator_mail_since`, with `message_id` and `sent_at`, which is `null` where the
  log gives no time, or `null` when no operator mail came since the brief, in both
  its compact and detailed views, so a turn that does not lead with the brief can
  see it.
- **Where else it is shown.** `line_status` gives the whole brief in its detailed view
  and its task and next action, cut to 400 bytes, in its compact one, each under
  `untrusted_data` with a note that the line wrote it. The role transition package
  takes from the former owner's brief what its journal entry lacks. Where both are
  silent it fills Task from the former owner's latest operator mail and Gate and
  coverage from its latest task record, each marked observed by the daemon, as
  [`ROLES.md`](../architecture/ROLES.md) describes.
- **Trust.** The brief is untrusted data, since a line may relay in it what other lines
  and agents wrote. The daemon stamps it and checks its bounds and the disclosure list,
  and checks nothing else. A brief that holds a term the disclosure list in force
  covers, which may have changed since the brief was set, is withheld whole from every
  view.
- **Porting note.** Which turn is on a new session is read from whether the daemon
  holds an engine for the line, which is in memory. A port keeps that test, or records
  the session's start, so that the first turn after its own restart also leads with the
  brief.

## Role ownership notices

After an `ownership_changed` event, the new owner receives the role transition package
at the start of its next turn. A former owner receives a notice at the start of its
next turn when another party made the change. Neither notice is sent by mail.

Each seat receives the notice once for that change. A turn counts only if its
`line_state_changed` event to `running` follows the `ownership_changed` event and its
`line_turn_ended` follows that start. Replay rebuilds the turn order, so a turn that
was already running at the ownership change does not suppress the notice after a
restart. If no post-change turn ended, the next turn still receives the notice,
including after a restart. A later ownership change starts a new notice for each
affected seat. The role transition package and former-owner notice are described in
[`ROLES.md`](../architecture/ROLES.md).

## Context and compaction

**Decided by the operator on 2026-10-26** in round 5, from the worker line's round 4
report, which found that about 2,000,000 cached tokens re-read in one turn set its cost.

- **Per-call usage.** **Fact, read from Agent SDK 0.2.174.** Each assistant message
  carries its engine call's `usage` and the call's message identifier. One response
  can arrive as several messages with one identifier, so each call is counted once.
  Each of `recent_turns` lists its `calls`, the latest 50, each with its fresh, cached
  and written input tokens and output tokens and whether a subagent made it, and a
  `calls_note` saying that the engine reports cost per turn and model only, not per
  call, or that the engine reports no per-call usage. Codex lines list their calls
  from round 6, as below.
- **The context estimate.** `line_status` gives `context`, with `estimated_tokens`, the
  last main-loop call's fresh, cached and cache-written input and its output, which the
  next call re-reads, `warn_at`, whether the line has `compact_session`, whether a
  compaction is pending, and the `last_compaction`, with its `trigger`, `manual` or
  `auto`, its `before_tokens` and `after_tokens`, and the time `at` which Dromon
  recorded it. Its `cause` is `compact_session` or `auto_compact_tokens` for a
  compaction Dromon asked for, which Claude Code reports as `manual`, or `engine` for
  one the engine made by itself. The live probe of round 6 showed an automatic
  compaction reported as `manual`, which is why the cause is given apart from the
  trigger. The `trigger` is only what the engine reported, with a `trigger_note`
  saying what it means. Codex reports none, so a Codex compaction has a `null`
  trigger. Before the round 7 report of luna, Dromon filled in `manual` for Codex,
  which read as contradicting an automatic cause. While the estimate is unknown after a compaction whose size was not
  reported, its `note` says so. All are kept in memory. **Fixed after the live probe of 2026-10-26.** A
  compaction, asked for or automatic, is recorded the moment the engine reports it,
  so a status read later in the same turn shows it, and the estimate then becomes the
  size it left, or unknown when the engine did not report that size. At the turn's end
  the estimate comes from the last main-loop call after the compaction, or the size it
  left when no call followed. Before the fix both were updated only at the turn's end,
  and a status read in the compacted turn showed no compaction and the old size.
- **The warning.** When the estimate after a turn reaches `context_warn_tokens`, the
  next prompt's first line says the context's size, that every engine call re-reads
  it, and that `compact_session` compacts it, or that an operator can enable it. A
  line with `allow_compact` is also sent a self-note that wakes it to compact before
  its next real turn, even when no mail was waiting. Repeated estimates coalesce onto
  one wake. If a later estimate falls below the effective compaction threshold before
  that wake fires, the daemon withdraws it. This threshold is the warning unless
  `auto_compact_tokens` sets an earlier one. A turn with only this compaction wake
  does not spend an autonomous turn. A hibernated line receives no wake until it is
  awakened. **Task of 2026-10-30.** One warning gives one wake. None is queued while
  the line's turn runs or while a compaction decided for it is under way, and the
  pass after the turn ends decides again from the turn's final estimate. Any
  reported compaction withdraws a pending wake, whether or not it reported a size,
  and the automatic hold then keeps a new one from being queued while the context
  stays past the threshold. A wake that falls due under that hold is withdrawn, as
  one below the threshold is. The engines line's log showed a second wake queued one
  second into the first wake's turn, which fired after it. The note says why it was
  scheduled, naming the estimated tokens, the warning and where the estimate came
  from, in fixed text, and says that a restart before it fires loses the request,
  since the estimate is held in memory. **Review of that task.** While another
  wake is pending, such as a wait for a background task, or while mail waits for
  the line, no wake is queued and the line is not marked as woken for one. The mail
  starts a turn that compacts on the threshold, and once the other wake is gone
  without a turn, the next pass queues the wake. A compaction reported after the
  wake fired and before its note was delivered records the note as delivered
  without a turn. After a restart, a pending wake whose note is the context note
  and which names no task is taken as the warning's, so a later compaction
  withdraws it. The log does not record who scheduled a wake, so a line's own wake
  with an imitating note is taken too. That affects only the line's own wake, and
  gives it at most the one uncounted compaction turn that `compact_session` at the
  warning also gives. The
  default of 150,000 leaves room before an engine's automatic compaction near a
  200,000 token window, and past it a turn of a few calls re-reads a million tokens.
- **`compact_session`.** No arguments before round 6. Only a Claude Code line, and
  from round 6 a Codex line, whose configuration sets `allow_compact` has it, and it is refused if the setting is withdrawn. It marks the
  line's own next turn to begin with a compaction. **Fact, read from the bundled Claude
  Code 2.1.310.** `compact` is a built-in command that runs outside an interactive
  session unless `DISABLE_COMPACT` is set, and the engine reports it with a
  `compact_boundary` system message whose `compact_metadata` gives `trigger`,
  `manual` for a typed command, `pre_tokens` and, optionally, `post_tokens`. So the
  turn first sends `/compact` as a prompt, through the turn's own checks, then its
  prompt, which opens with a note giving the context before and after, or saying that
  either was not reported, or that the engine reported no compaction. Both results'
  costs count toward the turn. A turn refused during the compaction sends no prompt.
  **From the round 5 review.** A pending request is dropped when the line's
  configuration changes, and `allow_compact` is checked again when the turn consumes
  it. No `/compact` is sent once the turn's init has been seen and refused. The engine
  reports its init only with its reply to a prompt, so normally the compaction's own
  reply carries it, and the init check then refuses and interrupts the compaction as
  it would an ordinary prompt, which is no weaker than a turn without a compaction.
- **Round 6, decided by the operator on 2026-10-26** from the round 5 reports.
  - **Its own wake.** `compact_session` takes an optional `seconds`, from 60 to
    86,400, by default 60, and schedules a wake through the `wake_after` machinery,
    so the compaction does not wait for unrelated mail. A wake already pending stays,
    and none is added. A bad `seconds` is refused with nothing asked or scheduled. In
    round 6 a compaction an earlier turn consumed left the wake to fire, which cost one
    short turn. Round 7 withdraws it, as below. **Review, F4.** The
    request is held in memory, so a daemon restart loses it while the wake survives.
    The wake's note says so, since the wake's turn cannot tell a lost request from a
    consumed one, and a line without a compaction note may ask again.
  - **The warning after the compaction.** A turn that begins with a compaction leaves
    the context warning out of its first line. The compaction's note ends with the
    warning as it stands once the compaction is recorded, so a compacted line is not
    told to compact again, and a compaction that leaves a large context still warns.
    An engine that cannot compact gets the warning at the head of its prompt instead.
  - **Automatic compaction.** A compactable line is woken when its estimate reaches
    `context_warn_tokens`. `auto_compact_tokens`, when set, can move the threshold
    earlier. A turn whose estimate is at or past the effective threshold begins with
    a compaction, whose note says that it was automatic and why. A request the line made takes
    precedence. No automatic compaction follows one that left the context at or past
    the threshold, or whose size after the engine did not report, as on Codex, until
    a context below the threshold is reported, because compacting again would not
    bring it below and would cost every turn. The warning shows meanwhile, and
    `line_status` gives `auto_compact_held`. **Review, F2 and F3.** A request belongs
    to the turn it was made for, which takes it as it starts, so a turn refused before
    its compaction point leaves nothing for a later turn. A Codex compaction in which
    no call was seen reports its usage as unknown, not as zeros.
  - **Its own cost.** Each of `recent_turns` has a `kind`, `turn` for a Dromon turn,
    and `compaction`, the compaction it began with, with its trigger, sizes, its own
    `cost_usd` and `usage`, which the turn's own include, or `null`. A Claude Code
    result that ends a turn outside Dromon's own is measured when it arrives, so the
    cost mark moves past it, and becomes an entry of its own, of kind `self_started`
    for a turn the engine began by itself, `interrupted` for a turn an operator
    interrupted, or `handshake` for a result with no turn behind it, between turns or
    as a resumed session's handoff, as [Kept sessions](#kept-sessions) says. Its cost
    counts toward the budget, and a line past it is held, but it
    is not an autonomous turn. Before round 6 such a result was discarded, which lost
    its cost where the engine reports per-turn usage.
  - **Codex lines.** **Facts, observed live on Codex 0.164.0 on 2026-10-26.** The
    app-server notifies `thread/tokenUsage/updated` with `threadId`, `turnId` and
    `tokenUsage`, which has `total`, `last` and `modelContextWindow`, each breakdown
    giving `totalTokens`, `inputTokens`, `cachedInputTokens`, `cacheWriteInputTokens`,
    `outputTokens` and `reasoningOutputTokens`. `totalTokens` was `inputTokens` plus
    `outputTokens`, so the input includes the cached part. `thread/compact/start`
    answers at once, and the engine then starts a turn of its own, in which a
    `contextCompaction` item starts and completes before the turn completes. The
    `thread/compacted` notice, whose name the binary holds, as read with `strings`
    and not by running it, was never sent, so the item's
    completion is read as the compaction, and the notice is still accepted. The
    compaction left the running totals unchanged and gave a usage notice whose `last`
    held a total alone, larger than the context before it, with every other count
    zero. That notice is not read as an engine call. No cost in money is reported
    anywhere. **Inference.** The context is `last.totalTokens` of a real call, as the
    engine's own gauge uses it. So a Codex line lists each call's tokens, estimates its
    context, reports the turn's tokens as the difference of the running totals, and
    may have `compact_session` and `auto_compact_tokens`. Its compaction is sent after
    every check of the turn, with its messages read as a turn of their own, whose tool
    use refuses the line's turn, and is waited for until that turn completes, at most
    600 seconds. The size before is Dromon's last estimate. The size after is not
    reported, and the estimate is unknown until the line's next call reports one. Its
    cost stays unknown.
- **Round 7, decided by the operator on 2026-10-26** from the round 6 reports.
  - **The compaction's wake is withdrawn.** When the engine reports any compaction,
    whether the engine made it itself or a turn made it at `compact_session`'s request,
    the daemon appends `wake_withdrawn` for the pending wake that call scheduled, if
    it is still pending, and `last_compaction.withdrawn_wake` names
    it, or is `null`. A wake the line scheduled itself, before or after, is never
    withdrawn, since only the identifier of the wake `compact_session` scheduled is
    matched. The wake is withdrawn on the engine's report and not when a turn takes the
    request, so a turn that fails or is refused before compacting leaves the wake to
    fire, and its note says that without a compaction note the line may ask again. A
    wake that falls due while the turn that took its request is still compacting is
    held back until that turn reports the compaction, which withdraws it, or ends,
    which lets it fire on the next tick. The hold is bounded by the turn.
    **Review, F1 to F4.** A withdrawn identifier is kept in the state, and a later
    `wake_scheduled`, `message_sent` or `task_noted` that takes it is refused, so a
    reply reference to it cannot reach another message. The compaction is recorded
    before the wake is withdrawn, and a withdrawal the log refuses is reported and
    leaves the wake to fire, so `last_compaction` always agrees with the turn. The
    hold begins when the turn is dispatched with its compaction decided, not when the
    turn's task starts, so no tick in between can fire the wake. A test confirms that
    a held wake fires on the first tick after its turn ends without compacting. **The restart case.** The
    identifier of the compaction's wake is in memory, as the request is, so after a
    restart the wake is not withdrawn and fires, and its note says that a restart
    loses the request. **Rejected alternative.** Skipping the wake at delivery was
    rejected, because a fired note is already a `message_sent` in the log, and dropping
    it would need an event of its own anyway and would leave a message sent and never
    delivered. **Porting cost.** One event kind with two identifiers, its check that
    the line's pending wake has that identifier, and one in-memory map per line.
  - **Zero or nothing.** A `self_started` or `interrupted` entry's `cost_note` says
    that the engine reported no cost, so none was counted, or that it reported a cost
    of zero, not nothing, with the likely reason, a turn that ended before any engine
    call, marked as an inference. Its `usage_note` says the same of its tokens. Both
    are `null` when the engine reported a nonzero amount.
  - **The estimate's source.** The context `note` names where the estimate came from:
    the last main-loop call, the size the last compaction left as the engine reported
    it, or the estimate below. The compaction's size stays the source until the turn's
    end replaces it with its last call's context.
  - **Codex's size after.** **Fact, observed live on Codex 0.164.0.** The engine does
    not report a compaction's size after, so `after_tokens` stays `null`. The first
    engine call after the compaction in the same turn gives
    `last_compaction.after_estimate_tokens`, the call's `inputTokens`, or its
    `totalTokens` where no input is given, with `after_estimate_note` saying that it
    is an upper bound because it includes the turn's prompt. The estimate is reported
    at once, becomes the context estimate, and holds automatic compaction when it is at
    or past `auto_compact_tokens` and releases it when below, as a reported size does.
    It is also given in that turn's `recent_turns` compaction entry. An estimate is
    ignored when the last compaction's size was reported, when one was already
    recorded for it, or when it is not a positive whole number. A Claude Code
    compaction gives none, and both fields stay `null`.

## Kept sessions

**Decided by the operator on 2026-10-26** in round 2, from the round 1 reports of three
lines. A Claude Code line with `keep_session` keeps its engine connected between turns,
so that a command it starts with Bash's `run_in_background` outlives the turn and the
engine's own background task tools work across turns. Without it, the engine's process
ends with each turn, and the next turn resumes the session in a new one.

- **The engine's tools.** **Fact, read from the Claude Code 2.1.310 bundled with Agent
  SDK 0.2.174 on 2026-10-26.** `TaskStop` is a built-in tool that stops a running
  background task by its identifier, and `KillShell` and `KillBash` are aliases of it.
  `TaskOutput` and `BashOutput` are in the engine's list of removed tools, so no tool
  reads a task's output. A background command's output goes to a file the engine
  names, which `Read` reads. A line is given these tools by naming them in
  `builtin_tools`, as any built-in. `TaskList` is defined beside `TaskStop`, but whether
  it lists background commands was not established.
- **Every check still runs.** At each turn the environment check runs, the
  configuration's MCP servers are expanded, and the instructions, project settings and
  plugins are read and checked, before the kept connection is used. A digest of
  everything the connection was opened with, and of every file under the working
  directory's `.claude` for `project_settings` and under each plugin, is compared with
  the connection's. Any difference closes the connection and opens a new one. A tree of
  more than 4,096 files, or a file over 1 MiB, refuses the turn, since a change there
  could not be noticed.
- **The init check on a reused connection.** **Fact, read from the same engine.** The
  engine reports its `init`, with its tools, credential source and model, on the first
  prompt of a connection, and again only after a change of model or permission mode.
  So before each later prompt Dromon sends the permission mode the last `init`
  reported, which leaves the mode unchanged and makes the engine report its `init`
  again, and the tool, credential and model checks run on it as on a new connection.
- **No call before the init check.** **Decided by the operator on 2026-10-26** in the
  round 2 review. On every Claude Code turn, kept or not, fresh or reused, the
  permission gate denies every tool call until the turn's `init` check has passed, and
  a turn that shows the model's output before an `init` is refused and interrupted. A
  refusal stands for the rest of the turn, so a later `init`, even one that passes,
  cannot clear it. A refused kept turn closes its connection.
- **The gate between turns.** On a kept connection the permission gate denies every tool
  call outside a Dromon turn.
- **Shell tools need a sandbox.** **Decided by the operator on 2026-10-26** in the round 2
  review. A line with `keep_session` and `Bash`, `Monitor` or `PowerShell` in
  `builtin_tools` needs an enabled sandbox, since what such a tool starts in the
  background outlives the turn, and every turn of such a line without one is refused.
  That `Monitor` runs commands is an inference from its description.
- **Loaded files between turns.** **Decided by the operator on 2026-10-26** in the round
  2 review. The engine watches its settings and plugins and may reload them while
  idle, an inference from its reload messages that was not tested. So between turns
  the watch digests every plugin tree, and the working directory's `.claude` under
  `project_settings`, every 5 seconds, and closes the connection as soon as the digest
  differs from the one taken when it opened, or can no longer be taken. The digest runs
  on the event loop's thread, because a Keleusma driver's virtual machine may be
  dropped only on the thread that made it. **A residual risk.** A change made less
  than 5 seconds before the engine reloads it could still reach a turn the engine began
  by itself, before the watch closes the connection. A change during a turn is left to
  the next turn's checks, as the same window between a check and the engine's start
  always was.
- **The driver's budget.** Each turn starts the driver afresh, so its tool budget is per
  turn as before.
- **Turns the engine begins.** **Fact, observed live on 2026-10-26.** When a background
  command ends while the connection is idle, the engine re-invokes the model by itself.
  A self-started turn linked to a pending task end runs as an ordinary Dromon turn when
  the scheduler could start the line at that moment, including the current tick's wake
  interval. The engine cannot predict that alignment. Admission checks and marks the
  line running in one scheduler step before handling the engine's first message, so a
  second event cannot admit another turn in the gap. The admitted turn enters the
  running state, counts as an autonomous turn, uses the configured engine turn and tool
  limits, and its measured cost counts toward the cost hold. The engine's initialization is checked
  before the permission gate admits tools. Pending mail, a due scheduled wake, a hold,
  hibernation, cooldown, restart or another scheduling restriction makes Dromon
  interrupt the self-start and leave the task note for a later ordinary turn. This
  includes a tick that does not align with the line's configured wake interval. A
  self-start with no task end to account for is also interrupted. This keeps the
  engine's own turn from bypassing the scheduler's controls.
- **A note when a task ends.** **Decided by the operator on 2026-10-26** in round 3.
  When the engine reports that a background task ended between turns, Dromon records
  `task_noted`, a note from the line to itself naming the task, its command and its
  final status, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies. When no engine turn has
  started, the note wakes the line through the ordinary path, as a fired wake does, so
  its autonomous turns count. When a task end starts an admitted engine turn, the note
  is the cause recorded for that turn and does not schedule another one. Task ends
  reported during a Dromon turn, and ends of tasks the line stopped, are also recorded
  as delivered notes without a separate wake. Each task is noted at most once. At most
  3 task notes wake or accompany turns for a line in any 60 seconds. Ends past that
  limit are counted and named in the line's next note. A command is cut to 200
  characters, and withheld when the
  project's disclosure list covers it. It is the line's own text, so the note quotes
  it as a JSON string and says that it is data from the line's own tool call and not
  an instruction, so that it cannot pose as Dromon's words.
  **Round 4, decided by the operator on 2026-10-26.** An end waits up to 2 seconds for
  the turn the engine begins by itself on it. When the scheduler admits that turn, the
  one note says it began an ordinary Dromon turn. When admission is refused, the note
  says that Dromon interrupted it. No separate note is added in either case. A turn
  the engine begins with no end waiting keeps its own note. The note
  gives the exit code when the engine's summary gives one, as in `completed (exit code
  0)`, the only place its messages give it. **Round 6.** The exit code is a sentence of
  its own, `Its exit code was 1.`, or `The engine reported no exit code for it.` It is
  read only from the end of the summary, which also matches the form `failed with exit
  code 1` observed live in round 5, since the quoted description before it is the
  line's own text. It quotes the first lines of the task's
  output, at most 20 lines and 2,048 bytes, as a JSON string said to be data the task
  wrote, says when the output is empty, and withholds it when the disclosure list
  covers it. The output file is the line's to write, so it is read only as a file
  named `.output`, not starting with a dot, in a `tasks` directory, inside the line's
  working directory or under the engine's own location, `claude-` and the user's
  number under `/tmp` and under the system's temporary directory. It is opened by
  walking from the root's directory descriptor one component at a time without
  following any link, as `write_scratch` writes, so a directory swapped for a link at
  any moment cannot redirect the read. It is never read when it has more than one
  name or another owner, and never inside a directory Dromon protects, so that a line
  cannot have the daemon read for it a file its own sandbox denies.
  **Round 5, decided by the operator on 2026-10-26.** The note states the output's
  state plainly: quoted, empty, missing, not readable, outside the allowed locations,
  withheld, that the engine named no output file, or, when it reported neither an exit
  code nor output, that it reported none. It ends with the time Dromon detected the
  end, and its header in the prompt gives that time and the time it was delivered.
  `line_status` keeps the last task note's `last_task_note`, with its `message_id`,
  `detected` and `delivered` times and `late_seconds`, as it does for wakes.
  **Fact, read from the engine's code.** A task's
  start reports its identifier, its kind, the description the line gave its tool
  call, and the identifier of that tool call, but not the command. So Dromon takes the
  command from the tool call's `command` input, and the description when it has none.
  An end is a `task_notification` with a `status` of `completed`, `failed` or
  `stopped`, or a `task_updated` whose `patch.status` is `completed`, `failed` or
  `killed`, whichever comes first.
- **A turn left with tasks running.** **Item B1 of 2026-10-28**, from a trial in which a
  line ended its turns with background tasks still running and told no one. When a
  turn ends while the kept connection reports background tasks, and the line sent no
  note or question to any party but itself during the turn, the line's next prompt
  opens with a reminder, once. Dromon saves the task identifiers it saw and checks
  them against the current kept-session listing and task records before describing
  them. It says they are still running only when the same identifiers remain in a
  reliable listing. A recorded end is described as ended, and a task missing from an
  unreliable or replaced session with no recorded end is described as unknown, with
  directions to check its task record and any output file it names. **Fact.** The
  reminder is generated by `scheduler._quiet_notice`. **Inference from the round 4
  report.** Its stale running message is consistent with a remembered task count that
  was never checked against later ends. The report did not identify its exact prompt,
  and the present event log does not retain the earlier prompt text. The wake when a
  task ends already comes through Dromon as the note above, and a turn the engine
  begins by itself is still interrupted, as above. A restart closes the kept
  connection, so the reminder state is held in memory only.
  **Task of 2026-10-30.** A turn the engine starts by itself when a task ends neither
  saves nor clears the reminder, so mail the line sent in it went unseen, and the
  next prompt said wrongly that it sent none. The saved state now holds the count of
  mail the line had sent to any party but itself, and the reminder is dropped when
  that count has grown by the time it would be told. Notes to itself, such as fired
  wakes, do not count. The reminder opens with "A turn of yours ended with" and says
  no mail was sent since, rather than speaking of the last turn. **Decided by core,
  for review.** The reminder is kept when every task it names has a recorded end,
  since the end's task note tells the line alone, and its closing sentence then says
  that no one learned how the work ended.
- **Cost.** **Fact, observed live on 2026-10-26.** On a kept connection the engine
  reports with each result a cost total for every turn since the connection opened.
  **Fact, read from the engine's code on 2026-10-26, and consistent with what was
  observed.** The engine writes the session's total into its transcript and restores
  it when the session is resumed, and starts from zero when it finds none, so a turn
  that is not kept, which resumes the session in a new process, also reports the
  session's earlier cost. So for every Claude Code line, kept or not, a turn's cost is
  the difference from the last total reported for the same engine session, and the
  whole total when the session differs or the total fell, since either means the count
  restarted. **Decided by the operator on 2026-10-26** in round 4. After each turn
  whose engine reports a new total, the daemon records `line_cost_marked` with the
  session and the total in millionths of a dollar, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies, one small event per such turn, and hands the latest to each engine it
  builds, so neither a rebuilt engine nor a restarted daemon counts a restored
  session's history again. A total within a millionth of the mark has not fallen.
  **Fact, observed live on 2026-10-26.** Before this, the first turn after a restart
  counted $97.60 for two acknowledgements.
  **The engine's per-turn figures, from the round 4 review.** **Fact, read from the
  engine's field descriptions.** Its results also carry an internal
  `model_usage_since_last_result`, each model's tokens and cost since the process's
  previous result, with nothing carried from one process to another. The SDK's
  parser drops it, so Dromon's session wrapper reads each raw message as the SDK's
  client does, parses it with the SDK's own parser, and keeps that one field on the
  result. Where a result carries it, the turn's cost and tokens are its sum over the
  models, which covers subagents and helper calls and needs no mark, and the
  difference from the mark is the fallback, also when an entry is malformed. Each
  recent turn in `line_status` says which source it used. The wrapper reads the SDK
  client's internal query, a porting and upgrade hazard that a test with a scripted
  transport guards.
  **Residual, recorded at the operator's direction in the round 4 review.** One
  cumulative total per result cannot tell a restart of the engine's count from
  honest cost, when the restart keeps the session and the new total passes the mark
  within a turn. The difference then undercounts by the mark. The per-turn figures,
  where present, do not have this fault. A
  resume that restores nothing, followed by a turn costing more than the last total,
  would be undercounted by that total, since a restart is told only by a fall. Before
  this, every turn that was not kept counted the session's whole history.
- **Background tasks.** The engine reports each background task's start and end, and
  Dromon counts those running. **Item 8 of the live check of 2026-10-26, read from the
  bundled Claude Code 2.1.310.** The engine also sends `background_tasks_changed`,
  its whole set of live background tasks, whenever the set changes. Dromon adds from it
  a task whose start it did not see, at any time, and removes by it a task the set no
  longer names only between turns, since during a turn a foreground agent still
  running is not in the set. A removed task's end, if reported later, is still noted.
  A malformed set, or one longer than 256 entries, removes nothing. A connection closes after `keep_session_idle_seconds`
  without a turn, or, while the engine reports tasks running, when
  `keep_session_max_seconds` and `keep_session_at_max` say, as the ceiling section
  below specifies, all counted from the end of the last turn and checked every 5
  seconds. Closing the connection ends the engine's process. An error in the
  watch is reported on the daemon's standard error and the watch goes on, so neither
  deadline is left unenforced. An interrupt or a close begun outside a turn is held by
  the engine until it ends, since asyncio keeps only weak references to tasks.
- **Other closes.** The connection also closes when the line is configured, overridden,
  rebound or has its session cleared, which includes a change of `workdir`, when the
  daemon stops, and when a turn is refused or fails, including by a check before the
  connection is used, since the engine may already have loaded what the check refused. A change made while a turn runs
  closes the connection when that turn ends. The next turn resumes the session in a new
  process as before, and when Dromon closed it for a reason the line could not see, the
  prompt says why and how many background tasks were running.
- **A resumed session's handoff.** **Item 6 of the live check of 2026-10-26. Fact,
  read from the bundled Claude Code 2.1.310.** A session resumed with a turn its last
  process left unfinished settles that turn on its first prompt, and where nothing is
  left to run, reports a result of subtype `success` with `num_turns` of zero, an
  empty `result` and no model call, without reading the prompt. A Dromon turn, kept or
  not, that receives such a result before any of the model's output counts its cost as
  a `handshake`, notes in the line's activity that the prompt was sent again, and
  sends it once more. A second such result refuses the turn, which closes a kept
  connection, since a success that ran nothing would misreport the turn. A result that
  reports an error is the turn's outcome as before.
- **The engine's death.** When the engine's process ends between turns, the next turn
  resumes the session in a new process, and its prompt says that the old process's
  background tasks were lost. When it ends during a turn, the turn fails, and the next
  prompt says the same.
- **Interrupt.** An operator's interrupt cancels the turn. Dromon interrupts the engine
  and discards the rest of that turn's output. If the engine does not take the
  interrupt within 10 seconds, the connection is closed. **Row 1 of the T3 Code review,
  decided by the operator on 2026-10-26.** Once the stopped turn has ended, a
  connection whose interrupt settled while the engine still reports background tasks
  running, or before the stopped turn's result arrived, is closed too, which ends the
  engine and, by the stop policy, what it started. Otherwise the connection stays
  open. [`CLIENT.md`](CLIENT.md) specifies what else a stop does. **Hypothesis, not
  observed.** The engine may stop its background tasks on an interrupt.
- **`line_status`.** `kept_session` gives whether the line is `configured` to keep its
  session, whether a connection is `kept` now, the time it `closes_at` in Coordinated
  Universal Time, which is `null` during a turn, the number of `background_tasks` the
  engine reports running, `background_tasks_unknown`, true when that count may be
  stale because the engine cannot give a reliable current listing, and `tasks`, each
  with its `task_id` and `command`. The compact status also gives
  `background_tasks_unknown` next to `background_task_count`. Round 3
  adds `last_fired_wake`, the latest scheduled wake delivered to the line, with its
  `message_id`, `due` and `delivered` times and `late_seconds`, or `null`. Round 4
  gives `recent_turns`, the line's last ten turns, oldest first, each with its
  `cost_usd` as a decimal string, a `cost_note` saying plainly when the engine reports
  no cost, as Codex does, its `usage`, the engine's token counts for the turn where it
  reports them, and a `usage_note` saying what they cover. **Fact, read from the
  engine's field descriptions.** Claude Code's per-turn `usage` covers its main loop
  only, while its cost also covers subagents and helper calls. `autonomous_turns` adds
  `current_turn_counts`, whether the turn in flight counts as autonomous, and a
  `note` saying that `used` counts finished turns. All of these are kept in memory and
  start empty after a restart. Step 6 of federation adds `peers`, each peer the line
  may send to with its `peer`, `state`, `waiting`, `oldest`, `oldest_sent_at` and
  `last_contact`, as [`FEDERATION.md`](../architecture/FEDERATION.md) describes, in
  the compact view too.
- **Warning before a loss.** **Decided by the operator on 2026-10-26** in round 3. When
  `configure_line`, a line's `set_setting`, `rebind_line`, `clear_session` or `stop`
  closes kept connections with background tasks running, the response adds
  `background_tasks_lost` and a `warning` naming the count, which `dromon stop` also
  prints. The action is not blocked. The count is read just before the action, so a
  task that ends in between is counted.
- **Other engines.** A Codex line may keep its session too, with the checks and tools
  [`CODEX.md`](CODEX.md) specifies. A Grok line refuses `keep_session`.

### The ceiling

**Decided by the operator on 2026-10-26**, decision A, from row 12 of the T3 Code
review, which asked whether `keep_session_max_seconds` may end background work. A kept
connection, of a Claude Code or a Codex line, that still has background work running
`keep_session_max_seconds` after the line's last turn does what `keep_session_at_max`
says.

| Value | At the ceiling |
|---|---|
| `close` | The connection closes, which ends its background work. The default, and the behaviour before the choice existed |
| `ask` | Dromon asks an operator, the one who last wrote to the line or the project's default, with the choices `extend`, `stop` and `let it finish`, and keeps the connection until the answer comes |
| `keep` | The connection stays open while the work runs |

- **The hard bound.** Whatever was chosen, the connection closes
  `keep_session_hard_seconds` after the last turn, so no wait is unbounded. An
  unanswered question keeps the connection until then.
- **The answers.** `extend` allows another `keep_session_max_seconds` from the answer,
  after which Dromon asks again. `stop` closes the connection at once. `let it finish`
  keeps it while the work runs, for the rest of the connection's life. An answer is
  read with its case and surrounding space set aside, and one that is none of the
  three decides nothing, so the connection closes at the hard bound. The answer also
  reaches the line as mail, as every answer does, and wakes it.
- **Withdrawal.** A turn starts the count again and withdraws a question still open,
  since its premise changed, and so do the connection's close and, **review F12**, its
  death without a close. **Review F11.** The connection does not survive a restart, so
  the daemon withdraws every ceiling question still open as it starts, told by its
  choices and its fixed wording. A line's own question written to match is withdrawn
  with them, which is accepted.
- **Without an operator.** A question that cannot be recorded, for any reason, closes
  the connection as `close` does.
- **Why `close` is the default.** It keeps the meaning of every configuration stored
  before the choice existed, and it is the only choice that an operator who is away
  cannot turn into a longer wait, since an unanswered `ask` lasts until the hard bound.
- **Notes.** The next prompt says why Dromon closed the connection. The reasons are
  `keep_session_idle_seconds`, `keep_session_max_seconds`, the hard bound, an
  operator's `stop`, and a question that could not be asked.

## Stop policy

**Decided by the operator on 2026-10-26**, decision B, from row 2 of the T3 Code review.
Every harness, of every engine, runs under Dromon's launcher, which ends it when the
daemon stops it, when the daemon stops or dies, and when the line's engine is stopped.
`stop_policy` decides what else ends with it.

| Value | Effect |
|---|---|
| `kill_all` | The harness, its process group, and every descendant a walk of the process table finds are terminated and then killed. The default, which fails closed |
| `leave_background` | The harness alone is frozen, listed around and killed, without being asked to stop or seeing its input close, and whatever it started is left running and recorded |

- **What is left is recorded.** Under `leave_background` the launcher writes the
  processes it left, at most 64 with their pids and commands, to a report in the
  state directory's `left` directory, readable by its owner alone. The daemon records
  each report as a `processes_left` event, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies, at the end of each turn, at each `line_status`, at each rest of its
  scheduler, at most a minute apart, and when it starts, which covers a report written
  after a crash. `line_status` gives the line's latest under `processes.left`, with
  its `stop_policy`. A recorded process is no longer Dromon's to end.
- **Commands are recorded masked.** **Review N9 and N10 of the stop policy's
  re-review, 2026-10-27.** A command line can carry a credential. The event log and
  `line_status` therefore record each left process as its pid, its executable's name
  and its arguments with bearer and basic credentials, a user and password in a URL,
  and the values of options and `name=value` pairs whose names hold `password`,
  `passwd`, `pwd`, `token`, `key`, `secret`, `credential` or `auth` masked. The whole
  command stays only in the report, which the daemon keeps aside once read, the
  newest 256. A background task's command and description are masked the same way
  as they are taken in. Masking is by pattern, so a credential in another form, such
  as a bare argument, is recorded as it is.
- **The daemon's death.** The launcher checks every half second that its parent is
  still the daemon that started it, which on macOS and on Linux stops being so when
  the daemon exits, however it exits, and then stops the harness by the policy.
- **Detached processes.** **Row 2 of the T3 Code review.** At the end of a turn whose
  engine has ended, the line's processes that still carry its mark have left the
  engine's tree, so no stop reached them. They are recorded with the cause
  `detached`, and are not ended. The same set is not recorded twice in a row.
- **Order and bounds.** **Review of the stop policy, F3 to F5, 2026-10-26.** Under
  `kill_all` the launcher's first act is `SIGSTOP` to the harness's whole group, so
  that nothing in it exits, and so orphans a child, while the process table is
  walked. Then `SIGTERM` and `SIGCONT` go to the group and to every descendant found,
  and `SIGKILL` to what remains 2 seconds later. Under `leave_background` the harness
  runs on for half a second with its input still open, then alone is stopped, listed
  around and killed. Each walk retries within a 0.75-second deadline, and `kill_all`
  confirms after `SIGKILL` for a further 0.25 seconds. A killed harness has half a
  second to be reaped. These bounds sum to 4.75 seconds. **Fact, read
  from Agent SDK 0.2.174.** The SDK's close closes the engine's standard input, waits 5
  seconds, sends `SIGTERM`, waits 5 seconds more and sends `SIGKILL`, to the launcher,
  which therefore finishes with a quarter second to spare. The daemon signals a launcher by
  its pid and by its group, kills it 6 seconds later, and waits 2 seconds more for it,
  then reports one that is still there and goes on.
- **A launcher that cannot finish.** **Review N1 of the stop policy's re-review,
  2026-10-27.** A launcher killed with `SIGKILL` cannot act, and one killed between
  its `SIGSTOP` and its `SIGCONT` would leave the harness's group frozen and alive. As
  the harness starts, the launcher therefore writes a launch record beside its
  reports, naming the line, itself, the harness's pid, which is its group's, the
  harness's executable and the policy, and marks it done once its stop has finished.
  Once a launcher has exited, for any reason, the daemon sweeps the group its record
  names, within the same bounds. Under `kill_all` it sends the group and the harness's
  descendants `SIGTERM` and `SIGCONT`, then `SIGKILL` 2 seconds later. Under
  `leave_background`, if the launcher did not finish, it kills the harness and sends
  its group `SIGCONT`. It records a stop the launcher did not finish as
  `stop_unverified`, which `line_status` shows as `processes.stop_unverified`, and
  what still ran as `processes_left` with the cause `swept`. It touches no group whose
  leader now runs another executable, or whose pid a running launcher's record
  claims, and when the process table cannot be read it touches nothing and the record
  waits. It reads the table whenever a record is unfinished, or its launcher's pid no
  longer answers a signal, so a sweep costs one `ps` while any engine runs. A
  launcher counts as exited when its pid is gone, when `ps` shows it `<defunct>`, or
  when its harness has another parent, since a running launcher stays its harness's
  parent until it reaps it. **Third live run, 2026-10-26.** A kept connection's
  launcher killed with `SIGKILL` still answered a signal for over 80 seconds, and the
  sweep, which then looked only at that answer, never reached its record while the
  daemon ran. That it stayed an unreaped child of the daemon is an inference, which
  a probe of the Agent SDK's process start did not reproduce. When a swept launcher
  was started since the line's present engine was built, the daemon treats that
  engine as failed, closes its connection after the sweep, says so in the line's
  activity, and builds a new engine for the next turn. A kept connection otherwise
  looks healthy, since its harness keeps the Agent SDK's pipes. Closing after the
  sweep keeps an orderly close of a living harness from ending the background work
  that `leave_background` keeps. The daemon sweeps at the end of each turn, at each
  `line_status`, as it starts and as it stops, and at each rest, which lasts at most
  60 seconds even when no wake is pending, so an idle daemon sweeps at least once a
  minute. **Fourth live run, 2026-10-26.** The rest was unbounded without a pending
  wake, so an idle daemon with a kept connection never swept its dead launcher. Each
  sweep that acts, each failure of one, and the process table's being readable again
  after a failure are written to the daemon's log. It reaches only the harness's group
  and, while the harness lives, its descendants, so a descendant in a group of its
  own whose parent exited is beyond it. On Linux the harness is also given a parent
  death signal, `SIGTERM` under `kill_all` and `SIGKILL` under `leave_background`, so
  it learns at once that its launcher died.
- **No reaped pid is signalled.** The launcher sees the harness exit without reaping
  it, and reaps it last, so the harness's pid, and with it its group's identifier,
  cannot have been reused by an unrelated process when it is signalled.
- **A walk that cannot be made refuses the turn.** **Review F2.** Under `kill_all` the
  daemon asks before each turn whether `ps` can list the process table, and refuses to
  start the turn, saying why, when it cannot. A failed walk is never taken for an
  empty tree. A typed timeout result is retried within a 0.75-second deadline. Other
  process-table errors are not retried. If a walk still fails at stop time, the launcher
  ends the harness's group, writes an unverified-stop report and exits unsuccessfully.
  After `SIGKILL`, it waits another 0.25 seconds and reports an unverified stop if a
  process still answers. The daemon records that report as `stop_unverified`.
  **Review F2 of the re-review, 2026-10-27.**
- **The harness's own teardown.** **Item 7 of the live check of 2026-10-26.** Under
  `leave_background` a graceful stop of the daemon still ended a background `sleep`,
  and nothing was recorded. **Fact, read from the bundled Claude Code 2.1.310.** Once
  its input has closed, Claude Code's print mode kills each background shell after a
  grace of 5 seconds, and kills any shell still running when its stream closes. The
  Agent SDK's close closes that input first, so Claude Code ended its own background
  work before the launcher could act. Under `leave_background` the launcher therefore
  stands between the daemon's input and the harness's, when that input is a pipe or a
  socket. It copies the input across and never closes the harness's side. When the
  daemon's side closes, or a stop arrives, the launcher lets the harness run on for
  half a second, then freezes it, lists its descendants and kills it with `SIGKILL`,
  which no harness can answer by ending what it started. Under `kill_all` nothing is
  relayed, and the harness's own close is kept. **Review N3.** The relay holds up to
  1 MiB the harness has not yet read, so a harness that stops reading does not keep
  the daemon's close from being seen. Past that bound it reads no more, and sees no
  close, until the harness reads again, while the stop signals and the parent check
  still reach the launcher.
- **What `leave_background` costs.** The harness has no orderly exit under it. Claude
  Code writes its session as it goes, and a session was resumed after a `SIGKILL` in
  the live check, but whatever it would have written as it closed is lost. **Review
  N2, read from the bundled Claude Code 2.1.310.** Claude Code offers no request that
  writes out its session without ending it or starting a turn. It writes its
  transcript from a queue it drains every 100 milliseconds, and as it resumes it seals
  a last line that a kill cut short. The half second the harness runs on before its
  freeze is that chance to write, five such drains. Whether a write larger than one
  drain can still be cut is not verified. A kept
  connection that Dromon closes, at its ceiling, its hard bound or an operator's
  `stop`, also leaves its background work running under this policy, and the ceiling's
  question and the line's next prompt say so instead of promising an end.
- **Platform limits.** **Review F1.** On Linux the launcher is a child subreaper, so a
  descendant whose parent exited, even one in a session of its own, is given to the
  launcher, and the walk reaches it. macOS has no subreaper. There a descendant that
  started a session of its own and whose parent exited before the walk is reached by
  no walk, and only its mark finds it, which macOS 27 does not let the daemon read.
  The daemon records that gap once in the event log, as `walk_limited`, and shows it
  in every line's status as `processes.walk_limit`. As it starts, the daemon also
  records the processes of an earlier run that still carry a line's mark, where it
  can read marks, as `detached`. A descendant that is not the launcher's own child
  can still be reaped by its parent, and its pid reused, between the walk and a
  signal, which repeating the walk before the last round narrows. `dromon.launcher`
  records these in full.

### Unverified under Claude Code, and the check to run first

**Review F7, F8, F10, F9, F5 and F16.** The tests use scripted harnesses only. What
follows is read from the Agent SDK's and the bundled Claude Code's source. The live
check of 2026-10-26, run by the operator's session with Claude Haiku 5.5 on macOS,
observed part of it, as the list below records.

- **No terminal.** **F7.** The harness runs in a session of its own, with no
  controlling terminal. Under the Agent SDK it had none before either, since the SDK
  starts it with pipes, so no change in behaviour is expected. This is an inference.
- **The command path and the version check.** **F8. Fact, read from Agent SDK
  0.2.174.** The SDK uses `cli_path` only to run it, and reads nothing beside it on
  disk. Before each connection it runs `cli_path -v` with its own environment, not the
  engine's, under a 2 second bound, warns when the version is below its minimum, and
  ignores every failure. The launcher answers that query from the same lookup the
  engine starts Claude Code from, by running that command with `-v` in its place. A
  test runs the SDK's own check through the launcher with a scripted version. The
  lookup imports the SDK, which took about 0.7 seconds here, inside the 2 second
  bound.
- **Signals and exit status.** **F10.** The harness sees no signal from the SDK
  directly. It sees `SIGSTOP`, `SIGTERM` and `SIGCONT` from the launcher, and `SIGKILL`
  two seconds later if it stays. A harness that dies by signal `n` makes the launcher
  exit with `128 + n`, where the SDK would have read `-n`. **Fact, read from Agent SDK
  0.2.174.** The SDK uses the status only in the text of the error it raises for any
  status other than 0, so only that text differs.
- **Settings and the pinned tool search.** **F9. Read from the bundled Claude Code
  2.1.310.** `setting_sources=[]` loads none of the user, project and local settings
  files, so none of their env blocks applies. Claude Code still copies the `env` key
  of its global configuration file, `~/.claude.json`, over its process environment,
  and then the flag and the managed policy settings' env blocks. Dromon therefore also
  pins `ENABLE_TOOL_SEARCH` in the flag settings it passes, which come after the
  global configuration. Managed policy settings come last, and Dromon does not
  override them.
- **The SDK's timing.** **F5.** The 5 second windows are read from the SDK, and the
  launcher's 3.5 second budget is measured only against scripted harnesses.
- **Fallback messages.** **F16. Read from the message schemas in the bundled Claude
  Code 2.1.310.** `model_fallback`, `model_refusal_fallback` and
  `model_consent_fallback` each name `original_model` and `fallback_model`. A turn is
  refused only when `fallback_model` is not the driver's model, and a consent fallback
  is also put to an operator as a question.

**The verify-first list,** for a live check with Claude Haiku 5.5, in this order. An
interrupt can be scripted with `dromon interrupt LINE`, as
[`CLIENT.md`](CLIENT.md) specifies.

1. A turn under the launcher completes, with `line_status` showing it `completed`, and
   its result and cost match a turn of the same prompt without the launcher, which can
   be had by pointing `cli_path` at Claude Code directly in a scratch run. **Observed
   in part on 2026-10-26.** A probe under the launcher finished in 3.7 seconds and
   reported Claude Code 2.1.310. The comparison without the launcher was not run.
2. The SDK's version check through the launcher reads the bundled Claude Code's
   version, which the SDK's warning at a raised minimum shows, and finishes within its
   2 second bound. **Not observed separately on 2026-10-26.** The probe raised no
   version warning, which is consistent with it and does not show it.
3. An operator's interrupt of a turn that runs `sleep 600` in the background ends the
   harness and the `sleep` within 5 seconds of the SDK's `SIGTERM`, and the launcher
   exits before the SDK's `SIGKILL`. **Not run on 2026-10-26,** for want of a
   scriptable interrupt.
4. The same with a background command that starts a session of its own, as `setsid
   sleep 600` does on Linux, and on macOS that `processes.walk_limit` names the gap.
   **Not run on 2026-10-26.** The tree observed was the launcher, then Claude Code,
   then a `bash` in a process group of its own, then the `sleep` in that group, so
   Claude Code puts each background command in a new group, which a signal to the
   harness's group alone never reaches.
5. The daemon killed with `SIGKILL` during such a turn leaves no harness and no
   `sleep` running. **Observed on 2026-10-26** under `kill_all`. All four processes
  were gone within about 6 seconds, longer than the about one second this item first
  expected. The parent check and the stop's declared bound account for at most 5 seconds of
   it. The rest is not explained, and the observation's own resolution is not known.
6. `leave_background` leaves the `sleep` running and records it in
   `processes.left`, after a graceful `dromon stop` and after an interrupt. **Failed
   on 2026-10-26** for a graceful stop, which Claude Code answered by ending the
   `sleep` itself, as the stop policy section says. The input relay is the change made
   for it, and is to be checked again.
7. With `"env": {"ENABLE_TOOL_SEARCH": "auto"}` in `~/.claude.json`, the init message's
   tools still show tool search on. **Not run on 2026-10-26.**
8. A harness that exits with an error shows the SDK's error text with the launcher's
   status. **Not run on 2026-10-26.**
9. The first turn after the daemon is killed with `SIGKILL` during a turn, on the
   resumed kept session, runs its prompt. **Failed on 2026-10-26.** It ended in about 2
   seconds as a success with no tool call, no text and no cost, and the waiting
   message was answered only in a later turn. The engine now sends the prompt once
   more after such a handoff result, notes so in the line's activity, and refuses the
   turn after a second, as [Kept sessions](#kept-sessions) says. That the handoff
   reaches no model output before its result, which the check relies on, is read from
   the source and is to be checked again. **Review N5.** Any success with no turn
  before the model's output now counts as a handoff, whatever its text, and the text
  is shown in the line's activity.
10. The daemon's log has no line for an unhandled `background_tasks_changed`, and
    `line_status` counts a background task that ends between turns out as soon as the
    engine's task set drops it. **Observed on 2026-10-26** as unhandled, which is now
    handled, and is to be checked again.
11. Under `leave_background`, a kept session closed at its ceiling or by an operator's
    `stop` leaves its background work running and records it, and its question says
    so beforehand. **Not run.**
12. Under `leave_background`, a `dromon stop` of the daemon leaves a turn's transcript
    whole, which a following turn resumes, and the launcher's half second of grace
    shows in the stop's time. **Not run.**
13. A launcher killed with `SIGKILL` during a turn, with the daemon still running,
    leaves its harness swept within a minute, and `processes.stop_unverified` says so.
    On Linux the harness ends at once by its parent death signal. **Third live run,
    2026-10-26, on macOS.** The sweep as the daemon started worked, but a kept
    connection's killed launcher was not swept while the daemon ran. The fourth run,
    after a fix of the liveness check, still swept nothing for 4 minutes, because an
    idle daemon's rest was unbounded. Both have been changed since, and item 14
    checks them. **Not run since.**
14. A kept connection's launcher killed with `SIGKILL` between turns, with the
    daemon still running, leaves the line's activity saying its engine was closed as
    failed, and the next turn starts a new connection. **Not run.**
15. Claude Code does not exit promptly when its input closes. **Observed in the
    third live run, 2026-10-26, on macOS.** With its launcher and then the daemon
    killed with `SIGKILL`, Claude Code 2.1.310 still ran 10 seconds after its input
    closed. So neither the launcher's stop nor the sweep relies on its own exit, and
    each ends it by signal within its bound.

## Instructions

Dromon starts Claude Code with no settings sources, so Claude Code reads no
`CLAUDE.md` by itself, and without `claude_code_prompt` the SDK passes an empty system
prompt. **Fact, read on 2026-10-25** in the SDK's command construction. The files named
in `instructions` are read at the start of each turn, each under a heading naming it,
and a file that is missing, larger than 256 KiB or not UTF-8 refuses the turn. Only the
paths are stored. On Codex the text becomes the thread's developer instructions, and
`claude_code_prompt` is refused, since Codex uses its own.

**Checked as the configuration arrives.** Review of 2026-10-27, findings I0-1 and
I0-2, by the operator's decision. A configuration is refused, with nothing recorded,
when it is created or configured by a client, overridden, or read from the project
file at start, at reload or by `dromon config`, if any of its instruction paths fails.
- A relative path must resolve, after `..` and symbolic links, inside the project
  root, so a project file cannot name the operator's other files through it.
- A path from `~` or from the root of the file system is the operator's explicit
  choice and may lie anywhere, such as `~/.claude/CLAUDE.md`.
- Every path must name a regular file that exists.

A refusal from the project file names the line, the key, and the line of the file that
holds it, which is the profile's table when the line takes its instructions from a
profile. A relative path is checked again for containment when a line's engine is
built, so a symbolic link changed after loading refuses the turn. A file removed after
loading still fails the turn that reads it. **Any failed turn.** A turn that fails
before its engine runs, including one whose prompt could not be composed, holds the
line and asks an operator, unless the line mailed an operator during the turn.

## Working directory

`workdir_access` defaults to `write`. With `read`, Claude Code and Codex apply their
engine sandbox or permission profile to prevent writes under the working directory.
Dromon's `write_scratch` tool is the supported write path, into the line's scratch as
above, which for a line that does not own a role is its own `tmp/lines/LINE`. That
directory is also the one a Codex line in this mode may write, checked for links on
every turn, as [`CODEX.md`](CODEX.md) records. Pi, Cursor Agent
and Antigravity refuse to start in this mode because their adapters cannot enforce it.
Grok refuses it too until a live probe shows its reader profile deny a write to the
worktree, as [`GROK.md`](GROK.md) records. Claude Code also refuses configurations with mechanisms that can
bypass its sandbox, as specified in [`SANDBOX.md`](SANDBOX.md).

**Project `read_only_paths`.** The project file may name existing files and directories
relative to each line's resolved workdir. Their real paths must stay inside that
workdir. The setting applies to every line and has no per-line override. It defaults
to an empty list. Claude Code, Codex and Grok Build enforce the paths. Pi, Cursor Agent
and Antigravity refuse to start a line while the setting is non-empty because their
adapters cannot enforce it. Dromon checks the configured paths again whenever it builds
an engine, and refuses a missing path or a symbolic link whose target leaves the
workdir. Dromon's `write_scratch` tool also refuses a target at or below a configured
read-only path, including paths under `tmp/`. Codex profiles add explicit read entries
under the `tmp/` write grant and retain read-only parents around protected descendants.
The test suite checks the generated profile, not Codex's runtime precedence for these
overlapping entries.

These engine rules are based on resolved paths when the engine is built. Dromon has not
verified how Claude Code or Codex applies them after a writable parent is renamed, or
when a line creates a new symbolic link that points into a read-only path. Until those
cases are checked against the live engines, do not rely on `read_only_paths` to prevent
those operations. The setting does not freeze directory names or re-resolve links on
every engine file operation.

**Codex writable paths.** An operator may name additional absolute directories with
`writable_paths` for a Codex line given `Edit` or `Write`. For example, a line may use
this configuration to write build output on a separate volume.

```json
{
  "builtin_tools": ["Bash", "Edit"],
  "writable_paths": ["/Volumes/build-output"]
}
```

At line configuration load, Dromon checks only that each path is absolute and unique.
When a Codex turn builds its permission profile, Dromon then requires each path to
exist as a directory and resolves symbolic links. It refuses overlap with known
credential paths, project and workdir `secret` directories, Dromon state and runtime
directories, the workdir's Git directories, and federated peer directories. Other
engines refuse `writable_paths` when the engine is built. The credential list is not
complete, and a project may contain other stores. The grant allows writes through the
Codex permission profile. It assumes the operator trusts the named directory and its
contents.

**Claude Code local binding.** The existing sandbox schema accepts
`network.allowLocalBinding`. A line may allow a sandboxed command to bind a local
network port with a setting such as this.

```json
{
  "sandbox": {
    "enabled": true,
    "network": {"allowLocalBinding": true}
  }
}
```

This setting does not grant Unix socket access, which remains controlled by
`network.allowUnixSockets`.

**Decided by the operator on 2026-10-25.** Each line's engine runs in its own working
directory, so that a line can work in a git worktree of its own while the daemon's
state stays in the project.

- **Resolution.** `workdir` is text of 1 to 4,096 characters without NUL. A relative
  path is resolved against the project root, and `~` or `~/` against the home
  directory. Another user's home, such as `~ana/`, is refused. Only the path is stored.
- **Checks.** After symbolic links are resolved, the directory must exist. It may not
  be the project's state directory or runtime directory or lie inside either. It may
  not contain either, unless it is the project root itself, which contains the state
  directory by default and is protected as every line's root is. A configuration that
  fails a check is refused when it is created, configured or read from the project
  file, and a turn whose directory has since vanished or moved under a protected
  directory ends before its engine starts.
- **Default.** Without `workdir` a line runs in the project root, as before, and its
  configuration encodes exactly as it did before the key existed.
- **What follows the directory.** Claude Code's working directory, the Codex workspace
  and its `:workspace_roots`, and the Grok agent's working directory. The relative
  paths of a line's sandbox document resolve against it. Its relative `instructions`
  paths do not, and resolve against the project root. **Luna's turn of 2026-10-27.**
  They resolved against the workdir until then, so a line moved to a worktree lost a
  file only the project root held, and its turn failed before it began. The engine
  receives each such path made absolute, so its heading in the composed instructions
  and in `read_instructions` names the absolute path, while the stored configuration
  keeps the path as written. Permission rules in its settings are interpreted by Claude Code against the same
  working directory. The project's state and runtime directories stay protected for
  every line wherever it runs.
- **Sessions.** Claude Code keeps a session under the directory it ran in. When a
  line's `workdir` changes, by `configure_line` or by the project file at start or
  reload, the daemon appends `line_session_cleared`, so the line's next turn starts a
  new session. A turn in flight at the change does not record its session, its
  context estimate or a compaction wake, as round 9's review, F1, decided. The
  comparison is of the stored text, so writing the same directory another way also
  starts a new session, including a first explicit `workdir` that names the project
  root. Other configuration changes keep the session.
- **Limit, a re-pointed link.** Because only the text is compared, a `workdir` that
  names a symbolic link keeps its text when the link is re-pointed to another
  directory. No `line_session_cleared` is appended, and the line's next turn resumes a
  session from the old directory, which Claude Code is then expected not to find. The
  directory itself is checked again when the engine is built, so the new target must
  still be allowed. An operator who re-points such a link can send `clear_session`.
- **Clients.** A client cannot change `workdir` through `set_setting`. The settings
  view shows it as an `info` entry, and `configure_line`, which replaces the whole
  configuration, is the way to change it. A `configure_line` without `workdir` returns
  the line to the project root.
- **Git safety.** Every enabled Claude Code sandbox denies writes to the hooks and
  configuration of the git checkout the line works in, as [`SANDBOX.md`](SANDBOX.md)
  specifies.

**Fact, observed on 2026-10-26 with Claude Code 2.1.310.** Claude Code stores a
session under a directory of `~/.claude/projects/` named for the working directory. A
Haiku line with its workdir in a linked worktree had its session stored under that
worktree's name, so a session resumed from another directory would not be found.

## Plugins

**Decided by the operator on 2026-10-26.** A line may load the local plugins its
instructions require, such as one that provides a skill, without opening Dromon's
isolation. Dromon loads no user setting source, so a user's installed plugins do not
reach a line unless its configuration names them.

- **Authority.** `plugins` is set only by the project file, by `create_line` or
  `configure_line` from a client, or by an external agent with `operator` access. An
  agent with `manage` access is refused, and `set_setting` cannot change it. The
  settings view shows it as an `info` entry.
- **Loading.** Each directory, with `~` expanded and links resolved, is handed to the
  engine as a local plugin, as the Agent SDK's `plugins` option does, so no setting
  source is opened.
- **Checks.** At every turn, before the engine starts, each plugin is checked against
  the allowlists [`SETTINGS.md`](SETTINGS.md) specifies, and a plugin that fails refuses
  the turn, naming the plugin and the key or entry. The same check runs when a
  configuration is accepted and when `dromond` reads its project file.
- **Skills.** A plugin's skills are used through the `Skill` tool, so a line that uses
  one lists `Skill` in `builtin_tools`. The init check accepts `Skill` only when it is
  configured and still refuses every other tool the line was not given.
- **Write protection.** An enabled sandbox denies writes to each plugin directory,
  whatever the line's tools, and the line's settings deny it to the built-in editing
  tools with an `Edit` rule, so a line cannot change what loads at its next turn. A
  line that loads any plugin must run with an enabled sandbox, whatever its tools.

## Engines

A line on the Codex, Grok Build, Pi or Cursor Agent engine accepts only the
configurations [`CODEX.md`](CODEX.md), [`GROK.md`](GROK.md), [`PI.md`](PI.md) or
[`CURSOR.md`](CURSOR.md) lists, and any other is refused when the line is created or
configured. Pi lines take no built-in tool. Cursor and Antigravity lines are refused
whatever their configuration, as [`CURSOR.md`](CURSOR.md) and
[`ANTIGRAVITY.md`](ANTIGRAVITY.md) record.

## Storage

The daemon validates a configuration, then appends `line_configured` with its canonical
JSON, which adds `"v": 1` and sorts every key. A new configuration replaces the old one
whole and applies from the line's next turn, which resumes the same engine session
unless the line's `workdir` changed.

## Trust

A configuration is operator authority and arrives only through the client. Server
`env` and `headers` may hold credentials. Literal values are stored in the event log,
whose file is readable by its owner only, and references are stored as written. `allow_all` and `inherit_account_mcp` hand a line every
tool they bring, including a claude.ai account's mail and file connectors, and suit
only lines the operator trusts with them.

Git over the Secure Shell protocol is not part of this document. A line receives the
project's `[git_ssh]` agent only through `git_ssh = true` on its own table or its
profile in the project file, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies,
so it never enters the event log and no client or line can set it. Added on
2026-10-29.

## Not yet specified

- References to secrets held in files or a keychain rather than the environment.
