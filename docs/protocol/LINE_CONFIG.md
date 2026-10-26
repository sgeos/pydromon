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
| `builtin_tools` | Claude Code tool names, each a capital letter followed by letters, digits or `_` | none |
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
| `instructions` | At most 16 distinct files, such as `CLAUDE.md`, whose text is appended to the system prompt at each turn. A relative path is resolved against the line's working directory and `~` against the home directory. Omitted when empty | none |
| `workdir` | The directory the line's engine runs in, as the working directory section below specifies. Omitted when unset | the project root |
| `keep_session` | Keep a Claude Code line's engine connected between turns, as the kept sessions section below specifies. Refused on Codex and Grok lines. Omitted when false | `false` |
| `keep_session_idle_seconds` | Seconds a kept connection waits for the next turn before it closes, a whole number from 60 to 86,400. Omitted at its default | `1800` |
| `keep_session_max_seconds` | Seconds after the last turn a kept connection may stay open while the engine reports background tasks running, a whole number from 60 to 86,400 and not less than `keep_session_idle_seconds`. Omitted at its default | `14400` |

A server name is an identifier and may not be `dromon`, which Dromon's own tools use. A
`stdio` server has `command` and optionally `args` and `env`. An `http` or `sse` server
has `url` and optionally `headers`. A URL must use `https`, or `http` on `127.0.0.1` or
`localhost`. At most 256 entries are accepted in any list or mapping, and each text is
at most 4,096 characters.

## Variable references

Any text of an MCP server, its command, arguments, `env` values, url and header values,
may hold `${NAME}`, or `${NAME:-default}` for a default used when `NAME` is unset, as
Claude Code's `.mcp.json` does. The configuration stores the reference, and the engine
replaces it from the daemon's environment at the start of each turn, so a token can stay
out of the event log.

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

## Dromon's own tools

Every line has Dromon's own tools, whatever its configuration, and each fixes its
sender to the line. External agents have a subset, as [`AGENTS.md`](AGENTS.md)
specifies. Their arguments are untrusted input and are checked strictly, and an
argument outside a tool's schema is refused.

| Tool | Effect |
|---|---|
| `send_message` | Sends a note to a line or an operator, delivered in a later tick |
| `ask_operator` | Asks an operator a question, which never pauses the line |
| `wake_after` | Schedules one wake for the line, as below |
| `list_mail`, `read_mail`, `acknowledge_mail` | Page through, open and acknowledge the line's mailbox |
| `list_sent` | Lists what became of the line's sent mail |
| `acquire_lease`, `release_lease` | Take and give up a lease the project declares, as below |
| `line_status` | Shows the line's own state, its daemon and the other lines, read only, as below |
| `read_instructions` | Shows the instructions the line's next turn receives, as below |
| `write_scratch` | Writes a file under `tmp/` in the line's working directory, as below |
| `list_processes`, `stop_process` | List and stop the processes the line started, as below |

**Decided by the operator on 2026-10-26**, from the lines' own reports, for what follows
in this section.

**`line_status`.** No arguments. It returns JSON for the calling line: its `state`, the
question holding it as `held_by`, its `autonomous_turns` used and allowed, its estimated
`cost_usd` since its last release and its limit, its `leases` with their ends, its
`scheduled_wake`, its `unread` mail and `pending` deliveries, its `model` and
`strategy`, its `workdir` as configured and as `workdir_path` resolved, its
`engine_session`, and its `kept_session`, as the kept sessions section below specifies.
It adds a `daemon` object and, for every other line, its `line_id`, `state`, whether it
is `held`, and the names of its `leases`. It holds no configuration document and no
message body.

The `daemon` object holds the following, decided by the operator on 2026-10-26 in round
2.

- **`version`.** The package version. **`version_note`** says that `0.0.0` is the version
  of every unreleased build, so that the commit identifies it, and is `null` for any
  other version.
- **`commit`.** The commit the daemon started from, read once when `dromond` starts, from
  the `HEAD` of the checkout its own code runs from, following loose refs, `packed-refs`
  and a linked worktree's pointers without running git. It is `null` when the daemon
  does not run from a checkout or the commit cannot be read.
- **`checkout_commit`.** The same checkout's `HEAD`, read again at every call, which costs
  a few reads of small files and no process.
- **`stale`.** `true` when the two differ, `false` when they match, and `null` when
  either is unknown. **`stale_note`** then says that the running daemon does not have the
  newer code until an operator restarts it. A checkout that moved does not prove that
  the code differs, since a commit can change only documents, so the note is a prompt to
  check rather than a verdict.

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

**`write_scratch`.** Arguments `path`, relative to `tmp/` in the line's working
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

Each turn's prompt opens with one line giving the line's autonomous turns used of
those allowed and its estimated cost since its last release of its limit, so the line
sees how close a hold is without calling `line_status`. The mail summary and the
headers of the newly delivered mail follow, and bodies stay in the mailbox.

- **Repeats.** A message whose sender, recipient and body match a message sent at most
  15 minutes before it is still delivered, and its header ends with
  `a repeat of` and the earlier message's identifier. The daemon remembers messages to
  lines by sender, recipient and a digest of the body, and forgets each once it is more
  than 15 minutes old, so the memory stays bounded and the check takes constant time.
  The memory does not survive a restart, so a message sent before one is never marked.
- **Scheduled wakes.** A fired wake's header says it is the line's scheduled wake and
  gives the time it was due and the time it is delivered. The due time is kept in the
  state the event log builds, so it survives a restart.
- **`list_mail`.** **Decided by the operator on 2026-10-26** in round 2. A listed entry
  ends with `repeat_of` and the earlier message's identifier when its message repeated
  one, and a fired wake's entry ends with `scheduled wake due` and its due time, then
  `delivered` and the time it was delivered. The repeat marks and delivery times live
  in the daemon's memory, at most 4,096 of each with the oldest forgotten first, so
  after a restart a wake's entry gives its due time only and no entry is a repeat.
- **Unanswered.** The summary's unanswered count leaves out a reply to a question Dromon
  asked on the line's behalf when it held the line, such as an operator's `resume`,
  because such a reply asks nothing of the line. The state the event log builds keeps
  every such question's identifier from its `line_held` event, so the rule survives a
  restart. **Decided by the operator on 2026-10-26** in round 2.

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
  engine session is cleared, whether by `clear_session` or by a change of its
  `workdir`, when its holder is rebound, and when a reload stops declaring its name.
- **Memory only.** Leases live in the daemon's memory and are not events, so a restart
  drops every lease. A resource a stopped daemon's lines were using is then no longer
  guarded, which suits a resource such as a gate whose processes the operator restarts
  too. A line that needs the lease after a restart acquires it again.
- **Visibility.** `status` and `line_view` show the leases each line holds.
- **Agents.** External agents have no lease tools.

**`wake_after`.** **Decided by the operator on 2026-10-26.** A line that starts a long
command in the background, which survives the end of its turn, needs a way to be woken
to read the result.

- **Arguments.** `seconds`, a whole number from 60 to 86,400, and `note`, text of at
  most 2,000 bytes. A number with a fraction, a boolean or text is refused.
- **Effect.** The daemon appends `wake_scheduled` with the due time, the wall clock in
  Coordinated Universal Time plus `seconds`, rounded up to the whole second so a wake
  never fires early. A line has at most one pending scheduled wake, and a new call
  replaces it, which the tool's answer reports with the replaced due time.
- **Firing.** At the first tick at or after the due time, the note is recorded as a
  message from the line to itself, as [`EVENT_LOG.md`](EVENT_LOG.md) specifies under
  scheduled wakes. It wakes the line through the ordinary path in a later tick, counts
  toward the line's autonomous-turn limit like any mail that is not from an operator,
  and waits like other mail while the line is held. A wake that fell due while the
  daemon was down fires at its first tick. A note that the disclosure list in force at
  firing covers is replaced by a fixed notice, as [`EVENT_LOG.md`](EVENT_LOG.md)
  specifies, so a list changed by a reload or a restart still applies.
- **Rest.** While nothing else is in flight, the daemon's loop rests until the next
  wake falls due, for at most 60 seconds at a time, so a change of the wall clock
  delays a wake by at most that much.
- **Visibility.** `status` and `line_view` show the pending wake's due time and the
  identifier its note will carry, as [`CLIENT.md`](CLIENT.md) specifies.

**Disclosure guard.** When the project names a disclosure list, `send_message`,
`ask_operator` and `wake_after` refuse a body, question, choice or note that contains a
listed term, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies, and record nothing.
A wake note is checked again when it fires.

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
  again, and the tool, credential and model checks run on it as on a new connection. A
  turn on a reused connection that shows the model's output before an `init` is
  refused and interrupted, and its connection closed.
- **The gate between turns.** On a kept connection the permission gate denies every tool
  call outside a Dromon turn, and inside a reused turn until its `init` check passes.
- **A residual risk.** **Inference from the engine's reload messages, not tested.** The
  engine watches its settings and plugins and may reload them while idle. The line's
  sandbox denies it writing them, but another writer could change them between turns,
  and a turn the engine began by itself could run a reloaded hook before Dromon
  interrupts it. The next turn's checks then refuse and close the connection. Within
  one turn the same window existed before, between the check and the engine's start.
- **The driver's budget.** Each turn starts the driver afresh, so its tool budget is per
  turn as before.
- **Turns the engine begins.** **Inference from the engine's own Bash description, not
  observed.** When a background command ends while the connection is idle, the engine
  re-invokes the model by itself. Only Dromon starts turns, so such a turn is
  interrupted as soon as it shows, its tool calls are denied by the gate, its remaining
  output is discarded, and the line's next prompt says so. If it has not finished by
  the next turn, the connection is closed instead.
- **Background tasks.** The engine reports each background task's start and end, and
  Dromon counts those running. A connection closes after `keep_session_idle_seconds`
  without a turn, or, while the engine reports tasks running, after
  `keep_session_max_seconds`, both counted from the end of the last turn and checked
  every 30 seconds. Closing the connection ends the engine's process.
- **Other closes.** The connection also closes when the line is configured, overridden,
  rebound or has its session cleared, which includes a change of `workdir`, when the
  daemon stops, and when a turn is refused or fails, including by a check before the
  connection is used, since the engine may already have loaded what the check refused. A change made while a turn runs
  closes the connection when that turn ends. The next turn resumes the session in a new
  process as before, and when Dromon closed it for a reason the line could not see, the
  prompt says why and how many background tasks were running.
- **The engine's death.** When the engine's process ends between turns, the next turn
  resumes the session in a new process, and its prompt says that the old process's
  background tasks were lost. When it ends during a turn, the turn fails, and the next
  prompt says the same.
- **Interrupt.** An operator's interrupt cancels the turn. Dromon interrupts the engine
  and discards the rest of that turn's output, and the connection stays open. If the
  engine does not take the interrupt within 10 seconds, the connection is closed.
  **Hypothesis, not observed.** The engine may stop its background tasks on an
  interrupt.
- **`line_status`.** `kept_session` gives whether the line is `configured` to keep its
  session, whether a connection is `kept` now, the time it `closes_at` in Coordinated
  Universal Time, which is `null` during a turn, and the number of `background_tasks`
  the engine reports running.
- **Not yet.** Codex and Grok lines refuse `keep_session`. A Codex line could keep its
  app-server thread and use its background terminal methods, which is recorded as
  future work in [`CODEX.md`](CODEX.md).

## Instructions

Dromon starts Claude Code with no settings sources, so Claude Code reads no
`CLAUDE.md` by itself, and without `claude_code_prompt` the SDK passes an empty system
prompt. **Fact, read on 2026-10-25** in the SDK's command construction. The files named
in `instructions` are read at the start of each turn, each under a heading naming it,
and a file that is missing, larger than 256 KiB or not UTF-8 refuses the turn. Only the
paths are stored. On Codex the text becomes the thread's developer instructions, and
`claude_code_prompt` is refused, since Codex uses its own.

## Working directory

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
  and its `:workspace_roots`, and the Grok agent's working directory. A line's relative
  `instructions` paths, and the relative paths of its sandbox document, resolve against
  it. Permission rules in its settings are interpreted by Claude Code against the same
  working directory. The project's state and runtime directories stay protected for
  every line wherever it runs.
- **Sessions.** Claude Code keeps a session under the directory it ran in. When a
  line's `workdir` changes, by `configure_line` or by the project file at start or
  reload, the daemon appends `line_session_cleared`, so the line's next turn starts a
  new session. A turn in flight at the change does not record its session. The
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

A line on the Codex or Grok Build engine accepts only the configurations
[`CODEX.md`](CODEX.md) or [`GROK.md`](GROK.md) lists, and any other is refused when the
line is created or configured.

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

## Not yet specified

- References to secrets held in files or a keychain rather than the environment.
