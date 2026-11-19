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

After printing the summary, `dromon config` warns when two lines that can write resolve
to the same workdir. A line can write it only when its `workdir_access` is `write`, and
then as its engine confines it. **Round 9 review, F16 and F17, decided by the
operator.**

- **Claude Code.** With a shell or file-write built-in, `Bash`, `Write`, `Edit` or
  `NotebookEdit`, or a tool policy that allows every tool.
- **Codex and Grok Build.** With `Edit` or `Write` only, since their sandbox keeps the
  workspace read-only otherwise. A shell alone cannot write there, and a policy that
  allows every tool does not change that.
- **Pi, Cursor Agent and Antigravity.** Always, since these engines cannot confine a
  line to reading its working directory and refuse `workdir_access = "read"`.
- **An engine Dromon does not know.** Always, so the warning errs toward being given.

Assign writing lines separate worktrees to avoid concurrent edits to one checkout.
**Round 9, 2026-10-31.** A line with
`workdir_access = "read"`, which its engine confines to reading, and a line whose only
tools come from Model Context Protocol (MCP) servers are no longer counted, since the
warning named read-only reviewers and a player with only its game's tools as writers.
**Assumption.** An MCP server is the operator's own program with the tools it declares,
so one that writes the working directory itself is not detected. This is a warning
only because tool names do not establish the full write capability of an engine or
remote tool.

**Round 16, at the operator's request, from another project's reports.** `dromon config`
gives two more warnings, neither of which refuses the file.

- **A read line a turn would refuse.** A Claude Code line with `workdir_access = "read"`
  and a configured MCP server, an allow-all policy or an allowed `mcp__` tool, project
  settings, plugins, or command hooks in its own settings or the project's, is refused at
  every turn, because Claude Code cannot confine those to a read-only directory. The
  refusal appeared only when a turn was tried. The warning gives the same reason, from
  `scheduler.claude_read_only_refusal`, the one function the engine builder uses, so
  the warning cannot differ from the refusal.
- **Round 17, item 7, every engine and the seats of a role.** The same warning now
  covers a line on any engine whose adapter refuses the configuration with read
  access, through `scheduler.read_only_refusal`, which asks the adapter's own
  `unsupported`. Pi, Cursor and Antigravity cannot confine a reader and refuse the
  access, and Codex gives the reasons of its configuration, such as a built-in tool it
  does not support. A seat of a role is built with read access whenever another seat
  owns the role, so each seat is judged as a reader, and a warning names the role and
  says the turns it takes while it does not own the role will be refused. A seat on an
  engine that cannot confine it, one outside the capability table's read-only set, is
  not refused and its mail waits while another seat owns the role, which the warning
  says. **Residual.** Ownership moves at run time, so a seat is judged as one that may
  not own the role. An engine the table does not name is not judged.
- **A one-slash absolute path in a permission rule.** In a Claude Code rule a path that
  begins with one slash is relative to the project, and an absolute one begins with two,
  as in `Read(//abs/path)`. A rule with one matches nothing, silently, and a `deny` rule
  that matches nothing protects nothing. `dromon config` warns of each rule of the
  project's `settings` and of each line's, for a tool that names files, `Read`, `Edit`,
  `Write`, `NotebookEdit`, `MultiEdit`, `Glob` or `Grep`, whose path starts with one
  slash and whose first segment is a directory of the filesystem root. It names the rule
  and the corrected one. `/src/**`, which names no directory of the root, is taken as
  project relative and not reported.
- **Round 17, item 6.** Two defects were confirmed against the check, and a third gap
  closed. A first segment that is a pattern, as in `Read(/*/secret)`, named no
  directory and was missed. It now names the root's directories it matches, with `*`,
  `?` and `[` read as `fnmatch` reads them, and is reported when it matches one. A
  segment with `**` is never reported, since a project writes a rule over everything
  it holds that way. A project, or a line's working directory, that has a directory by
  the name of one at the root, such as `/tmp`, and means its own, was warned of
  wrongly. A rule that the project or the line's working directory can satisfy is now
  left alone. For a pattern in the first segment that means a directory it matches, and
  when a plain path follows, that path under one of them, so that `Read(/*/todo.md)` is
  taken for the project's only if some directory holds the file. **Residual.** Where
  both the project and the root hold the name the two readings cannot be told apart, so
  the rule is left unreported, and a deny rule of that kind protects nothing of the
  root. The daemon now says the same as `dromon config`, since `configwarnings` is one
  module that both call. It prints each warning to its standard error at a start, and
  gives them as `warnings` in the answer to `reload_project`, which `dromon reload`
  prints. A warning never refuses the file.

The warning of two writing lines that share a workdir, above, already leaves out a line
that cannot write, as the operator's item of the same round asked. It did so from round
9, so the report came from a daemon older than that.

## Format

The file is TOML version 1.0, encoded in UTF-8, at most 4 MiB.

```toml
version = 1
read_only_paths = ["docs/process", "docs/roadmap"]

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

[relays.tail]
kind = "file"           # one JSON line per notice, for tail -f
path = ".dromon/run/notices.jsonl"

[relays.mail]
command = ["/usr/local/bin/notify-by-mail", "--to", "ana"]
timeout_seconds = 30    # the default, in (0, 3600]

[operators.ana]
relay = "tail"          # the route of operator ana's mail
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | required | Must be 1 |
| `daemon.operator` | identifier | `operator` | The default operator. It also declares that operator, whom a peer may reach before that operator has sent anything, as decision 25 of [`FEDERATION.md`](../architecture/FEDERATION.md) specifies. The built-in default declares no one |
| `daemon.tick_seconds` | number in [0, 60] | 1 | The maximum tick wait in seconds. Zero waits for events and due timers |
| `daemon.require_sandbox` | boolean | `false` | Whether every Claude Code line must run with an enabled sandbox, as below |
| `daemon.default_profile` | profile name | none | The profile a line created or configured through the client takes when the file does not declare it and it runs on the engine of the profile's `model`, as below. A client `config` whose sandbox sets a key that weakens the sandbox or is operator only is refused, naming the key. Added in round 18 |
| `daemon.disclosure_terms` | path | none | The project's disclosure list, relative to the project root, as below |
| `daemon.machine_status` | path | none | The file the machine's monitor writes its status to, read before a run or a turn starts, as [`RUNS.md`](../architecture/RUNS.md) specifies. A leading `~` is the home directory and a relative path is relative to the project root. Nothing is read, and nothing is deferred, unless it is set. A reload applies it. No line's sandbox or file tools may write the file or its directory. A status that is stale, missing or unreadable defers runs until the monitor writes again, so the operator restores gates by restarting the monitor or removing this key and reloading. Added on 2026-11-11 |
| `daemon.session_name` | template or `false` | `"PDr{vv}-{project}-{role}-{variant-}{model}{effort}"` | The display name each Claude Code session Dromon starts is given, or `false` for none, as below. Checked when the file is read. A reload applies it to engines built afterwards. Added in round 19 |
| `read_only_paths` | list of relative paths | empty | Existing files or directories denied writes under every line's resolved workdir. Symbolic links are resolved and targets must remain inside that workdir |
| `leases.names` | list of identifiers | none | The leases lines may acquire, each named once, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies |
| `leases.ttl_seconds` | integer in [60, 86400] | 10800 | How long an acquired lease lasts unless released first, and from round 16 the longest a line may ask for with `acquire_lease`'s `seconds` |
| `runs.commands` | table of names to arrays of text | none | The commands lines may start as supervised runs, at most 64, each name an identifier and each command its program and at most 63 arguments, never a shell string, as [`RUNS.md`](../architecture/RUNS.md) specifies. A declared command runs with the daemon's authority, outside any line's sandbox. Added on 2026-10-31 |
| `runs.commit_commands` | table of names to arrays of text | none | Commands a line, a line that only reads its working directory included, may start on a commit it names, in a checkout the daemon makes in its state directory, as the commit run section of [`RUNS.md`](../architecture/RUNS.md) specifies. Each as in `runs.commands`, and no name may be in both tables. Declaring one requires `runs.commit_refs.patterns`. Added in round 14 |
| `runs.parameters.COMMAND.NAME` | table with `pattern` text and an optional `description` | none | A parameter of the declared command COMMAND, whose argument `{NAME}` a line's `start_run` replaces with a value it gives in `arguments`. `pattern` is a regular expression of at most 256 bytes without `(?`, backreferences or possessive quantifiers, which the whole value must match, and `description` printable text of at most 256 bytes. Every placeholder of a command must be declared and every declaration used, at most eight a command. A value is also 1 to 256 printable bytes and never begins with `-`, as the parameter section of [`RUNS.md`](../architecture/RUNS.md) specifies with its trust argument. Added in the task of 2026-11-05 |
| `runs.queue_max_age_seconds` | whole number, 60 to 86,400 | 7200 | The longest a queued run waits before it is dropped, with an event and a note to its line. Two hours is eight runs of a quarter of an hour, the queue's size at the length of the full gate. Added in round 17, review of 883c00f |
| `runs.slot_lease` | identifier of a declared lease | none | The project's gate slot. A run a line starts waits in the queue while another line holds this lease, and begins when it is free, as the queued runs section of [`RUNS.md`](../architecture/RUNS.md) specifies. It must be in `leases.names`. Added in round 17 |
| `runs.commit_refs.patterns` | array of text | none | The ref patterns, 1 to 16, each beginning `refs/remotes/REMOTE/` with a remote named without wildcards, without spaces or `..`, as `git for-each-ref` reads them. A commit run's commit must be the tip of a ref matching one. A pattern under `refs/heads`, `refs/tags` or anywhere else outside `refs/remotes` is refused, since a line's sandbox may write those refs. Added in round 14, and narrowed to remote-tracking tips in its review of 1449c5c |
| `limits.*` | as in `CLIENT.md` | 12, 2, 0 | The conversation limits of every line |
| `wake.every_ticks` | integer in [1, 3600] | 1 | The wake policy of every line |
| `profiles.NAME` | table | none | A reusable line template with every line key but `profile` and `session` |
| `lines.ID` | table | none | A line, named by a line identifier |
| `lines.ID.profile` | profile name | none | The profile the line starts from |
| `lines.ID.model` | model | required | `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`, `claude-fable-5-2`, `claude-sonnet-5-5`, `gpt-6-luna`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6.2-sol` and `gpt-6.2-astra` on Codex as [`CODEX.md`](CODEX.md) specifies, `grok-4.7` and `grok-4.7-build-fast` on Grok Build as [`GROK.md`](GROK.md) specifies, `pi:openai/gpt-6-luna`, `pi:openai/gpt-6-astra`, `pi:openai/gpt-6-sol`, `pi:openai/gpt-6.1-sol` and `pi:openai/gpt-6.1-terra` and `pi:google/gemini-4-argon` on Pi as [`PI.md`](PI.md) specifies, `cursor-agent:gpt-5.6-luna-medium`, `cursor-agent:gemini-4-argon-medium`, `cursor-agent:gemini-4-carbon-medium` and `cursor-agent:gemini-4-hydrogen-medium` on Cursor Agent as [`CURSOR.md`](CURSOR.md) specifies, or `antigravity:gemini-3.8-flash-high` on Antigravity as [`ANTIGRAVITY.md`](ANTIGRAVITY.md) specifies. A daemon refuses Cursor and Antigravity lines |
| `lines.ID.strategy` | strategy | `subscription` | `subscription`, `api` or `flexible` |
| `lines.ID.limits` | table | the file's | Limits for this line, each key defaulting to the file's |
| `lines.ID.wake` | table | the file's | The wake policy for this line |
| tool keys | as in `LINE_CONFIG.md` | as there | `builtin_tools`, `mcp_servers`, `tool_policy`, `allowed_tools`, `inherit_account_mcp`, `effort`, `claude_code_prompt`, `instructions`, `workdir`, `workdir_access`, `project_settings` |
| `lines.ID.secrets` | list of environment variable names | none | Names this project grants that line to reference in its MCP configuration. Values are read from the daemon environment only for that line's MCP configuration and are never added to its engine process environment. A profile may grant names to its lines. |
| `lines.ID.may_own` | boolean | `true` | Whether the line, as a seat of a role, may own the role. A profile may set it |
| `lines.ID.git_ssh` | boolean | `false` | Whether the line's shell commands get the project's `[git_ssh]` agent, as below. A profile may set it. True without a `[git_ssh]` table is an error |
| `git_ssh` | table | off | One Secure Shell (SSH) agent socket and pinned host keys for the lines that set `git_ssh`, as below |
| `git_ssh.agent` | `inherit` or absolute path | required | `inherit` forwards the daemon's own `SSH_AUTH_SOCK`. A path names a socket, such as that of a separate agent holding only a deploy key |
| `git_ssh.known_hosts` | path | required | A `known_hosts` file, absolute or relative to the project root, that holds a key for `github.com` |
| `git_ssh.public_key` | path | required | The public key, absolute or relative to the project root, whose private half the agent holds, and the only key ssh offers |
| `roles.ID` | table | none | A role, named by an identifier, as [`ROLES.md`](../architecture/ROLES.md) designs it |
| `roles.ID.workdir` | relative path | required | The role's worktree, relative to the project root, with `/` between steps and no empty, `.` or `..` step, or `.` for the root |
| `roles.ID.seats` | list of 1 to 16 line identifiers | required | The role's seats, each a line this file declares, distinct, and a seat of no other role in the file. Their order is the order a failover follows |
| `roles.ID.owner` | line identifier | none | The role's first owner, a seat whose `may_own` is true. Without it the role starts vacant |
| `roles.ID.handoff_seconds` | integer in [60, 86400] | 3600 | How long a planned ownership handoff may take |
| `roles.ID.failover` | `automatic` or `ask` | `automatic` | Whether the harness passes the role to the next usable seat itself when its owner cannot act, or leaves the change to an operator, as [`ROLES.md`](../architecture/ROLES.md) specifies |
| `roles.ID.handoff_below_percent` | integer in [0, 100] | 10 | The stated budget of the owner's provider below which the owner is asked to prepare a handoff. 0 never asks |
| `roles.ID.reviews` | list of at most 16 role identifiers | empty | The roles whose work this role reviews, distinct and never the role itself. Each must be a role this file declares or one the log holds, which is checked when the file's roles are planned, so a file naming any other is refused whole at a start or a reload. It refuses no review. `role_status` and the reviewing owner's turns say when the two owners run the same engine and model, as [`ROLES.md`](../architecture/ROLES.md) gives. Added in round 15, item 5 |
| `lines.ID.session` | text | none | An existing engine session the line adopts when the daemon creates it, checked as `line_session` is in [`EVENT_LOG.md`](EVENT_LOG.md). Ignored for a line that exists |
| `teacher` | table | none | The project's local teacher model, as [`TEACHER.md`](TEACHER.md) specifies |
| `agents` | table | off | Which external agents may connect, and with what access, as [`AGENTS.md`](AGENTS.md) specifies |
| `federation.listen` | boolean | `false` | Whether the daemon opens its peer socket. Validated, with no effect until the peer socket is built |
| `federation.tls_listen` | `HOST:PORT` text | none | The address of the daemon's Transport Layer Security listener, a host name or an address with a version 6 address in brackets, and a port in [1, 65535]. Required exactly when a `tls` peer is inbound or `remote` is open, so no network listener is configured unless a peer or the open mode needs one. Every declared peer may be an inbound `tls` peer, since the transport's bound of 64 certificates one listener trusts is the bound on declared peers. Two identities made before certificates named their own key share one subject name, so one of them must be made again with `dromon peer key` before one listener trusts both. A listener that cannot start is said on standard error, and the rest of the daemon still serves |
| `federation.accept_unconfined` | boolean | `false` | Whether lines on an engine Dromon cannot confine may run in a project with peers, as below |
| `federation.waiting_per_recipient` | integer in [1, 100000] | 200 | The bound on mail from all peers waiting for one local party. Validated, with no effect until peer mail is built |
| `federation.local` | `allowlist` or `open` | `allowlist` | Whether projects of this machine reach this one only as declared peers, or also through the per-user registry, as [`FEDERATION.md`](../architecture/FEDERATION.md) specifies under open federation. `open` needs `listen` and `name`. Added on 2026-10-31. **Rollout hazard.** A daemon before this change refuses the key |
| `federation.remote` | `allowlist` or `open` | `allowlist` | Whether the TLS listener also takes a key no declared peer pins, inbound only, which needs `tls_listen` set explicitly. The open listener uses pyOpenSSL and accepts only a self-signed Ed25519 key over TLS 1.3, as FEDERATION.md records. A daemon with `open` and no identity of its own listens on nothing over TLS. Added on 2026-10-31 |
| `federation.name` | identifier | none | The name this daemon registers under when `local` is open, unique among live entries, and refused when it is 64 hexadecimal digits, a declared peer's name, or on the disclosure list. Required when `local` is open |
| `federation.allowed_networks` | list of 1 to 64 address ranges | every address | The ranges, such as `192.0.2.0/24` or `2001:db8::/32`, written with no host bits set, from which the TLS listener accepts a connection, checked by the connecting address before the connection counts under the cap and before its handshake. Needs `tls_listen`. Required while `remote` is open, so a listener admits every address only when the file lists `"0.0.0.0/0"` and `"::/0"`, decided by the operator on 2026-10-31 |
| `federation.open.reach`, `.senders` | lists of local addresses | empty | As `peers.ID.reach` and `.senders`, for every open peer on this machine that the file does not declare. Empty lists deliver nothing |
| `federation.open.remote` | list of `NAME:PARTY` texts | empty | The parties of open peers local senders may address, with `NAME` a federation name or `*` for every open peer, and `PARTY` always named, so a line cannot guess at another project's parties |
| `federation.open.priority_in`, `.priority_out`, `.max_body_bytes`, `.expire_hours`, `.turns_per_hour`, `.waiting` | as for `peers.ID` | as for `peers.ID` | The limits of every open peer on this machine |
| `federation.open.remote_reach` | list of local addresses | empty | The local parties an undeclared key on another machine may send to. Such a key is inbound only and never priority |
| `federation.open.remote_max_body_bytes`, `.remote_turns_per_hour`, `.remote_waiting` | integers | 16384, 2, 50 | The stricter limits of an undeclared key on another machine |
| `federation.open.remote_total_turns_per_hour`, `.remote_total_waiting` | integers | 6, 500 | The turns for one local line in any hour, and the waiting notes, that all undeclared keys share, besides each key's own. One declared peer's defaults. At most 64 undeclared keys are recorded, and a new key past that is refused |
| `federation.open.remote_total_deliveries` | integer | 60 | The `deliver` requests all undeclared keys together may make in ten minutes, whatever their answers, besides each key's own 60. One declared peer's rate. From 1 to 3,840, which is 64 keys at their own rate |
| `peers.ID` | table | none | Another Dromon daemon, on this machine or on another, named by an identifier, as [`FEDERATION.md`](../architecture/FEDERATION.md) designs it. At most 64 |
| `peers.ID.transport` | `unix` or `tls` | `unix` | A `unix` peer is on this machine. A `tls` peer is reached over Transport Layer Security, the second phase. Mail to it passes the same policy as mail to a `unix` peer. A key of the other transport is an error |
| `peers.ID.socket` | absolute path | required for `unix` | The peer's `peers.sock`, at most 103 bytes, without `.` or `..` steps. Its directory is the peer's runtime directory |
| `peers.ID.project` | absolute path | none | A `unix` peer's project root, whose `.dromon` is then denied to local lines |
| `peers.ID.address` | `HOST:PORT` text | required for `tls` | The peer's listener, written as `federation.tls_listen` is |
| `peers.ID.pin_sha256` | 64 lowercase hexadecimal digits | required for `tls` | The SHA-256 of the peer's public key, which its operator reads from `dromon peer key`. No two peers share one |
| `peers.ID.certificate` | relative path | required for `tls` | The peer's public certificate, relative to `.dromon`, at most 1024 bytes, without empty, `.` or `..` steps |
| `peers.ID.inbound`, `outbound`, `priority_in`, `priority_out` | boolean | `false` | The peer's directions and priority, as the design specifies |
| `peers.ID.reach`, `senders`, `remote` | list of at most 256 local addresses | empty | The local parties the peer may send to, the local parties that may send to it, and its parties that may be addressed, each named once. A remote address is refused |
| `peers.ID.max_body_bytes` | integer in [1, 1048576] | 65536 | The largest body sent to or accepted from the peer |
| `peers.ID.expire_hours` | integer in [1, 2160] | 168 | How long mail to the peer waits before it expires |
| `peers.ID.turns_per_hour` | integer in [1, 60] | 6 | Turns the peer's mail may start for one local line in an hour |
| `peers.ID.waiting` | integer in [1, 100000] | 500 | The bound on mail from the peer waiting for all local parties |
| `sandbox` | table | none | The project's sandbox, merged under every line's, as [`SANDBOX.md`](SANDBOX.md) specifies |
| `profiles.NAME.sandbox`, `lines.ID.sandbox` | table | none | A profile's or a line's sandbox. These merge rather than replace |
| `relays.NAME` | table | none | A relay to operators, named by an identifier, as [`RELAY.md`](RELAY.md) specifies. At most 32. Added on 2026-10-31 |
| `relays.NAME.kind` | `command` or `file` | `command` | A command relay runs a command once per notice. A file relay appends each notice to one file |
| `relays.NAME.command` | list of 1 to 64 non-empty texts | required for `command` | The argument vector, run without a shell, each argument at most 4096 bytes. A key of the other kind is an error |
| `relays.NAME.path` | path | required for `file` | The notice file, absolute, under `~`, or relative to the project root. Its directory must exist, and it may not lie in any line's working directory except inside a directory Dromon protects, as below |
| `relays.NAME.timeout_seconds` | number in (0, 3600] | 30 | How long a command relay may run before it and its process group are killed |
| `operators.ID` | table | none | An operator, named by an operator identifier, and declared, so that a peer may reach it as `daemon.operator` may be reached |
| `operators.ID.relay` | relay name | required | The relay that carries the operator's mail. An operator with no table is unrouted, and its mail waits for a client |
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
  reload with it. The file never unbinds a seat or retires a role, so one the file
  drops stays in the log as it was. Added on 2026-10-27.
- **Roles changed at run time.** **Round 10, 2026-10-31.** An operator's `dromon role
  create`, `update` or `delete` wins over the file, and the file never undoes it
  silently. A role a command changed is held by the operator, which `role_held`
  records, and a start or a reload leaves it as it is while the file's table for it
  differs, with a notice naming each term and seat that differs. Once the table agrees,
  the role is held by the file again, with a notice, and later changes to the file
  apply. A retired role's table is never applied, since a role identifier is not
  reused, and a role the file dropped stays, with a notice to retire it with `dromon
  role delete`. A role created by a command and absent from the file gets no notice. A
  seat the file names that an operator bound to a role the operator holds is left
  out, with a notice, and the rest of the file applies. A reload returns the notices
  as `role_notices`, and a start prints them. The cases are tabulated in
  [`ROLES.md`](../architecture/ROLES.md). A role that returns to the file is given the
  file's `owner` in the same pass, where it never had an owner, and `dromon role
  status` shows each difference of a role an operator holds.
- **The one-writer rule.** **Round 10 review, 2026-10-31.** A file is refused whole, at
  a start and at a reload, if the state it would make has two roles on one worktree,
  compared by real path, which the refusal names, or an owning seat outside its role's
  worktree, or a line that is not a seat of a role and could write that role's
  worktree while another seat owns it, which the refusal names with the reason. The
  check reads the lines only in the log as well, with their overrides, so a line that
  a log made before the rule refuses the file until it is moved, made read only or
  made a seat. The rule is stated in [`ROLES.md`](../architecture/ROLES.md).
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
  created through the client and absent from the file are left as they are, and
  `dromon status` names each as not in the project file. From round 14 an operator
  may retire such a line with `dromon retire`, and a file that declares a retired line
  is refused, since a line identifier is never used again.
- **Reload.** `reload_project` applies the file again without a restart, with the same
  rules, after checking all of it, so a refused reload applies nothing. It refuses a
  change to the sandbox, the engine settings, the teacher, the external agent policy,
  the federation and peer tables, the `[git_ssh]` table or the lines that set
  `git_ssh`, which apply only at start. Its roles are recorded as above. A line the file no longer declares returns to the
  project's limits and wake policy. The leases table is applied again, and a held lease
  whose name the file no longer declares ends, as it also does at start. The runs table
  is applied again, and a run already started is unaffected.
- **Relays.** At start, and before a reload applies anything, the daemon builds each
  routed operator's relay, and refuses the start or the reload when a file relay's
  notice file lies in the working directory of any line, whether in the log or in the
  file, outside the directories Dromon protects. A reload may add, change or remove
  relays and routes, and they apply from the relayer's next pass. A notice already
  handed to its old relay finishes there. **Decided by core for review, on
  2026-10-31.** A notice file under `.dromon`, such as `.dromon/run/notices.jsonl`,
  is allowed although `.dromon` lies in the project root, which is a line's working
  directory, because every enabled sandbox denies `.dromon` to lines and no line may
  run in it. A line without a sandbox reaches the file wherever it is.
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
with operator authority, as [`SANDBOX.md`](SANDBOX.md) records. **Round 18 follow-up.**
A Claude Code line that no layer enables or disables is given an enabled sandbox at
its build, so this check and the peers' check below count it as sandboxed. Only an
explicit `enabled` false fails them.

**Default profile, round 18, last pass, item 6.** A line created through a client,
or by an agent through the client's operations, names no profile, so before this key it
took none of the denials and ask rules a profile gives the lines the file declares.
With `daemon.default_profile` naming a profile, `create_line` builds such a line's
configuration from that profile under the client's `config`, when the line's model runs
on the same engine as the profile's `model`. The profile's tool keys are replaced whole
by any key the client sends, as a line's own key replaces its profile's in the file, and
its `sandbox` and `settings` merge under the client's, the client's being the more
specific layer, so a scalar both set takes the client's value and the lists of both
are united. **The guarantee, round 18, addendum 2, item 1.** What a client cannot
remove is every list entry the profile adds, which are its settings rules, such as
`ask` and `deny`, and its sandbox lists, such as `filesystem.denyWrite`, together
with the sandbox that carries them. A client document that sets `sandbox.enabled`
false, `sandbox.failIfUnavailable` false or `sandbox.filesystem.disabled` true is
refused with an error naming the key, unless the profile itself sets that value,
since each would switch off or make optional the sandbox that carries the profile's
denials. An unset key counts as its secure value, which the daemon fills in for a
Claude Code line. The refusal fails closed rather than letting the profile's value
win silently, so the client learns that its document was not taken as sent. **Round
18, addendum 3.** The same refusal covers every other sandbox key that weakens the
sandbox or that this file may not set, listed once as `HELD_SANDBOX_KEYS` in
`dromon.projectconfig`. A client document is refused when it sets
`allowAppleEvents`, `allowNestedUserNamespaces`, `network.allowAllUnixSockets`,
`enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation` or
`network.allowLocalBinding` true, or `network.strictAllowlist` false, and when it
sets `network.tlsTerminate`, `network.httpProxyPort`, `network.socksProxyPort`,
`network.allowUnixSockets` or `network.allowMachLookup` at all, since a proxy port, a
Unix socket or a Mach service is a way out of the sandbox rather than a grant inside
it. It is also refused when it sets `allowUnsandboxedCommands` true or any
`excludedCommands`, although the daemon would neutralise both when it builds the
engine's settings, so that the client sees a refusal rather than a document silently
not taken. A key the profile sets itself may be given the profile's own value. **What
a client may still change.** It may replace any tool key, such as `builtin_tools`, add
grants inside the sandbox, which are `filesystem.allowWrite`, `filesystem.allowRead`
and `network.allowedDomains`, refused only where they reach a credential path or a
directory Dromon protects, add denials, which are `filesystem.denyWrite`,
`filesystem.denyRead`, `network.deniedDomains` and the `credentials` lists, and set
`autoAllowBashIfSandboxed` and `ignoreViolations`. A test fails when the sandbox parser
learns a key that is placed on neither side. An `allow` settings rule the client adds
does not remove the profile's `ask` or `deny` rules, and that Claude Code applies a
`deny` before an `allow` is an inference from its documentation that was not tested
here. **Residuals.** The guarantee is against a client or an agent with manage
access weakening the profile's sandbox, and not against every widening of a line,
which the operator at a client may make by design within the grants above. That
`ignoreViolations` only quietens reports and opens nothing is an inference from Claude
Code's documentation, not tested. Whether an `allowRead` entry lifts a profile's
`denyRead` entry beneath it was not tested either, and the profile this project names
denies writes and not reads. `configure_line` does the same for a line the file
does not declare, so a later configuration cannot drop them either. A line the file
declares takes the file's definition, as before. A line on another engine is created
as the client sent it, so a profile for Claude Code never reaches a Codex, Grok, Pi,
Cursor or Antigravity line. The file is refused at load when the key names no profile,
a profile with no `model`, or a profile that grants `secrets`, since a client-created
line is granted none, and at start when the profile's own engine could not run it, as
an Antigravity profile cannot, since a daemon refuses every Antigravity line. The
profile's `git_ssh`, `limits`, `wake` and `may_own` are not applied, since each is
authority or policy the file gives the lines it names, and `dromon config` and
`dromond` warn when the profile sets `git_ssh`. **Lines that already exist.** The key
changes nothing in the event log, so a line created before it was named keeps its
configuration until it is configured again. `dromon configure LINE` with the options
the line was created with applies the profile from the line's next turn. The command
replaces the line's whole configuration, so options such as `--workdir` must be given
again. **Residual.** `rebind_line` moving a line onto Claude Code from another engine
does not apply the profile, and a line so moved takes it at its next configure.

**Peers.** **Added on 2026-10-28, step 2 of phase 1 of
[`FEDERATION.md`](../architecture/FEDERATION.md).** Each declared peer's runtime
directory, the directory of its `socket`, and the `.dromon` of its `project` when one is
named, are protected as the project's own state and runtime directories are. Every
enabled sandbox denies them, the Codex profile denies those that exist, the check of
the Grok profiles requires them, and no `workdir` may lie in or contain one, as
[`SANDBOX.md`](SANDBOX.md) specifies. A peer whose directory is one of the project's
own, or two peers that share one, are refused. **Open federation, added on
2026-10-31.** While `local` is open, the per-user registry joins the protected
directories, and so do the runtime and state directories of every project it holds,
read again at each engine build, so a project registered after the daemon started is
denied from each line's next turn. While the project declares any peer, or either
mode is open, the daemon refuses a line on Pi, Cursor Agent or Antigravity, which Dromon cannot
confine, unless `federation.accept_unconfined` is true. The check runs where the
required sandbox's does. **Why.** A line that is not confined runs as the operator's
user and could read the peer tokens of a later step and act as a peer, and the
operator decided on 2026-10-28 that peers need the operator's explicit acceptance of
that. **Added on 2026-10-28, step 3.** A Claude Code line without an enabled sandbox is
refused too, by the operator's decision of that day. At start the daemon records each
declared peer with `peer_declared`, and a peer the file no longer declares with
`peer_removed`, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies, and refuses to start when
`.dromon/peers`, the token store, is wider than mode 0700 or holds anything but files
of mode 0600 of the operator's user.

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
  a listed term is refused naming its `name` field, and nothing is recorded. This
  applies on every connection, so a name listed after the agent first connected is refused
  from then on. An agent the operator registered with `agent_add` was named by the
  operator and is exempt. Agent names hold only lowercase letters, digits, `.`, `_` and
  `-`, so a term with a space or a capital letter matches only its folded form, and a
  term with a space matches no name.
- **What a refusal says.** A tool refusal names at most one field, such as `body`
  or `note`, only when the call offered text in that one field. A call offering
  several fields receives a generic refusal even when only one matches. The round 4
  review F1 rule prevents learning more than one bit from each counted refusal.
  **Task of 2026-11-05, item 1, the operator's decision.** `set_brief` is the exception. Its
  refusal names the one field among `task`, `decisions`, `next_action` and
  `artifacts` that matched, so a line knows what to reword, at a cost of at most two
  bits a refusal. Two or more matching fields still get the generic refusal, so no
  refusal names more than one field, and none names a term, a line or a position,
  which the operator declined.
- **Not checked.** Messages from operators, who hold authority, and the questions the
  daemon asks on a held line's behalf.
- **Secrecy.** Terms are never logged, returned or stored. Errors about the file name
  the file and not its content. The settings view shows the path only.
- **Limits.** A term written another way, split across messages, or encoded passes.
  The guard catches accidents and is not a barrier against a line that tries to evade
  it. Engine transcripts, which the engines keep themselves, are outside it.
- **A file already checked costs nothing more.** **Round 15, item 4.** A line is told
  to check a draft before it writes it and to check the file it wrote. Checking the
  draft as `text` and then the file spent two checks of the ten. The daemon now
  remembers the SHA-256 of every text that passed and of every text `write_scratch`
  wrote, per line, and a later `check_disclosure` of a file whose exact bytes have that
  digest answers `pass`, says it spent no check, records nothing and is answered even
  at the limit, since it tells the line nothing it did not learn when the bytes passed.
  A list of files costs nothing only when every file is remembered, and otherwise costs
  one check and remembers each file that passed. Any edit, even of one byte, is a
  different digest. A failed check is never remembered. A file that passed only
  through an operator's accepted line is remembered for its own path alone. **Limits.**
  The memory is in the daemon's process, holds the 64 most recent passes of a line,
  and is emptied by a restart and when the list is replaced, so the next check then
  spends. **Round 15 review, G1.** It is also emptied for a line whose effective
  configuration differs from the one the passes were made under, such as another
  working directory, because a pass is bound to a path relative to that directory and
  the operator's accepted lines to the project path it resolves to. Any change of the
  configuration forgets the line's passes, which costs at most one more check, and the
  same configuration set again keeps them. A text check is never answered from memory. A held line is refused first.
- **Checking before sending.** **Item B2 of 2026-10-28.** A line's `check_disclosure`
  tool checks exactly one of `text`, a `path`, or a `paths` list of one to 64 files
  relative to its working directory (**round 17, item 5**, the limit was eight, and is now the 64 passes a line remembers), and answers `pass` or `fail` with how many lines
  hold a listed term, compared as above, and never which term matched. The answer
  never names a matching file or line location, including when several
  files were supplied. File read failures also omit file names.
  Each file is checked independently and the counts are summed, so a match cannot span
  a file boundary. **Round 14, item 3.** A file is checked in parts of at most 1 MiB,
  each cut after a newline so that no line is split and no term straddles a cut, and
  the counts are summed, so the file costs one check and the sum equals the count for
  the whole. The combined size of the files may not exceed 16 MiB, and a line longer
  than 1 MiB is refused, since a cut there could split a term. A text is still at most
  1 MiB. A `path` may be absolute when it lies under the line's working directory, as
  `write_scratch` reports the file it wrote, so a line checks the file it wrote and
  not a draft. It is normalized lexically and then walked without following a link. A
  file that cannot be
  read refuses the whole call without recording a check. Each successful call,
  including a list, is recorded as one
  `disclosure_checked` event, without the text or its name, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies. **Proposed.** A line may
  have at most 10 failing checks in any 3,600 seconds, counted from the recorded times on the
  wall clock, so the limit survives a restart. Every answer says whether a text holds
  a term, so an unlimited check would let a line test candidate words one by one.
  **Task of 2026-11-05, item 1, the operator's decision.** A check that passes spends
  nothing of the failure limit, and the answer says which happened. **Round 15 review,
  D1, the operator's decision.** Free passes let a line try about 130 times more
  candidates an hour, so a short term fell to a few failures. So a line also has at
  most 60 passing checks in the same window, and its checks may read at most 64 MiB in
  all, passes and failures together. The failure limit bounds the terms a line can
  confirm, the pass limit the candidate texts it can try, and the byte ceiling the
  candidate bytes it can test and the daemon's time spent matching for it. 64 MiB is
  less than the 160 MiB that ten checks of 16 MiB allowed before passes were free.
  Once either count is spent, every check is refused unread, a would-be pass or
  failure too, since answering only one kind would tell a line which texts are the
  other. A text or file whose size would pass the bytes left is refused by its size
  before it is read, and records nothing. **Round 15 review, D3.** A check reads and
  matches on a worker thread, so it does not hold the daemon's event loop, and one
  check runs at a time for each line. A tool's refusal may spend the last failure
  while a check reads, so the limits are read again before the check is recorded, and
  the check is then refused unanswered. **Round 15, item 4, merged with D1.** A check
  of files whose exact bytes all already passed, as a checked text, as a checked file
  of the same path, or as text `write_scratch` screened and wrote, is answered as a pass
  from the daemon's memory of passes and spends nothing of any budget and records
  nothing, even once a budget is spent, since it tells the line nothing it did not
  learn when the bytes passed. A text check is never answered from memory. Once a
  budget is spent, a file is read for the comparison only when its size equals that
  of a remembered pass, and is never matched, so other content is still refused
  unread. The memory holds 64 passes of a line in the daemon's process, and is
  emptied by a restart, a replaced list and a changed configuration of the line. A
  check whose list or configuration changed while it ran is refused when it would
  have been answered from memory, and its pass is otherwise not remembered.
  **Round 15 review, C1.** A file the daemon writes without screening, as
  `scratch_place` writes an operator's CI log, is never remembered as passed, so a
  listed term in it is still found. **Round 15 re-review.** A `write_scratch` call
  that the list screens is a check too, since the audit wrote 100 clean texts of
  256 KiB in two seconds and spent nothing. Its text and path spend one pass when
  they pass, or one failure when refused, and their bytes in both cases. Once either
  count is spent, or when they would pass the bytes left, the write is refused
  before it is screened or written, without holding the line, as a spent
  `check_disclosure` is. A screened write's pass is recorded before the file is
  written, since a write refused afterwards still tells the line that the text
  passed, and it counts even when its text is remembered, since the path is screened
  too. **Residual risk.** A text that passes the screen of another tool, such as a
  note or a brief, is still not counted, as it never was, so a line can still test
  candidates through those tools at their sizes, which are far below a scratch
  file's, and the guard remains against accidents, as the limits above say. A
  file is read as `write_scratch` writes, walked without following a link, and only
  when it is a regular file of the daemon's user with one name, outside every
  directory Dromon protects, and at most 16 MiB combined. A refusal, of a missing list, a limit
  reached, or a text or file not read, records nothing and reveals nothing of the
  list. **Decided by the operator on 2026-10-28.** A refusal for disclosure by
  `send_message`, `ask_operator`, `wake_after` or `write_scratch` identifies the
  offending field without naming its value, so it counts against the same limit and
  is recorded as `disclosure_checked`
  too. Dromon applies this to every line tool that refuses for disclosure, which
  adds `record_role_note`, `relinquish_ownership`, `take_ownership` and
  `propose_change`. A text that passes is not counted. When a refusal finds the
  limit spent, Dromon holds the line and asks an operator instead of answering, and
  every one of these tools and `check_disclosure` then gives one fixed answer,
  without checking, until an operator releases the line. A release does not empty
  the window, so a further refusal within it holds the line again. A line already
  held for another reason is not asked about twice, and the release of that hold
  ends this one too. `check_disclosure` itself, when its limit is reached, still
  refuses with the time of its next check and does not hold the line, since that
  refusal reveals nothing of the list. **Round 15 review, R1.** A run's output
  summary and tail are screened the same way before its end is recorded, and
  withheld whole when either holds a term, which counts as a failing check of the line
  that started the run, as [`RUNS.md`](../architecture/RUNS.md) specifies. **Fact.** Each spent limit still yields one
  further answer, since the held reply differs from a sent one, so a line learns at
  most 11 answers per release.

## Git over SSH

**Required by the operator on 2026-10-29.** A line's engine starts with an allowlisted
environment and a home directory of its own, so the operator's `SSH_AUTH_SOCK`,
`~/.ssh/config` and `known_hosts` never reach its shell commands, and an enabled
sandbox denies the agent's socket. The `[git_ssh]` table lets the operator give
chosen lines git over SSH. It is off by default, and only the project file sets it,
so neither a client nor a line can turn it on.

```toml
[git_ssh]
agent = "inherit"                  # or the absolute path of an agent socket
known_hosts = "ops/known_hosts"    # holds GitHub's published host keys
public_key = "ops/deploy.pub"      # the key the agent holds, public half only

[lines.alpha]
model = "claude-opus-5-5"
git_ssh = true
```

- **What is forwarded.** Only an agent socket, never a key file and never the
  operator's home directory. The line's shell commands get one variable,
  `GIT_SSH_COMMAND`, and not `SSH_AUTH_SOCK`, so git uses the agent and other
  programs are not pointed at it. The command is `ssh -F /dev/null` with
  `IdentityAgent` set to the socket, `IdentityFile` set to the public key,
  `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile` set to the
  named file, `GlobalKnownHostsFile=/dev/null`, `UpdateHostKeys=no` and
  `BatchMode=yes`. Each path is double quoted for ssh and the whole is quoted for the
  shell git runs it with, so a space in a path is safe.
- **Why a public key is required.** **Inference from the `ssh_config` manual page,
  not tested here.** `IdentitiesOnly` offers only the agent keys that match a
  configured identity file. A line's home holds none of the default files, so
  without `public_key` ssh would offer no key at all.
- **Checks at start.** The daemon checks the table whenever the file has one, even
  when no line sets `git_ssh` yet, and refuses to start on a failure. `dromon config`
  runs the same checks against its own environment, which may differ from the
  daemon's, and shows the socket, the files and the lines given them. Refused are an
  `inherit` agent when the daemon's environment has no `SSH_AUTH_SOCK`, a socket path
  that does not exist, is not a socket, belongs to another user or is longer than 103
  bytes, a socket in a directory that belongs to neither the operator nor root or
  that its group or others may write, a `known_hosts` that is missing, is not UTF-8
  or holds no plain entry with a well-formed key for `github.com`, and a public key
  file that holds a private key or anything but one well-formed public key line.
  Another user who may write the socket's directory could put a socket of their own
  in its place after the check. A sticky directory such as `/tmp` is refused as
  well, so that the rule stays one rule, and `ssh-agent` and the macOS launchd agent
  both make a private directory for their socket. Each file must be a regular file
  of the operator's user that neither its group nor others may write. A wildcard, a
  line whose host list holds any negated pattern, and a line marked `@revoked` or
  `@cert-authority` pin nothing, and a hashed name counts when it hashes
  `github.com`. A negated pattern voids its whole line, as `ssh-keygen -F` reports
  for `!github.com,github.com`, which errs toward refusing.
- **Well-formed keys.** A key's base64 must decode to the SSH wire form, a
  length-prefixed type string followed by the key data, whose type string equals the
  key type the line names and whose data has that type's shape with nothing after
  it. The types accepted are `ssh-ed25519`, `sk-ssh-ed25519@openssh.com`,
  `ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`,
  `sk-ecdsa-sha2-nistp256@openssh.com` and `ssh-rsa`. **Inference from the
  `ssh_config` manual page's default `HostKeyAlgorithms`.** The certificate types
  are left out, since they pin a host only under `@cert-authority`, and so is
  `ssh-dss`, which OpenSSH no longer offers by default. The check proves that a
  well-formed key is pinned, and never that the key is GitHub's. The operator must
  compare the fingerprints, as `ssh-keygen -lf` prints them, with the ones GitHub
  publishes before starting the daemon. Any of the three inside a directory Dromon protects is refused,
  since an enabled sandbox denies it. **Round 18.** For a Claude Code line with an
  enabled sandbox that includes the known credential paths under the daemon's home,
  such as `~/.ssh`, so an agent socket or either file kept there is refused, and is
  kept elsewhere, as [`SANDBOX.md`](SANDBOX.md) gives. **Round 18 follow-up.** A
  Claude Code line that no layer enables or disables is given an enabled sandbox, so
  this refusal now reaches it too, and `dromond` reaches it at start. No path may hold a control character, a quote, a
  backslash, `$` or `%`, which ssh would expand or read as part of its option. No
  host key is written into Dromon's code.
- **Sandbox.** For a Claude Code line whose merged sandbox is enabled, Dromon merges
  one further layer last, which adds the socket to `network.allowUnixSockets`,
  `github.com` to `network.allowedDomains`, and both files to `filesystem.denyWrite`,
  so a command cannot add a host key. The protected directories are denied after it,
  as [`SANDBOX.md`](SANDBOX.md) specifies, so it lifts none of them. A line whose
  sandbox denies reading either file is refused, since ssh inside it could not read
  them. A line whose sandbox is absent or disabled gets the variable alone.
- **Engines.** One function, `dromon.gitssh.environment`, gives the variable to every
  adapter. Claude Code, Grok, Pi, Cursor Agent and Antigravity receive it in their
  engine's environment, and Claude Code's launcher passes only Dromon's value, never
  one in the daemon's own environment. **Fact, read from the pinned Agent SDK
  0.2.174.** Its subprocess transport builds the engine's environment from the
  daemon's own, then its entry point variable, then `options.env`, so `options.env`
  wins. The adapter sets the relayed name in `options.env` for every line, empty for
  a line without the option, so a value of that name in the daemon's environment
  never wins, and the launcher's filter drops an empty one. Codex receives it through
  `shell_environment_policy.set`, as the line's process mark does, so it reaches the
  commands Codex runs and not Codex itself. Pi lines take no built-in tool, so a Pi
  line has no shell command to use it.
- **Changes between turns.** The daemon records the SHA-256 digest of both files as
  it checks them at start. Before each turn of a line given the option, before its
  engine is built or a prompt sent, it reads both files again. If either differs
  from its digest, or is no longer a regular file of the operator's that no one else
  may write, the turn is refused, the line is held, and an operator is asked, in a
  question that names the file and never its contents. Answering resumes the line,
  and its next turn is checked again, so the operator restores the file first or
  restarts the daemon to accept it. A line whose sandbox is disabled runs as the
  operator's user and can do anything that user can, including changing either
  file between two checks and restoring it before the next. The digest check
  therefore detects tampering at a turn's start and does not prevent it. Keep both
  files outside every line's writable directories, such as its worktree, its home
  and any directory its sandbox allows writing.
- **Restart.** The socket is resolved once, at start. A reload that changes the table
  or the lines that set `git_ssh` is refused, and an agent restarted on another socket
  needs a daemon restart.
- **Threat model.** A command that can reach the socket can ask the agent to sign
  with every key the agent holds, for every host and repository those keys reach. It
  can also run ssh with options of its own, so `GIT_SSH_COMMAND` narrows what git does
  and not what a line can do, and `IdentitiesOnly` does not confine a line. An agent
  that holds only a deploy key narrows the reach to that one repository and its
  permission, and is the recommended form. The option grants a capability and lifts
  no rule, so a project's own rules that forbid lines to push, as pydromon's line rules
  do, still apply.
- **Observed on 2026-10-29, inside an enabled Claude Code sandbox.** The test ran in
  the operator's own Claude Code session, whose sandbox is configured like, but not
  identically to, a Dromon line's. There `ssh` could not resolve `github.com` or
  `ssh.github.com`, since the sandbox allows no direct connection, and a tunnel through
  the sandbox's HTTP proxy with `ncat` was refused for port 22 and port 443. No further
  route was tried, since working around the proxy is not Dromon's to do.
  **Inference.** Git over SSH works only for a line whose Claude Code sandbox is
  disabled. Under an enabled sandbox, use HTTPS with a credential the operator
  provides instead. Dromon configures no proxy route.
- **Observed on 2026-10-29, outside a sandbox.** The `GIT_SSH_COMMAND` that
  `dromon.gitssh` builds, run by `git ls-remote` with no `SSH_AUTH_SOCK` and a home
  directory that does not exist, reached GitHub, verified its host key against a
  `known_hosts` file built from GitHub's published keys, and stopped at public key
  authentication for a throwaway key GitHub does not know. With an empty `known_hosts`
  file the same command was refused for the unknown host key.
- **Unverified.** Signing with a key the agent holds, since the operator's agent held
  none during the test, and any push. Whether Codex's and Grok's own sandboxes let a
  command connect to the socket or reach the network, and Codex's workspace profile is
  believed to deny the network.

## Session names

**Round 19, by the operator's direction.** Claude Code names a session from its working
directory unless it is given `--name`, so the sessions of several lines in one project
were hard to tell apart on the machine. Dromon gives each Claude Code session it starts
a name from the `daemon.session_name` template, through the Agent SDK's `extra_args`,
which the bundled Claude Code 2.1.310 takes as `-n` or `--name`. `dromon.sessionname`
holds the one naming function.

- **Fields.** A template is literal text and fields in braces, each field at most once.
  - `{vv}` is the installed pydromon's major version followed by its minor version,
    each in decimal, so 0.0.0 gives `00`.
  - `{project}` is the last step of the project root, the name the fleet index gives.
  - `{role}` is the role the line is a seat of, or the line's identifier when it is a
    seat of none.
  - `{variant-}` is empty, or the variant followed by `-`. The variant is the part of
    the line identifier beyond the role. A seat `review-luna` or `luna-review` of role
    `review` gives `luna`. A seat whose identifier neither begins with the role and
    `-` nor ends with `-` and the role gives its whole identifier.
  - `{model}` is the model's code and version, `Op`, `Ha`, `Fa` or `Sn` for Opus,
    Haiku, Fable or Sonnet, followed by the version's digits, so `claude-opus-5-5`
    gives `Op55`.
  - `{effort}` is `lo`, `me`, `hi`, `xh` or `mx` for low, medium, high, xhigh or max.
- **Model and effort.** Both are the ones the engine is started with, the line's
  configured effort or else its driver's, which Dromon always passes as `--effort`. So
  the effort is always known for a Claude Code line, and is left out only for an effort
  without a code. A model without a code gives no name, with a warning.
- **Uniqueness.** When a name would equal the name of another line whose engine is
  live, digits are appended to the variant, the fewest that make it unique, trying 1,
  2 and on in order. A role's own line then takes the digits alone as its variant, so
  `PDr00-pydromon-core-Sn55hi` is followed by `PDr00-pydromon-core-1-Sn55hi`. A
  dropped engine, or a build refused after it was named, frees its name. A rebuilt
  engine is named again, and keeps its name when no other live line has taken it.
- **Checks at load.** A template is 1 to 128 characters, holds only letters, digits,
  `.`, `_` and `-` outside its fields, names only the fields above, does not begin with
  `-`, and holds `{variant-}`, where the digits go. Any other is refused with the file.
- **Checks on each name.** A name must be one argument of at most 96 letters, digits,
  `.`, `_` and `-`, not beginning with `-`, and must hold no term of the disclosure
  list. A name that fails is not given, the session keeps Claude Code's own, and the
  daemon writes a warning naming the line and the reason, never the term, to its
  standard error.
- **Every start.** The name is part of the options every start, resume and kept
  connection of the engine is built from. `dromon probe` and a scheduler without a
  project give no name.
- **Residuals.** The bound of 96 is Dromon's own, since no limit of Claude Code's is
  known. Two versions can give one `{vv}` once a major or minor version reaches 10,
  such as 1.12 and 11.2. Uniqueness holds among the sessions of one daemon, not across
  daemons, so two projects of one name on one machine could show equal names. A
  warning reaches only the daemon's standard error, not a line's status. Whether
  Claude Code keeps a name across a resume, or takes the latest given, was not
  observed, since Dromon passes the same name on each.
- **Other engines.** Out of scope for round 19. From strings in the installed files,
  not from running them, which is an inference. Grok Build 1.0.70 has an interactive
  `/rename` command, a protocol method `x.ai/session/rename`, and `-s` or
  `--session-id` to choose a session identifier, but no launch option like `--name`
  was found. Cursor Agent 2026.10.01 has an interactive `/rename` command. Pi holds a
  `setSessionName` function whose use was not established. Codex was not examined,
  since its native binary was not found where it was sought.

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

## Accepted disclosure occurrences

The operator may set `[daemon.disclosure_accepted]` to a mapping from project relative
file paths to lists of lowercase Secure Hash Algorithm 256 digests. Each digest covers
the exact UTF-8 line content without its line ending. A trailing carriage return
is removed before hashing in both the gate and tool. There are at most 256 paths and at most
256 digests per path. Absolute paths and parent traversal are refused.

An acceptance applies only to that file during `check_disclosure` and the local gate
scan. Identical content anywhere in that file is exempt, including repeated lines.
Moving the line within that file preserves acceptance. Editing the line or
copying it to another file checks it again. Text checks and message fields receive no
exemption. Existing private gate exceptions continue to apply for compatibility.

The project file is an operator trust boundary. Lines are denied `.dromon`, so they
cannot read the accepted hashes or test guesses against them offline. Someone who can
read that configuration can test candidate short lines against a hash, so the hashes
must not be treated as secret resistant to offline guessing. Entries contain paths
and hashes, never listed terms or exact line text.

A line able to edit an accepted file can try candidate content and observe whether
the aggregate count drops. Each such check spends the existing hourly allowance.
Acceptance remains scoped to content within the configured file under that limit.
Refusals name a field only for calls offering one text field. No file names, line
numbers or line ranges are returned to a line. File and line locations were declined because
they would let a line narrow its own text to a listed term. Aggregate counts and
the hourly limit remain diagnostic safeguards, not protection against a malicious
agent. The gate reads the same project acceptance table.

## Waiting between ticks

A positive `daemon.tick_seconds` bounds the wait between work events. Zero disables
periodic ticks. A running turn alone does not require another tick. Messages, turn
completion and configuration changes wake the loop. Scheduled wakes and mail whose
cooldown expires retain their timers. Mail awaiting a wake cadence advances only
while a tick can make progress. Stop requests interrupt the wait.

Launch record inspection and pending peer mail retain bounded maintenance timers,
even with zero cadence. Tests that advance an injected clock must also provide an
event to notify the scheduler of that change.
This avoids spending processor time polling a clock that cannot advance itself.
