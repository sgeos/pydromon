# Project Configuration

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, file version 1, written on 2026-10-25. Normative. The Python module
`dromon.projectconfig` implements it, and `dromon.daemon` applies it.

## Purpose

A project declares its session lines, their engines and tools, its conversation limits
and its wake policy in `.dromon/dromon.toml`. The daemon reads the file when it starts.
`dromon config` validates the file without a daemon and prints a summary. By the
operator's direction, the format is expected to grow as large as a multi-agent project
needs, so it is strict and versioned from the start.

After printing the summary, `dromon config` warns when multiple lines with a shell,
file-write or Model Context Protocol (MCP) tool resolve to the same workdir, or when
their tool policy allows every tool. Assign writing lines separate worktrees to avoid
concurrent edits to one checkout. This is a warning only because tool names do not
establish the full write capability of an engine or remote tool.

## Format

The file is TOML version 1.0, encoded in UTF-8, at most 4 MiB.

```toml
version = 1

[daemon]
operator = "ana"        # who hears about a held line no operator has messaged
tick_seconds = 0.5      # time between ticks while work is in flight, in [0, 60]

[limits]
max_autonomous_turns = 8
max_cost_usd = 1.5
cooldown_seconds = 0

[wake]
every_ticks = 2         # a line may start a turn only on ticks divisible by this

[profiles.reader]
model = "claude-haiku-5-5"
strategy = "subscription"
builtin_tools = ["Read", "Grep"]

[profiles.reader.mcp_servers.docs]
type = "http"
url = "https://docs.example/mcp"

[lines.alpha]
profile = "reader"

[lines.beta]
profile = "reader"
model = "claude-opus-5-5"
builtin_tools = ["Read"]
tool_policy = "allow_all"

[lines.beta.limits]
max_autonomous_turns = 3

[lines.beta.wake]
every_ticks = 5

[lines.gamma]
model = "grok-4.7"
may_own = false         # a seat that may read and advise but never own its role

[roles.docs]
workdir = "tmp/worktrees/docs"
owner = "beta"          # the first owner, recorded only while the role never had one
seats = ["beta", "gamma"]
handoff_seconds = 3600  # the default
failover = "automatic"  # the default, or "ask"
handoff_below_percent = 10  # the default, or 0 for never
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | required | Must be 1 |
| `daemon.operator` | identifier | `operator` | The default operator |
| `daemon.tick_seconds` | number in [0, 60] | 1 | The tick cadence |
| `daemon.require_sandbox` | boolean | `false` | Whether every Claude Code line must run with an enabled sandbox, as below |
| `daemon.disclosure_terms` | path | none | The project's disclosure list, relative to the project root, as below |
| `leases.names` | list of identifiers | none | The leases lines may acquire, each named once, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies |
| `leases.ttl_seconds` | integer in [60, 86400] | 10800 | How long an acquired lease lasts unless released first |
| `limits.*` | as in `CLIENT.md` | 12, 2, 0 | The conversation limits of every line |
| `wake.every_ticks` | integer in [1, 3600] | 1 | The wake policy of every line |
| `profiles.NAME` | table | none | A reusable line template with every line key but `profile` and `session` |
| `lines.ID` | table | none | A line, named by a line identifier |
| `lines.ID.profile` | profile name | none | The profile the line starts from |
| `lines.ID.model` | model | required | `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`, `gpt-6-luna` on Codex as [`CODEX.md`](CODEX.md) specifies, `grok-4.7` on Grok Build as [`GROK.md`](GROK.md) specifies, `pi:openai/gpt-6-luna` on Pi as [`PI.md`](PI.md) specifies, `cursor-agent:gpt-5.6-luna-medium` on Cursor Agent as [`CURSOR.md`](CURSOR.md) specifies, or `antigravity:gemini-3.8-flash-high` on Antigravity as [`ANTIGRAVITY.md`](ANTIGRAVITY.md) specifies. A daemon refuses Cursor and Antigravity lines |
| `lines.ID.strategy` | strategy | `subscription` | `subscription`, `api` or `flexible` |
| `lines.ID.limits` | table | the file's | Limits for this line, each key defaulting to the file's |
| `lines.ID.wake` | table | the file's | The wake policy for this line |
| tool keys | as in `LINE_CONFIG.md` | as there | `builtin_tools`, `mcp_servers`, `tool_policy`, `allowed_tools`, `inherit_account_mcp`, `effort`, `claude_code_prompt`, `instructions`, `workdir`, `project_settings` |
| `lines.ID.may_own` | boolean | `true` | Whether the line, as a seat of a role, may own the role. A profile may set it |
| `roles.ID` | table | none | A role, named by an identifier, as [`ROLES.md`](../architecture/ROLES.md) designs it |
| `roles.ID.workdir` | relative path | required | The role's worktree, relative to the project root, with `/` between steps and no empty, `.` or `..` step, or `.` for the root |
| `roles.ID.seats` | list of 1 to 16 line identifiers | required | The role's seats, each a line this file declares, distinct, and a seat of no other role in the file. Their order is the order a failover follows |
| `roles.ID.owner` | line identifier | none | The role's first owner, a seat whose `may_own` is true. Without it the role starts vacant |
| `roles.ID.handoff_seconds` | integer in [60, 86400] | 3600 | How long a planned ownership handoff may take |
| `roles.ID.failover` | `automatic` or `ask` | `automatic` | Whether the harness passes the role to the next usable seat itself when its owner cannot act, or leaves the change to an operator, as [`ROLES.md`](../architecture/ROLES.md) specifies |
| `roles.ID.handoff_below_percent` | integer in [0, 100] | 10 | The stated budget of the owner's provider below which the owner is asked to prepare a handoff. 0 never asks |
| `lines.ID.session` | text | none | An existing engine session the line adopts when the daemon creates it, checked as `line_session` is in [`EVENT_LOG.md`](EVENT_LOG.md). Ignored for a line that exists |
| `teacher` | table | none | The project's local teacher model, as [`TEACHER.md`](TEACHER.md) specifies |
| `agents` | table | off | Which external agents may connect, and with what access, as [`AGENTS.md`](AGENTS.md) specifies |
| `federation.listen` | boolean | `false` | Whether the daemon opens its peer socket. Validated, with no effect until the peer socket is built |
| `federation.accept_unconfined` | boolean | `false` | Whether lines on an engine Dromon cannot confine may run in a project with peers, as below |
| `federation.waiting_per_recipient` | integer in [1, 100000] | 200 | The bound on mail from all peers waiting for one local party. Validated, with no effect until peer mail is built |
| `peers.ID` | table | none | Another Dromon daemon on this machine, named by an identifier, as [`FEDERATION.md`](../architecture/FEDERATION.md) designs it. At most 64 |
| `peers.ID.transport` | `unix` | `unix` | `tls` belongs to the second phase and is refused until it is built |
| `peers.ID.socket` | absolute path | required | The peer's `peers.sock`, at most 103 bytes, without `.` or `..` steps. Its directory is the peer's runtime directory |
| `peers.ID.project` | absolute path | none | The peer's project root, whose `.dromon` is then denied to local lines |
| `peers.ID.inbound`, `outbound`, `priority_in`, `priority_out` | boolean | `false` | The peer's directions and priority, as the design specifies |
| `peers.ID.reach`, `senders`, `remote` | list of at most 256 local addresses | empty | The local parties the peer may send to, the local parties that may send to it, and its parties that may be addressed, each named once. A remote address is refused |
| `peers.ID.max_body_bytes` | integer in [1, 1048576] | 65536 | The largest body sent to or accepted from the peer |
| `peers.ID.expire_hours` | integer in [1, 2160] | 168 | How long mail to the peer waits before it expires |
| `peers.ID.turns_per_hour` | integer in [1, 60] | 6 | Turns the peer's mail may start for one local line in an hour |
| `peers.ID.waiting` | integer in [1, 100000] | 500 | The bound on mail from the peer waiting for all local parties |
| `sandbox` | table | none | The project's sandbox, merged under every line's, as [`SANDBOX.md`](SANDBOX.md) specifies |
| `profiles.NAME.sandbox`, `lines.ID.sandbox` | table | none | A profile's or a line's sandbox. These merge rather than replace |
| `settings` | table | none | The project's engine settings, merged under every line's, as [`SETTINGS.md`](SETTINGS.md) specifies |
| `profiles.NAME.settings`, `lines.ID.settings` | table | none | A profile's or a line's engine settings. These merge rather than replace |

## Rules

- **Strict keys.** A key that the table above does not name is an error, so a misspelt
  key is reported rather than ignored.
- **Profiles.** A line's own key replaces the profile's key whole. Lists and tables are
  not merged, so the tools of a line can be read in one place. The `sandbox` and
  `settings` tables are the exception, and merge as their documents specify, so that no
  layer drops another's deny entries.
- **Precedence.** A `dromond` flag overrides an operator's project override, which
  overrides the file, and the file overrides the built-in defaults. A line override
  overrides the line's configuration from the file. Overrides are set from a client
  and recorded in the event log, as the settings section of
  [`CLIENT.md`](CLIENT.md) specifies, and the daemon never writes this file. User and
  managed layers are not yet specified.
- **Working directories.** Before it starts, and before a reload applies anything,
  the daemon checks every declared line's `workdir` as
  [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, and for each line whose merged sandbox
  is enabled, reads the git files [`SANDBOX.md`](SANDBOX.md) guards. A failure refuses
  the start or the reload.
- **Roles.** A seat that sets no `workdir` takes its role's, and one that sets its own
  must name the same directory once both are resolved. The role's worktree is checked
  as a line's `workdir` is, so it must exist and lie outside the directories Dromon
  protects. At start, and at a reload, the daemon records the file's roles with the
  events [`EVENT_LOG.md`](EVENT_LOG.md) specifies, after creating the seats' lines and
  setting the project's operator. A new or changed role appends `role_declared`, a new
  seat or a changed `may_own` appends `seat_bound`, and `owner` appends an
  `ownership_changed` with cause `declared`, made by the project's operator, only while
  the role has never had an owner. A restart therefore never undoes a handoff, a
  recovery or a relinquishment. The events are checked against the log before any is
  appended, so a file that moves an owned role's worktree, bars its current owner from
  owning it, or makes a seat of another role's seat is refused, and the start or the
  reload with it. The log has no event that unbinds a seat or retires a role, so one
  the file drops stays in the log as it was. Added on 2026-10-27.
- **Located errors.** Every error names the file. A syntax error carries its line and
  column. Any other error carries the dotted key path and the line of its table or key
  when it can be found. The daemon refuses to start on an invalid file.
- **Application.** At start, a declared line that does not exist is created, adopting
  its `session` when one is given, and one whose tools differ is reconfigured. A
  reconfiguration that changes the line's `workdir` also appends
  `line_session_cleared`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies. A line's model and strategy are fixed at
  creation or its last rebind, so a file that differs from the line's current binding
  is refused and the daemon does not start. After `rebind_line`, the file is updated to
  the new binding, which it then matches.
  A client may still switch a line's model within its engine, as an override. Lines
  created through the client and absent from the file are left as they are.
- **Reload.** `reload_project` applies the file again without a restart, with the same
  rules, after checking all of it, so a refused reload applies nothing. It refuses a
  change to the sandbox, the engine settings, the teacher, the external agent policy or
  the federation and peer tables, which apply only at start. Its roles are recorded as above. A line the file no longer declares returns to the
  project's limits and wake policy. The leases table is applied again, and a held lease
  whose name the file no longer declares ends.
- **Lines on other engines.** A project file whose own `[sandbox]` or `[settings]`
  table would reach a line on any engine but Claude Code is refused at start, since
  only Claude Code honours either document.
- **Restart.** The policies of limits and wake live in the daemon and are re-read on
  every start. The lines and their configurations are events in the log.

## Guards

**Decided by the operator on 2026-10-26.**

**Required sandbox.** With `daemon.require_sandbox` true, the daemon refuses any Claude
Code line whose merged sandbox, the project's `[sandbox]` with the line's own as
[`SANDBOX.md`](SANDBOX.md) merges them, is absent or not enabled. The check runs at
start, over the lines the file declares and the lines created through a client, at
`create_line`, at `configure_line`, and at reload, where it uses the reloaded file's
value, so a reload may turn the requirement on or off. A refusal names the line and
changes nothing. Codex and Grok lines run under their own permission profiles, as
[`CODEX.md`](CODEX.md) and [`GROK.md`](GROK.md) specify, and Pi lines take no built-in
tool, as [`PI.md`](PI.md) specifies, so all three satisfy it. **Why.** A
line without an enabled sandbox can reach the daemon's socket from its shell and act
with operator authority, as [`SANDBOX.md`](SANDBOX.md) records.

**Peers.** **Added on 2026-10-28, step 2 of phase 1 of
[`FEDERATION.md`](../architecture/FEDERATION.md).** Each declared peer's runtime
directory, the directory of its `socket`, and the `.dromon` of its `project` when one is
named, are protected as the project's own state and runtime directories are. Every
enabled sandbox denies them, the Codex profile denies those that exist, the check of
the Grok profiles requires them, and no `workdir` may lie in or contain one, as
[`SANDBOX.md`](SANDBOX.md) specifies. A peer whose directory is one of the project's
own, or two peers that share one, are refused. While the project declares any peer,
the daemon refuses a line on Pi, Cursor Agent or Antigravity, which Dromon cannot
confine, unless `federation.accept_unconfined` is true. The check runs where the
required sandbox's does. **Why.** A line that is not confined runs as the operator's
user and could read the peer tokens of a later step and act as a peer, and the
operator decided on 2026-10-28 that peers need the operator's explicit acceptance of
that. **Open.** Whether a Claude Code line without an enabled sandbox is refused too
awaits the operator.

**Disclosure list.** `daemon.disclosure_terms` names a file of terms that must not leave
the machine through Dromon, such as private context or code names, resolved against
the project root. Each line is one term, used as written, and blank lines and lines
whose first character other than white space is `#` are ignored, as in
`scripts/check.sh`. The daemon reads the file at start and at each reload, and at most
1 MiB of UTF-8 is accepted. A file that is missing, unreadable, too large or not UTF-8
stops the start, or refuses the reload and keeps the previous list.

- **What is checked.** A note that a line or an external agent sends with
  `send_message`, a question and its choices that a line asks with `ask_operator`, and
  a note a line schedules with `wake_after`. A text is refused when it contains any
  term as a substring, compared after Unicode case folding. A wake note is checked
  again when it fires, against the list then in force, and fires as a fixed notice if
  it now discloses, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies. **Why not refuse the
  reload.** A reload that refused while a pending note disclosed would leave a restart
  unguarded, and a restart refused for the same reason would leave the operator no way
  to start the daemon and clear the wake. Checking at firing covers both and still
  wakes the line.
- **Anonymous agents.** An external agent admitted without a token names itself, and
  the name becomes the sender of its mail. An anonymous connection whose name contains
  a listed term is refused with the same text, and nothing is recorded. This applies
  on every connection, so a name listed after the agent first connected is refused
  from then on. An agent the operator registered with `agent_add` was named by the
  operator and is exempt. Agent names hold only lowercase letters, digits, `.`, `_` and
  `-`, so a term with a space or a capital letter matches only its folded form, and a
  term with a space matches no name.
- **What a refusal says.** That the text contains a term from the project's disclosure
  list, and never which term. Nothing is recorded.
- **Not checked.** Messages from operators, who hold authority, and the questions the
  daemon asks on a held line's behalf.
- **Secrecy.** Terms are never logged, returned or stored. Errors about the file name
  the file and not its content. The settings view shows the path only.
- **Limits.** A term written another way, split across messages, or encoded passes.
  The guard catches accidents and is not a barrier against a line that tries to evade
  it. Engine transcripts, which the engines keep themselves, are outside it.

## Trust

The file is operator configuration, committed with the project. It can grant a line
tools, MCP servers, hooks and the credentials in their `env` and `headers`, which are
then stored in the event log as `LINE_CONFIG.md` describes. Like a stdio MCP server, a
hook runs a command with the daemon's privileges. The file deserves the review any such
file does. A project that holds secrets should keep them out of the committed file, and
its MCP servers can refer to them in the daemon's environment as `${NAME}`.

## Comparison with other harnesses

Reviewed on 2026-10-25 from public documentation.

- **Claude Code** layers JSON settings, managed over command line over project local
  over shared project over user, and merges lists across layers.
  Source <https://code.claude.com/docs/en/settings>.
- **Codex** reads `~/.codex/config.toml` and a project `.codex/config.toml` that applies
  only to trusted projects, with profiles, `mcp_servers` tables, `-c` overrides and a
  `requirements.toml` for organizations.
  Source <https://developers.openai.com/codex/config-reference>.
- **Grok Build** reads `~/.grok/config.toml` and a project `.grok/config.toml`, with
  managed and requirements files under `/etc/grok`.
  Source <https://docs.x.ai/build/settings>.

Dromon follows the TOML and profile convention of Codex and Grok Build. It replaces
rather than merges lists, unlike Claude Code, so that a line's grant is explicit.
