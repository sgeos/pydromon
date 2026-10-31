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
| `daemon.tick_seconds` | number in [0, 60] | 1 | The tick cadence |
| `daemon.require_sandbox` | boolean | `false` | Whether every Claude Code line must run with an enabled sandbox, as below |
| `daemon.disclosure_terms` | path | none | The project's disclosure list, relative to the project root, as below |
| `read_only_paths` | list of relative paths | empty | Existing files or directories denied writes under every line's resolved workdir. Symbolic links are resolved and targets must remain inside that workdir |
| `leases.names` | list of identifiers | none | The leases lines may acquire, each named once, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies |
| `leases.ttl_seconds` | integer in [60, 86400] | 10800 | How long an acquired lease lasts unless released first |
| `runs.commands` | table of names to arrays of text | none | The commands lines may start as supervised runs, at most 64, each name an identifier and each command its program and at most 63 arguments, never a shell string, as [`RUNS.md`](../architecture/RUNS.md) specifies. A declared command runs with the daemon's authority, outside any line's sandbox. Added on 2026-10-31 |
| `limits.*` | as in `CLIENT.md` | 12, 2, 0 | The conversation limits of every line |
| `wake.every_ticks` | integer in [1, 3600] | 1 | The wake policy of every line |
| `profiles.NAME` | table | none | A reusable line template with every line key but `profile` and `session` |
| `lines.ID` | table | none | A line, named by a line identifier |
| `lines.ID.profile` | profile name | none | The profile the line starts from |
| `lines.ID.model` | model | required | `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5`, `gpt-6-luna`, `gpt-6.1-sol` and `gpt-6.1-terra` on Codex as [`CODEX.md`](CODEX.md) specifies, `grok-4.7` on Grok Build as [`GROK.md`](GROK.md) specifies, `pi:openai/gpt-6-luna` on Pi as [`PI.md`](PI.md) specifies, `cursor-agent:gpt-5.6-luna-medium` on Cursor Agent as [`CURSOR.md`](CURSOR.md) specifies, or `antigravity:gemini-3.8-flash-high` on Antigravity as [`ANTIGRAVITY.md`](ANTIGRAVITY.md) specifies. A daemon refuses Cursor and Antigravity lines |
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
  created through the client and absent from the file are left as they are.
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
with operator authority, as [`SANDBOX.md`](SANDBOX.md) records.

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
- **What a refusal says.** A tool refusal names the field of the call that contains a
  listed term, such as `body` or `note`, when the call offered text in that one field
  only, and never names the term or its position. A call that offered text in several
  fields is refused with the generic text, which names no field. **Review r4, finding
  F1.** Naming each matching field of such a call would answer one bit per field in
  one counted refusal, so a line could test several candidate words for the price of
  one. A line that wants the field named sends it alone, at the cost of one check. No
  message is recorded, and the refusal is recorded as `disclosure_checked`, which
  holds no text. A refusal of an outbound federated message follows the same rule for
  its `body` and `sender`.
- **Not checked.** Messages from operators, who hold authority, and the questions the
  daemon asks on a held line's behalf.
- **Secrecy.** Terms are never logged, returned or stored. Errors about the file name
  the file and not its content. The settings view shows the path only.
- **Limits.** A term written another way, split across messages, or encoded passes.
  The guard catches accidents and is not a barrier against a line that tries to evade
  it. Engine transcripts, which the engines keep themselves, are outside it.
- **Checking before sending.** **Item B2 of 2026-10-28.** A line's `check_disclosure`
  tool checks exactly one of `text`, a `path`, or a `paths` list of one to eight files
  relative to its working directory, and answers `pass` or `fail` with how many lines
  hold a listed term, compared as above, and never which term or which file matched.
  Each file is checked independently and the counts are summed, so a match cannot span
  a file boundary. Their combined size may not exceed 1 MiB. A file that cannot be
  read refuses the whole call without recording a check. Each successful call,
  including a list, is recorded as one
  `disclosure_checked` event, without the text or its name, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies. **Proposed.** A line may
  check at most 10 times in any 3,600 seconds, counted from the recorded times on the
  wall clock, so the limit survives a restart. Every answer says whether a text holds
  a term, so an unlimited check would let a line test candidate words one by one. A
  file is read as `write_scratch` writes, walked without following a link, and only
  when it is a regular file of the daemon's user with one name, outside every
  directory Dromon protects, and at most 1 MiB. A refusal, of a missing list, a limit
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
  refusal reveals nothing of the list. **Fact.** Each spent limit still yields one
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
  since an enabled sandbox denies it. No path may hold a control character, a quote, a
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
