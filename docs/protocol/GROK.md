# Grok Build Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative for Dromon's side. The Python module
`dromon.grok` implements it against Grok Build 1.0.70. **Not verified against a live
turn.** An operator probe on 2026-10-27 created a session, which Dromon refused before
any prompt was sent, as the probe section below records. Dromon's own Grok home, which
that probe called for, was built the same day and has not yet run.

## Purpose

A line with `workdir_access: read` is refused. **Review of read-only seats, finding 3,
and its second round, findings 1 and 2.** The `dromon-reader` profile rests on Grok's
built-in `read-only` base, whose denial of writes Dromon names but cannot inspect, and
no live run has shown that profile deny a write to the worktree. The capability table
therefore records Grok's `read_only_workdir` as unknown, and the adapter refuses a
read-only line while it is anything but known. A probe that confirms the reader profile
lifts the refusal by changing that entry. Until then the reader profile serves lines
given no `Edit` or `Write`.

**Directed by the operator on 2026-10-25.** Nominal support for Grok, without testing
until Grok usage is available. A line whose driver names the `grok-build` engine runs
its turns through `grok agent --no-leader stdio`, which speaks the Agent Client
Protocol, ACP, as JSON-RPC 2.0, one message per line. The protocol client, its request
loop and the answers Dromon gives, lives in `dromon.acp`, which the Antigravity adapter
of [`ANTIGRAVITY.md`](ANTIGRAVITY.md) shares. Grok's sandbox profile, sign-in check and
tool policy stay in `dromon.grok`.

## Evidence

**Facts, read on 2026-10-25** in Grok Build's bundled guide, `~/.grok/docs/user-guide`,
and its `README.md`.

- `grok agent stdio` runs the agent over ACP. Agent options such as `--model` and
  `--reasoning-effort` precede the transport, and `--no-leader` starts a local agent.
- `session/new` and `session/load` take `cwd`, `mcpServers` and `_meta`, whose
  `yoloMode` and `autoMode` choose always-approve and auto mode, and whose `rules` are
  appended to the system prompt. `session/prompt` streams `session/update`
  notifications, including `agent_message_chunk`, `agent_thought_chunk` and
  `tool_call`, and the agent may send `session/request_permission`.
- Custom sandbox profiles are defined only in `~/.grok/sandbox.toml` or a project's
  `.grok/sandbox.toml`, where the user's definition wins, and are selected with
  `GROK_SANDBOX`. A `deny` entry is kernel-enforced against reads, writes and renames.
  A custom profile that cannot be applied stops Grok from starting. On macOS no profile
  blocks a child process's network access. The `GROK_CONFIG` overlay cannot set the
  sandbox, the permission mode or MCP servers.
- Grok resolves a credential from its cached sign-in before `XAI_API_KEY`.

**Fact, observed on 2026-10-26** from installed Grok Build 1.0.70, commit
`f50904eaee6c`. `grok agent --help` lists `--always-approve`, but neither it nor
`grok agent stdio --help` lists a permission-mode flag. Dromon therefore does not pass
one. It sets both `yoloMode` and `autoMode` to false and checks or switches the session
mode to `default` through ACP before the prompt.

**Facts, observed on 2026-10-25.** An `initialize` request, which runs no model,
answered with protocol version 1, `loadSession`, MCP over HTTP and SSE, the
authentication methods `cached_token` and `grok.com` with `defaultAuthMethodId` set to
`cached_token`, and a current model of `grok-4.5`. Grok's model cache gives `grok-4.7` a
default context of 256,000 tokens and the efforts `low`, `medium`, `high` and `xhigh`.
The operator's Grok configuration sets `permission_mode` to `always-approve` and
declares MCP servers for mail and documents.

## The first live probe

**Facts, observed in the operator's probe on 2026-10-27** against Grok Build 1.0.70
with `grok-4.7`. The raw recording is private, and no part of it is tracked. The
fixtures in `tests/test_grok.py` and `tests/test_evidence.py` are invented in the same
shapes.

- `initialize` answered as before, with `defaultAuthMethodId` `cached_token`. Its
  `_meta` also names the agent's version, its ids and the machine's host name.
- `session/new` answered with `sessionId`, `models` and `configOptions`, whose ids are
  `model` with the category `model` and `reasoning_effort` with the category
  `thought_level`. It had no `modes`, so Dromon refused the turn with "the engine does
  not report its permission mode" before the prompt. Its `_meta` holds
  `x.ai/sessionConfig`, whose options list the models and, under the category `mode`,
  the reasoning efforts. That category is therefore not a permission mode.
- Before that answer Grok sent `_x.ai/session/setup` in phases from `auth` to
  `tool_overrides`, `_x.ai/mcp/servers_updated`, `_x.ai/models/update`,
  `_x.ai/settings/update`, `_x.ai/announcements/update` twice, `_x.ai/mcp/init_progress`,
  `session/update` with `available_commands_update` twice, and
  `_x.ai/session_notification` with `model_changed`. Grok sends the extension methods
  with ACP's `_` prefix, which its guide omits.
- `_x.ai/mcp/servers_updated` listed eleven MCP servers from the operator's own
  configuration, ten over HTTP and one over stdio, although Dromon passed
  `mcpServers` empty. `_x.ai/mcp/init_progress` then gave a `total` of twelve, one more
  than the list. **Inference.** A server outside that list, such as one a plugin
  brings, was counting, so the list alone does not show every server.
- `_x.ai/settings/update` carried `permission_mode` null while the operator's
  `config.toml` sets `always-approve`. **Inference.** That notice holds the account's
  settings from Grok's service, not the session's mode.

**Facts, read on 2026-10-27** in Grok's bundled guide and shell completions, without
running Grok. `grok agent` takes no `--permission-mode` and no flag that skips MCP
servers. The mode is set by `[ui] permission_mode` or `yolo` in `~/.grok/config.toml`,
by a Claude Code `defaultMode`, by `--always-approve`, or by `yoloMode` and `autoMode`
in `session/new`, which can turn approval on but are not documented to turn it off.
The `GROK_CONFIG` overlay can set neither the mode nor MCP servers. MCP servers load
from the user's and the project's `config.toml`, from plugins, and from Claude Code's
and Cursor's files, the last two of which `GROK_CLAUDE_MCPS_ENABLED` and
`GROK_CURSOR_MCPS_ENABLED` turn off. An allow list in `requirements.toml` can block
servers, but it binds the operator's own Grok as well. `GROK_HOME` moves the whole
configuration, sign-in included.

**What follows for Dromon.** Nothing in Grok 1.0.70 lets Dromon confirm or set the
permission mode, or keep the user's MCP servers out, from the client side alone. The
way open is a Grok home that Dromon owns, which the next section describes.

## Dromon's Grok home

**Decided by the operator on 2026-10-27**, following Pi's dedicated agent directory.
A Grok line runs with `GROK_HOME` set to `~/.grok/dromon-home`, never the operator's
own `~/.grok`, and an inherited `GROK_HOME` is replaced. Grok's guides say that
`config.toml`, `auth.json`, `managed_config.toml`, `requirements.toml`, sessions, rules
and workflows live in `$GROK_HOME`.

- **What Dromon writes.** Before each turn Dromon creates the home, owner-only, and
  writes its `config.toml` and its `sandbox.toml` when either is missing. An existing
  file is never replaced. The `config.toml` sets `[ui] permission_mode = "default"`,
  Grok's asking mode, declares no MCP server and no hook, and sets the `mcps` and
  `hooks` cells of `[compat.claude]` and `[compat.cursor]` to false. The
  `sandbox.toml` holds the two profiles below, written for the line that first needs
  them.
- **What Dromon refuses.** Before each turn, and before Grok starts, Dromon refuses a
  home that is a symbolic link or may be written by other users, a `config.toml` whose
  parsed contents differ from Dromon's in any key, a `managed_config.toml` or
  `requirements.toml`, in the home or in `/etc/grok`, that sets anything, a
  `trusted_folders.toml` that trusts any folder, and any of `agents`, `hooks`,
  `hooks-paths`, `installed-plugins`, `lsp.json`, `plugins`, `rules`, `skills` or
  `workflows`. Empty regular files are allowed because they load no entries, and the
  `installed-plugins` directory may hold only empty regular files named `*.lock`.
  **Observed on 2026-10-30.** Grok created an empty `hooks-paths` file and an
  `installed-plugins` directory containing an empty `registry.lock` file.
  **Inference, not verified live.** These records load no hook paths or plugins.
  Symbolic links, non-empty files and all other directory contents are refused. The
  refusal for a drifted `config.toml` says to delete it, after which Dromon writes it
  again.
- **Credentials.** Dromon never writes, reads or copies a credential. It never opens
  `auth.json` in either home. The operator signs Grok in to Dromon's home once, by
  hand, with the command in Verify first below.
- **The line's home directory.** **Decided by the operator on 2026-10-27**, as an
  Antigravity line runs. Grok runs with `HOME` set to `~/.grok/dromon-home/home`, a
  directory inside Dromon's Grok home, and with `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
  `XDG_STATE_HOME` and `XDG_CACHE_HOME` removed. Dromon creates it owner-only and
  refuses one that is a symbolic link or may be written by other users. Grok's guides
  name `~/.claude`, `~/.claude.json`, `~/.cursor` and `~/.agents` as places from which
  it loads another harness's settings, servers, hooks, rules and skills, and `~/.grok`
  is its own default home. Dromon refuses a turn when the line's home directory holds
  any of them, a file when present and a folder when it holds anything. So Grok cannot
  read the operator's own `~/.claude/settings.json`, which Grok has no setting to stop
  it reading. **Not established by the installed guides.** They do not say whether
  Grok creates a `.grok` entry in this isolated `HOME` on start. **Operator report, not
  independently verified.** The entry most likely came from manual Grok initialization,
  rather than a Dromon-run turn. No code allowance is added. A live Dromon turn will
  establish whether Grok creates the entry in this isolated home.
- **Claude Code's settings.** Grok's permissions guide says it also reads Claude
  Code's `defaultMode` from `~/.claude/settings.json`, `settings.local.json`, Claude
  Code's managed settings, and each project level up to the repository's root, and it
  does not say whether its own `permission_mode` outranks that. Dromon therefore
  refuses a turn when any of those files names a mode other than `default`, with `~`
  read as the line's home directory, before the check of that directory above, so a
  mode set there is named in the refusal. **Fact, observed on 2026-10-27.** A file in
  the operator's own home names another mode, which refused every Grok turn until the
  line's home directory replaced it. On that machine, the managed files and this
  repository's project files, in its root and in the engines worktree, were absent on
  that day.
- **What depends on `HOME`.** **Inference from Grok's authentication guide.** The
  sign-in is `auth.json` under `$GROK_HOME`, and the guide names no keychain, so the
  sign-in does not depend on `HOME`. Its interactive permission grants are kept in
  "Grok's own state directory under your home directory", which this document reads
  as `$GROK_HOME`. A `~` in a sandbox profile is anchored at `HOME`, so Dromon reads
  one as the line's home directory. Dromon's own MCP server starts from absolute
  paths and does not read `HOME`. Git inside a Grok turn finds no global
  configuration of the operator's.
- **A project's own `.grok`.** **Review of read-only seats, second round, finding 3.**
  Grok's guides say a project's `.grok` may hold a `config.toml` that adds MCP
  servers, plugins and permission rules, a `sandbox.toml` with profiles, and hooks,
  rules, skills and commands, which a worktree could supply. Dromon refuses a turn
  when any project level, the same levels as the Claude Code settings above, holds a
  `.grok` file or a `.grok` directory with anything in it. It refuses them whole
  rather than read them.
- **Not covered.** macOS device management, which Grok also reads for policy, cannot
  be checked from Dromon. Claude Code's skills, rules and instruction files in the
  project still reach Grok through its other compatibility cells, which add
  instructions but no tools.

## A turn

1. **Environment.** `XAI_API_KEY`, `GROK_AUTH`, `GROK_CODE_XAI_API_KEY`,
   `GROK_AUTH_PATH`, `GROK_CONFIG` and `GROK_CONFIG_PATH` are removed.
   `XAI_API_BASE_URL`, `GROK_CLI_CHAT_PROXY_BASE_URL`, `GROK_AUTH_PROVIDER_COMMAND`,
   `GROK_OIDC_ISSUER` and `GROK_OIDC_CLIENT_ID` are refused. `GROK_HOME` names
   Dromon's Grok home, `HOME` names the line's home directory inside it, the four
   `XDG_` home variables are removed, and `GROK_SANDBOX` names Dromon's profile.
   `GROK_CLAUDE_MCPS_ENABLED`, `GROK_CURSOR_MCPS_ENABLED`, `GROK_CLAUDE_HOOKS_ENABLED`
   and `GROK_CURSOR_HOOKS_ENABLED` are set to `0`. **Inference.** That `0` turns those
   scans off.
2. **Home and profile.** Dromon's Grok home, the line's home directory, the
   project's levels and Claude Code's settings must pass the checks above, and the profile below must exist in the home and pass its checks, or
   the turn is refused before Grok starts.
3. **`initialize`**, advertising no file system and no terminal, so Grok uses its own
   tools inside its sandbox. The protocol version must be 1 and
   `defaultAuthMethodId` must be `cached_token`.
4. **`session/new`**, or `session/load` with the stored session identifier, with the
   line's working directory as `cwd`, which is its `workdir` as
   [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies or the project root, Dromon's MCP
   server, and `_meta` with `yoloMode` and `autoMode` false
   and the line's instructions as `rules`.
5. **MCP servers.** Grok must have sent `x.ai/mcp/servers_updated` before its answer,
   listing no server but Dromon's own, which is none without a bridge. An
   `x.ai/mcp/init_progress` whose `total` exceeds Dromon's servers refuses the turn
   too, as does a server list at any later point that names another server. A refusal
   counts the other servers and never names them.
6. **Model and mode.** The model identifier comes from `driver.settings.model`, the
   same name Dromon passes to Grok's `--model` option. It must equal the driver's model
   name exactly. Dromon does not derive it from the engine table or accept Grok's
   reported default as a fallback. Both `session/new` and `session/load` must report a
   `configOptions` entry for `model`. If `acp.reported_model` does not equal the
   driver's model, Dromon requests `session/set_config_option` with `configId` `model`
   and `value` containing the driver's identifier. The response is the updated option
   list, from which Dromon reads the model with `acp.reported_model` and runs the
   driver's `init` check. If both `models.currentModelId` and the model option report a
   value, they must agree. Dromon refuses conflicting or unreadable model reports
   rather than choosing one field. A refusal, absent model option, or different model
   refuses the turn before the prompt. This also applies when resuming a session with
   `session/load`. A successful pin supersedes a mismatching model notice held before
   its response. The response follows the notice in the session stream and reports the
   updated options, so Dromon treats it as the later account of the session state. A
   mismatching notice received after that response refuses the turn. When the answer
   has `modes`, `currentModeId` must be `default` or
   `ask`, and otherwise Dromon sends `session/set_mode` with `default` if Grok offers
   it. Without `modes`, a `configOptions` entry of the category `mode` must have one
   of those values. With neither, as from Grok 1.0.70, the turn rests on the checks of
   step 2. **Changed on 2026-10-27.** Before Dromon's Grok home, such a session was
   refused. A `current_mode_update` to another mode during the turn cancels it.
7. **Grok's notices.** An `x.ai/models/update` with a `currentModelId`, or an
   `x.ai/session_notification` of `model_changed`, that is not the driver's model
   refuses the turn. An `x.ai/settings/update` whose `permission_mode` is set and is
   not an asking mode refuses it. `x.ai/session/setup` and `x.ai/announcements/update`
   are passed over, as is any notice not named here. A request not named here is
   answered with an error.
8. **`session/prompt`** with the wake prompt. Message chunks are text and thought
   chunks are thinking. A tool call is judged against the line's tools when it is first
   seen, in a `tool_call`, a `tool_call_update` or a permission request, and its
   `toolName`, `title` and `kind` are recorded then. For `use_tool`, the checked
   `rawInput` is recorded too. A later update or request that changes a recorded field
   refuses the turn, as does a call without an identifier. The normal order of one
   announcement, one permission request and subsequent updates for that identifier is
   accepted and counted once. A permission request that arrives before its announcement
   is also accepted and counted once. A repeated announcement or another permission
   grant for an identifier already granted in the turn is refused.
9. **Permission requests** are answered with an `allow_once` option for a permitted call
   and a `reject` option otherwise. A tool call the line was not given, a refused
   permission request, or the driver's tool budget, sends `session/cancel`, and every
   permission request after it is answered as cancelled. Other requests from the agent
   are answered with an error.
10. **Cancel.** **Re-review, N2.** After `session/cancel` Dromon waits at most
   `ABORT_SECONDS`, 60 seconds, for Grok to answer its prompt, and then leaves the
   connection, which stops Grok. An interrupted turn is reported as cancelled, and a
   refused one keeps its reason. Before, a Grok that ignored the cancel held the line
   without bound.
11. **Wait bounds.** Initialization, sign-in and session opening have a 180 second
   deadline, with the failed stage named. A turn has a sliding inactivity deadline of
   `TURN_SECONDS`, 3600 seconds, reset by model or tool activity. Its absolute deadline
   is `ABSOLUTE_TURN_SECONDS`, four hours, so a continuous stream cannot hold the line
   forever. The error names inactivity or the absolute cap.

| ACP tool kind | Permitted by |
|---|---|
| `read` | `Read` |
| `search` | `Grep`, `Glob` or `Read` |
| `edit`, `delete`, `move` | `Edit` or `Write` |
| `execute` | `Bash` |
| `fetch` | `WebFetch` or `WebSearch` |
| Any other | Only a tool of Dromon's MCP server, `dromon__NAME` in `toolName` |

**Changed on 2026-10-26, Antigravity review, F4.** A call is one of Dromon's own tools
only when its structured `toolName` names it. Before, the shared client fell back to
the call's `title`, which a file name or the model can make equal to a Dromon tool's
name. A call with only a title is judged by its kind as in the table, so one of the
kind `other` is refused. This fails closed.

**Fact, read on 2026-10-31** in Grok's installed `07-mcp-servers.md` under Tool
Discovery. `search_tool` discovers available integration tools by name or description.
`use_tool` calls a discovered integration tool by its fully qualified name. The guide's
inline input example is an object with exactly `tool_name` and `tool_input`, where
`tool_name` is a qualified catalog key such as `github__create_issue`. The guide also
documents file-backed input forms, which Dromon refuses. The installed
`14-headless-mode.md` shows ACP calls carry `rawInput`. **Not verified.** No recorded
Grok ACP call establishes that its `use_tool` raw input has the documented inline
shape. Dromon accepts only `rawInput` with exactly those two fields, a `tool_name`
equal to one of this line's own `dromon__NAME` tools, and object-valued `tool_input`.
It requires the ACP `kind` to be `other`, rejects a missing or ambiguous target, and
keeps the checked input fixed across the permission request. It counts the underlying
Dromon tool for the driver's budget and the report. `search_tool` is allowed only as
discovery and is counted as `search_tool`; the guide describes it as finding available
tools, while `use_tool` performs the integration call. Since the session is required
to load only Dromon's server, discovery can reveal that server's tool names and
descriptions, but does not invoke those tools. A title alone never grants either
meta-tool.

**Changed on 2026-10-31, the observed call shape.** Fact, from a Grok session log of
another project's line on 2026-10-31. Grok's first `tool_call` for `search_tool` had no
`kind` and no `toolName`, a `title` of `search_tool`, and `_meta["x.ai/tool"]` with
`name` `search_tool` and `namespace` `grok_build`. Its `tool_call_update` added `kind`
`other`, rewrote the `title` for display and added `variant` `SearchTool` to
`rawInput`. So every Grok call was judged as the kind `other` when it was first seen,
and refused, and a later update would have refused the turn for changing its title.

A call whose `_meta["x.ai/tool"]` names the namespace `grok_build` is now judged by that
name, which Grok writes and the model does not. `search_tool` is discovery and is
counted as itself. `use_tool` is permitted only for one of the line's own tools in the
inline form, with exactly `tool_name` and `tool_input` and at most a string `variant`,
and is counted as that tool. Each of Grok's built-ins in the table below is permitted
by the line's tool beside it, and every other Grok tool is refused. Such a call is
identified by its `_meta` name alone, so its title and kind may change, but a change of
that name, an added `toolName` or a changed `use_tool` target refuses the turn. A
permission request is judged by the name recorded for its call. A call without that
`_meta`, or in another namespace, is judged as before. **Hypothesis.** The table is
read from the tool names in the session's `tool_definitions.json`, and no live call of
each built-in, or of `use_tool`, has been recorded.

| Grok tool | Permitted by |
|---|---|
| `read_file` | `Read` |
| `list_dir` | `Read` or `Glob` |
| `grep` | `Grep` |
| `search_replace`, `write` | `Edit` or `Write` |
| `run_terminal_command`, `get_command_or_subagent_output`, `kill_command_or_subagent` | `Bash` |
| `web_fetch` | `WebFetch` |
| `web_search` | `WebSearch` |
| `search_tool` | Always, as discovery |
| `use_tool` | Only a tool of Dromon's MCP server, `dromon__NAME` in `tool_name` |
| Any other | Refused |

**Changed on 2026-10-26, Antigravity review, F1 to F3, at the operator's direction.**
Grok no longer exempts the `think` kind. A `think` call was let through uncounted, so
any call reported with that kind escaped the tool check. It now falls under Any other
in the table and is refused. Grok also judged only the `tool_call` notification, so a
`tool_call_update` could rename or change the kind of a call already let through. Every
update is now compared with what was recorded. **Hypothesis, not verified.** If Grok
reports its reasoning as `think` tool calls, or refines a call's title while it runs,
every such turn is refused. Both fail closed, and the first live turn should show
which kinds and updates Grok sends.

## Sandbox profiles

A line without `Edit` or `Write` runs under `dromon-reader`, and one with either runs
under `dromon-writer`. Dromon writes both into its Grok home's `sandbox.toml` when that
file is missing. The following paths are placeholders for the daemon home and Git
directories. This example assumes every listed credential path exists.

```toml
[profiles.dromon-reader]
extends = "read-only"
deny = [".dromon", "/placeholder/project/secret", "/placeholder/worktree/secret",
        "/placeholder/operator-home/.ssh", "/placeholder/operator-home/.aws",
        "/placeholder/operator-home/.codex/auth.json",
        "/placeholder/operator-home/.cargo/credentials.toml",
        "/placeholder/operator-home/.pi/agent/auth.json"]

[profiles.dromon-writer]
extends = "workspace"
read_only = ["/placeholder/worktree/gitdir", "/placeholder/repository/gitdir"]
deny = [".dromon", "/placeholder/project/secret", "/placeholder/worktree/secret",
        "/placeholder/operator-home/.ssh", "/placeholder/operator-home/.aws",
        "/placeholder/operator-home/.codex/auth.json",
        "/placeholder/operator-home/.cargo/credentials.toml",
        "/placeholder/operator-home/.pi/agent/auth.json"]
```

Before each turn Dromon checks that the profile extends `read-only` for a reader, or
`workspace` or `strict` for a writer, that its `deny` entries cover the state and runtime
directories and any existing credential paths as absolute paths under the daemon's
real home, never through `~`, that a writer's `read_only` entries cover both the
worktree's Git directory and the common Git directory, and that
the profile sets no key but `extends`, `deny`, `read_only` and `restrict_network`, so a
`read_write` grant is refused. The workspace is the line's working directory. Only the
home's `sandbox.toml` is read, and a project's own `.grok`, where its `sandbox.toml`
would live, refuses the turn, as the home section says. **Inference, not verified.**
That Grok reads `sandbox.toml` from `GROK_HOME`, as it reads `config.toml`, and that
the built-in `read-only` base denies every write to the workspace. The profile names are fixed, so Grok lines in different working
directories share one definition of each, whose entries must cover every one of their
Git directory and every protected directory by its real path. Dromon writes the file
for the first line only. If a profile already exists, refusal advice lists only missing
values to add to its arrays and says to leave other tables and entries as they are. If a profile
is missing, the advice gives its complete table. A new profile includes only credential
paths that exist when the file is written. If one is created later, the next turn is
refused and its refusal advice names the path. A credential path that is a symbolic
link is denied by its resolved target path. **Not verified.** Whether Grok matches a
deny entry through a symbolic link. Writer lines do not commit, so their Git
directories remain read-only. For a linked worktree, the `.git` pointer
file and `commondir` file cannot be held as profile entries. A writer could rewrite a
pointer during a turn. The next turn resolves it again and refuses unless the resulting
Git directories are already held read-only. Between the check and that next turn, the
pointer files themselves remain unguarded by the profile. The bridge below still runs
in the project root, where it finds the runtime directory. The shared credential list
is not complete, and a project may contain other stores. It includes `.grok/auth.json`
and `.claude.json`. Grok exempts only its own sign-in path, `.grok/auth.json`;
`.claude.json` remains a required deny entry. No claim is made that the listed paths
cover every credential store.
**Inference, not verified.** A `read_only` entry inside a writable workspace keeps it
read-only, as the field's name says.

Existing `secret` directories under both the project root and the line's working
directory are also required deny entries, by real absolute path. This covers a line
working in a worktree whose project root is elsewhere. Grok's profile has no
`writable_paths` setting in Dromon. **Not verified.** Whether the Grok profile blocks a
write through a symbolic link from the worktree to a target outside it. Dromon has no
evidence that this matches Codex's reported gap, and this change does not add an
external write grant to Grok.

## Dromon's tools

ACP has no tools hosted by the client, so a Grok line reaches its own tools, such as
`send_message`, through `dromon mcp --line LINE`, which Grok starts as a stdio MCP
server. The bridge connects to `lines.sock` in the runtime directory, mode 0600, which
the daemon always opens. A request there must carry the token the daemon issued for
that line, held only in memory, and it serves only that line's own tools. **Row 12 of
the T3 Code review, 2026-10-26.** The token is renewed as each turn starts Grok's
process and revoked as that process ends, which is within the turn. It does not expire
while the turn runs, and expires after an hour without use between turns.

**Rows 1 and 2 of the T3 Code review, 2026-10-26.** Grok runs under Dromon's
launcher, which ends Grok's whole process group and every descendant a walk of the
process table finds, which covers the nested groups a second implementation saw
Grok's tools start, or leaves them and records them under the `leave_background` stop
policy. Before this, Dromon signalled the agent process only. [`LINE_CONFIG.md`](LINE_CONFIG.md)
specifies the stop policy.

## Not verified

- That Grok honours `yoloMode` false, accepts `session/set_mode`, and names MCP tools
  as `server__tool` in `toolName`. Grok 1.0.70 reports `models` and `configOptions`
  and no `modes`, as the probe showed.
- That a cached sign-in is a subscription rather than usage billed per token. Dromon
  treats it as a subscription, and refuses the API strategy.
- That `x.ai/mcp/servers_updated` and `x.ai/mcp/init_progress` together report
  every server a session loads, and that Grok sends the list on `session/load` as on
  `session/new`. A Grok that does not is refused.
- That Grok's permission grants remembered for a project do not approve calls Dromon
  would refuse.
- That Grok runs a session in the mode of Dromon's Grok home, which it cannot report,
  and which of that mode and a Claude Code `defaultMode` it follows.
- That Grok's own sign-in leaves the home's `config.toml` as Dromon wrote it, and
  that Grok writes its policy and trust files there empty, as it does in the
  operator's home. A Grok that writes more is refused until the file is removed.
- That Grok finds the home directory through `HOME` rather than the account database,
  that it writes none of the refused harness entries into the line's home directory,
  and that its sign-in and its interactive grants depend on `GROK_HOME` alone.

## Verify first

**Re-review, N1, at the operator's direction on 2026-10-26.** These are the checks for
the first live Grok turn, in order. Each rule they test fails closed, so a wrong guess
refuses turns rather than permits them.

**Established by the probe of 2026-10-27.** Grok's answer shapes for `initialize` and
`session/new`, the notices it sends while a session opens, and that it loads the
user's MCP servers and reports no permission mode.

0. **Sign-in.** The operator signs Grok in to Dromon's Grok home once, by hand, with
   the sign-in of Grok's authentication guide and the `GROK_HOME` of its configuration
   guide, and with `HOME` set as a turn sets it, so that the sign-in reads none of the
   operator's other harness files and keeps anything it writes under `HOME` where a
   turn finds it. The command creates that directory first, owner-only.

   ```sh
   mkdir -p -m 700 "$HOME/.grok/dromon-home/home" && GROK_HOME="$HOME/.grok/dromon-home" HOME="$HOME/.grok/dromon-home/home" grok login
   ```

   `grok login --device-auth` is the guide's form for a machine without a browser.
   Afterwards confirm that the line's home directory holds none of the entries the
   home section refuses, and that the home's `config.toml`, if Grok wrote one, is
   Dromon's or absent, and run the probe. It should show `x.ai/mcp/servers_updated` listing only
   Dromon's server, an `x.ai/mcp/init_progress` total of no more than that, and
   `defaultAuthMethodId` `cached_token`. While a Claude Code settings file that Grok
   reads names a mode other than `default`, Dromon refuses before Grok starts, as the
   home section says.

1. **Model selection.** On the first live turn, record the model in the `session/new`
   response and confirm that a mismatch causes `session/set_config_option` to request
   the exact identifier from the driver's model setting. Confirm that the complete
   updated option list reports the requested model before Dromon sends the prompt.
   Repeat with a resumed session to confirm that `session/load` follows the same rule.
   A refused request or a response naming any other model must stop the turn.
2. **MCP meta-tools.** Run a first live line turn that uses `search_tool` and then calls
   one Dromon tool, such as `send_message`, through `use_tool`. Record `toolName`,
   `kind`, `rawInput`, every update and the permission request. Confirm that discovery
   exposes only Dromon's server, and that the call's `rawInput` is the documented inline
   `tool_name` and `tool_input` object. Confirm that Dromon counts the underlying tool
   name toward its budget and gate. The ACP call shape has not been observed, so any
   difference is refused and must be reviewed before this check is loosened.
3. **Kinds and updates.** Record the `kind` of each tool call a plain turn and a tool
   turn carry, whether any arrives as `think`, and whether a later update repeats,
   omits, adds or changes `toolName`, `title` or `kind`. A `think` call, an added field
   and a changed field each refuse the turn.
4. **Cancel.** Interrupt a turn and confirm that Grok answers its prompt with
   `stopReason` `cancelled` within `ABORT_SECONDS`, 60 seconds, after which Dromon
   stops Grok.
5. **Compatibility switches.** **Inferences, review of read-only seats, second round,
   finding 4.** Dromon sets each of these to `0`, which Grok's guide implies turns a
   scan off without giving the value.

   | Variable | Value | Inferred meaning |
   |---|---|---|
   | `GROK_CLAUDE_MCPS_ENABLED` | `0` | Claude Code's MCP servers are not loaded |
   | `GROK_CURSOR_MCPS_ENABLED` | `0` | Cursor's MCP servers are not loaded |
   | `GROK_CLAUDE_HOOKS_ENABLED` | `0` | Claude Code's hooks are not loaded |
   | `GROK_CURSOR_HOOKS_ENABLED` | `0` | Cursor's hooks are not loaded |

   Confirm each by placing a server or a hook of that kind in a scratch home given as
   `HOME` and checking that Grok neither lists the server nor runs the hook. The
   home's `config.toml` sets the same cells to false, which `check_home` compares, and
   the line's home directory now holds no Claude Code or Cursor files, so a wrong
   value would expose only a project's `.claude` or `.cursor` files, and the servers
   Grok reports are checked in any case.

## Not at parity

- No MCP servers of the line's own, no sandbox or settings documents, and no
  `project_settings`, which loads Claude Code's settings.
- No network blocking for a Grok line's commands on macOS.
- Grok reports no cost, and the driver's `max_turns` is not enforced.
