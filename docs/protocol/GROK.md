# Grok Build Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative for Dromon's side. The Python module
`dromon.grok` implements it against Grok Build 1.0.70. **Not verified against a live
turn.** An operator probe on 2026-10-27 created a session, which Dromon refused before
any prompt was sent, as the probe section below records. Dromon's own Grok home, which
that probe called for, was built the same day and has not yet run. **Fact, observed on
2026-11-01** by the operator, under the daemon at commit fb11f22, on grok-4.7. After the
`--deny` hotfix and a regenerated Grok home configuration with the `PreToolUse` hook, a
line named grok-review ran turns again, resumed a session and called `read_file`,
`use_tool`, `search_tool` and `run_terminal_command`. The sections below record which
points that run settled and which it did not.

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

## Refusal explanations

The Agent Client Protocol version 1 `RequestPermissionResponse` selects an offered
option identifier or reports cancellation. It has no standard field for a reason.
The local T3 Code schema in `packages/effect-acp/src/_generated/schema-v1.gen.ts`
confirms this shape. Its metadata is an extension point whose interpretation cannot
be assumed. A JSON Remote Procedure Call error is not a documented way to deliver a
tool refusal to the model. Dromon therefore sends neither invented reason fields nor
an extra prompt while a turn is active.

Ordinary denials include tools absent from the line configuration, malformed
`use_tool` inputs and image requests that fail the configured image policy. They are
rejected once when the runtime asks before execution and offers a rejection option.
The runtime controls what the model learns from that selection. Whether Grok shows
a useful denial message and continues is not verified live. Dromon's own tools return
explanatory Model Context Protocol errors for invalid arguments, disclosure refusals
and other call failures without ending the turn. Disclosure errors never name a term.

Identity changes, reused identifiers, execution without approval, execution after
rejection, missing permission options, model or permission mode drift, undeclared
servers, unsafe protocol messages, deadlines and driver budgets keep their turn stop.
When a refusal raises an engine error, or a driver interruption reports its cause,
Dromon records a bounded harness note for the next prompt. This note survives a
restart and does not wake the line or release a hold. A later reported turn retires
it, while an unreported turn leaves it owed. The note uses only a closed vocabulary owned by
Dromon. Engine labels, titles, identifiers and error text are never interpolated.
Unrecognized causes receive a generic safety or protocol explanation. Persisted
notes from older versions are classified again before display. The original refusal
or driver cause survives a connection failure or protocol error
during cancellation. Priority mail and restart interruptions retain their existing
notices.

Fixed stop notices contain only public Dromon wording and are not screened against
the disclosure list. Their wording never depends on that list, so observing a notice
reveals no list membership. They consume no line disclosure allowance.
Line-originated disclosure checks retain their existing allowance.

## Evidence

## Provider budgets and quota errors

**Facts, read on 2026-10-31** in Grok Build's bundled guides. The `StopFailure` hook
receives an `error` category. Grok documents `rate_limit`, `authentication_failed`,
`invalid_request`, `server_error`, `max_output_tokens` and `unknown`. Capacity failures
with HTTP status 503 or 529 are categorized as `rate_limit`. This category does not
distinguish a spent plan from a transient rate limit. The hook input may also carry
`errorDetails`, but the guide does not define a quota-exhaustion category, a reset field
or a mapping from those details to a reset time.

**Fact.** Those hook fields are not a documented ACP `session/update` or
`session/prompt` response. Dromon's Grok home registers a tool denial hook, and the ACP adapter
reads no hook event. A generic JSON-RPC error therefore does not establish that a plan
is exhausted, even if its text contains `quota` or `rate_limit`. Dromon classifies no
new Grok plan limit and extracts no reset time. The turn still fails as an ordinary
engine refusal, which does not fail a role over.

**Facts, read on 2026-10-31.** Grok's `/usage` view in the interactive interface shows
the account allowance, while `grok usage` reads persisted session token and cost
totals. The local guide does not describe an ACP-accessible account allowance. Dromon
uses ACP in a non-interactive process, so neither surface provides a remaining provider
budget it can read without spending.

The following table records whether each engine reports a remaining provider budget on
a surface Dromon can read without spending. A window's token or request utilization is
not treated as a provider budget unless the engine's documented report identifies that
budget.

| Engine | Readable budget evidence | Dromon behavior |
|---|---|---|
| Claude Code | The Agent SDK types and recorded probe expose utilization and reset time for a rate-limit window. | Dromon records a positive remainder for the reported window. It does not combine separate windows. A rejected window is handled by the existing plan-limit path after a failed turn. |
| Codex | The recorded no-credit notice reports `hasCredits` false and `unlimited` false, but no remaining percentage. No positive balance shape is recorded in the repository fixtures. | Dromon records the observed no-credit state and reads a reset from the usage-limit message wording, under the local-time assumption in `CODEX.md`. It does not infer a positive balance. |
| Grok Build | The interactive `/usage` view includes account allowance. ACP and `grok usage` do not document a readable remaining provider budget. | No remaining provider budget or quota-error shape is wired. |
| Pi | Reports per-call token usage and estimated cost, not remaining provider allowance. | No remaining provider budget is wired. |
| Cursor Agent | Its protocol reports no token usage or cost. | No remaining provider budget is wired. |
| Antigravity | Its ACP `usage_update` reports context and token use, and may report session cost. | No remaining provider budget is wired. |

**Limit.** The repository has no recorded Grok quota failure. The documented hook
category is not evidence of what the ACP adapter receives, so a live Grok turn or a
recorded ACP response would be needed before adding a classifier. This task uses no
live probe and spends no Grok usage.

**Facts, read on 2026-10-25** in Grok Build's bundled guide, `~/.grok/docs/user-guide`,
and its `README.md`.

- `grok agent stdio` runs the agent over ACP. Agent options such as `--model` and
  `--reasoning-effort` precede the transport, and `--no-leader` starts a local agent.
- `session/new` and `session/load` take `cwd`, `mcpServers` and `_meta`, whose
  `yoloMode` and `autoMode` choose always-approve and auto mode. Rules are documented
  for session creation, with retention on load discussed below. `session/prompt` streams `session/update`
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
  Grok's asking mode, declares no MCP server, installs a fixed denial hook and sets the `mcps` and
  `hooks` cells of `[compat.claude]` and `[compat.cursor]` to false. The
  `sandbox.toml` holds the two profiles below, written for the line that first needs
  them.
- **What Dromon refuses.** Before each turn, and before Grok starts, Dromon refuses a
  home that is a symbolic link or may be written by other users, a `config.toml` whose
  parsed contents differ from Dromon's in any key, a `managed_config.toml` or
  `requirements.toml`, in the home or in `/etc/grok`, that sets anything, a
  `trusted_folders.toml` that trusts any folder, and any of `agents`, `hooks`,
  `hooks-paths`, `disabled-hooks`, `installed-plugins`, `lsp.json`, `plugins`, `rules`, `skills` or
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
   request for an identifier already answered in the turn is refused.
9. **Permission requests** are answered with an `allow_once` option for a permitted call
   and a rejection option otherwise. An ordinary policy denial leaves the turn
   running. An explicitly `pending` unauthorized call waits for its permission
   request. Every update after rejection must carry `_meta.toolNotRun` equal to
   the Boolean `true`. Status alone does not establish non-execution. A rejected
   call reported as `pending`, `failed`, completed or unknown without that marker
   stops the turn, as does missing status without the marker. An execution status
   or unknown status contradicting the marker also stops the turn. The marker is
   an engine assertion that Dromon trusts to mean the rejected call did not run.
   It is not independent proof that no side effect occurred. A runtime that falsely
   asserts non-execution can therefore leave the turn running. Rejection
   before execution depends on the runtime honoring its permission answer.
   An unauthorized call with no explicit
   pending status, an identity change, a reused identifier, a permission request for
   a call no longer pending, a missing permission option or the driver's tool budget
   sends `session/cancel`, and every
   permission request after it is answered as cancelled. If the runtime offers no
   option of the required kind, Dromon answers as cancelled, cancels and refuses the
   turn with bounded evidence naming the tool, its intended decision and the offered
   option kinds and identifiers. It never substitutes `allow_always` for `allow_once`.
   Other requests from the agent are answered with an error.
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
inline form, with exactly `tool_name` and `tool_input` and at most a `variant` of
`UseTool`, and is counted as that tool. Each of Grok's built-ins in the table below is permitted
by the line's tool beside it, and every other Grok tool is refused. Such a call is
identified by its `_meta` name alone, so its title and kind may change, but a change of
that name, an added `toolName` or a changed `use_tool` target refuses the turn. A
permission request is judged by the name recorded for its call. **Review G1 and G2,
2026-10-31.** A `kind` the call carries, at first sight or in any later update, must
be one the table below gives that tool, so a `_meta` naming one tool cannot carry a
kind that names another, even when the line has both. **Fact, observed on 2026-10-31**
in a Grok line turn of 58 tool calls, from each `tool_call_update`: `read_file` as
`read`, `grep` as `search`, `run_terminal_command` as `execute`, and `list_dir`,
`search_tool` and `use_tool` as `other`. Twenty-six `use_tool` calls of Dromon's own
tools passed in that turn. `list_dir` was first refused for `other`, which the table now
allows. A later turn on 2026-10-31 showed `get_command_or_subagent_output` as `other` too, with Grok's
`_meta` kind `background_task_action`, which `kill_command_or_subagent` shares, so `other`
is allowed for both, observed for the first and inferred for the second. In a later turn on 2026-10-31, nineteen inline `use_tool` calls passed, and one in the
file-backed form, with `tool_input_file`, was refused, as intended, since Dromon cannot see
arguments Grok reads from a file only after the call is judged. Its refusal now says to pass
the arguments inline. Grok wrote that file under `/tmp`, which the `workspace` base of the
writer profile lets it write. The kinds of the other built-ins are a **hypothesis**, and a call with another
kind is refused, which may refuse too much and never grants more. A `_meta` name
added in a later update to a call first judged without one refuses the turn. A call without that
`_meta`, or in another namespace, is judged as before. **Hypothesis.** The table is
read from the tool names in the session's `tool_definitions.json`, and no live call of
each built-in, or of `use_tool`, has been recorded.

| Grok tool | Permitted by | Kinds it may carry |
|---|---|---|
| `read_file` | `Read` | `read` |
| `list_dir` | `Read` or `Glob` | `read`, `search`, `other` |
| `grep` | `Grep` | `search` |
| `search_replace`, `write` | `Edit` or `Write` | `edit` |
| `run_terminal_command` | `Bash` | `execute` |
| `get_command_or_subagent_output`, `kill_command_or_subagent` | `Bash` | `execute`, `other` |
| `web_fetch` | `WebFetch` | `fetch` |
| `web_search` | `WebSearch` | `fetch`, `search` |
| `search_tool` | Always, as discovery | `other` |
| `use_tool` | Only a tool of Dromon's MCP server, `dromon__NAME` in `tool_name` | `other` |
| Any other | Refused | |

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
read_only = ["/usr/bin"]
deny = [".dromon", "/placeholder/project/secret", "/placeholder/worktree/secret",
        "/placeholder/operator-home/.ssh", "/placeholder/operator-home/.aws",
        "/placeholder/operator-home/.codex/auth.json",
        "/placeholder/operator-home/.cargo/credentials.toml",
        "/placeholder/operator-home/.pi/agent/auth.json"]

[profiles.dromon-writer]
extends = "workspace"
read_only = ["/usr/bin", "/placeholder/worktree/gitdir", "/placeholder/repository/gitdir"]
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
worktree's Git directory and the common Git directory, that the project's
`read_only_paths` are held by `read_only` for each line's workdir, and that
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

## Viewing an image

**Round 11.** No line could look at an image, because Grok's image tools were refused.
Grok's tools were searched on 2026-10-31 in the `tool_definitions.json` of its session
logs and in `~/.grok/docs`.

- **Fact.** `read_file` says "The tool can read PDF, PPTX, and image files. You see
  image files as images." It is the only tool that shows a local image to the model.
- **Fact.** `image_gen`, `image_edit`, `image_to_video` and `reference_to_video`
  generate or edit, and Grok's configuration reference names `features.image_gen` and
  `features.image_edit`. They are not in `GROK_TOOLS`, so they are refused for every
  line, `ViewImage` included.
- **Consequence.** `ViewImage` on a Grok line permits `read_file`, with its kind `read`
  in `GROK_TOOL_KINDS`. Because `read_file` also reads text, Dromon narrows it for a
  line that has `ViewImage` and not `Read`. Its `rawInput` `target_file` must pass
  `imageaccess.image_refusal`, which requires a file inside the working directory and
  outside `.dromon`, `.git` and `secret`, a regular file, a png, jpg, jpeg, gif or webp
  by name and wholly one complete container of that type, and at most
  `MAX_IMAGE_BYTES`, 5 MiB. A line with `Read`
  already reads any file its profile allows, so `ViewImage` does not narrow it, and the
  call counts as `Read`.
- **Do not enable it yet.** `ViewImage` is not to be enabled for a Grok line until the
  verify-first item 6 below shows that Grok's first `read_file` notice carries its
  input. Until then it fails closed, and a line given it cannot view any image on Grok.
  **Review of d00da44, I2.**
- **Where it is enforced.** At the first notice of a call, which is refused unless it
  carries its input, since `read_file` also serves text and could finish before a
  later update names the file. **Review of 1045994, I2.** At a later update that brings
  the input, which cancels the turn, and at the permission request, which is denied
  unless it names an acceptable image. A request without a `rawInput` is denied. If
  Grok's first notice never carries the input, a line without `Read` cannot view an
  image on Grok, which fails closed, and the verify-first item 6 below shows whether it does.
- **Limits.** Whether Grok asks permission before `read_file` is not known, and without
  a request only an update carrying the input can stop the call, after Grok has begun
  it. The check and the read are two steps. Grok resizes and prices the image, and
  Dromon bounds bytes and not dimensions. Whether Grok delivers an image to the model
  for the model its driver pins is also not known.
- **Inference, not observed.** That the input names the file `target_file`, as the
  tool's definition does. A different key denies every view of a line without `Read`.

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

6. **Viewing an image.** **Round 11.** Make a small synthetic image in a scratch
   project, for example a 64 by 64 png of one colour with a made up word drawn on it,
   far under 5 MiB. Create a Grok line there with `builtin_tools` of `["ViewImage"]` and
   ask it to view the file and report the word. Record `toolName`, `kind`, `rawInput`,
   every update and every permission request of the call. Confirm that the call is
   `read_file` with kind `read`, that `rawInput` has `target_file`, that the first
   notice of the call carries `rawInput` and not only a later update, whether a request
   reached Dromon before the read and whether it carried `rawInput`, and that the line
   reports the word. Then repeat with a `.txt` file, a png over 5 MiB, a path outside
   the working directory and a path under `.dromon`, and confirm that each is refused.
   Repeat with a line without `ViewImage` and `Read` and confirm that the view is
   refused. If no request arrives for `read_file`, record that the check is after
   the fact for this tool.
7. **Permission options.** On a permitted call of one of Dromon's tools through
   `use_tool`, record every `session/request_permission` option's `kind` and
   `optionId`. Repeat with a call the line is not allowed to use. Confirm that Dromon
   chooses only `allow_once` to permit and a rejection option to refuse, and that a
   request with only unexpected kinds is answered as cancelled and refuses the turn.
   In particular, confirm that Dromon never chooses `allow_always` to replace
   `allow_once`. **The ACP permission option list has not been recorded on a live Grok
   call.**

## Not at parity

- No MCP servers of the line's own, no sandbox or settings documents, and no
  `project_settings`, which loads Claude Code's settings.
- No network blocking for a Grok line's commands on macOS.
- Grok reports no cost, and the driver's `max_turns` is not enforced.

## Rules retained by sessions

**Fact.** The bundled guide `15-agent-mode.md` documents `_meta.rules` under optional
fields on `session/new`. `17-sessions.md` documents loading a persisted session but
does not promise replacing its rules. Round 14 operator mail reports resumed rules
remaining unchanged. No live exchange independently verifies that report here.

**Inference.** Passing current rules to `session/load` is insufficient evidence that
they apply. Dromon stores the session identifier followed by `:rules-` and the full
Secure Hash Algorithm 256 digest of the rendered instructions, tool switches, built-in grants, allowed tool
patterns, tool policy and Dromon tool names supplied at creation.
It loads only when the current digest matches. A changed or absent digest creates a
new session and explains the replacement in the first prompt. The mailbox, standing
brief and files remain. Legacy sessions are replaced once because their rules cannot
be established. The digest records the creation snapshot even if an instruction file
changes during the turn. Restoring earlier rules does not restore earlier context.
A valid replacement identifier is cached on the adapter before model, permission
and prompt checks can refuse the turn. A retry loads that replacement rather than
creating another session. If replacement fails before yielding a valid identifier,
the adapter permits only one attempt for that digest and requires operator recovery.
The digest also covers the generated home configuration. This bound is local to the adapter. The scheduler persists a session identifier only
from a successful turn report, so adapter reconstruction or a daemon restart can
allow a new replacement attempt after a failed turn.

## Profile checks before startup

**Fact.** Every effective Grok line is checked after project declarations are applied
to the event log during the shared daemon start and restart preflight. This includes
lines created through clients. The check reads profiles without creating homes or
starting engines. A refusal names the line and the profile table or missing array
entries. The required paths include project read-only paths and both fleet indexes.
Protected paths require denial even before they exist. Turn-time checking remains
necessary because paths and registered peers can change after preflight.

## Tool advertisement filtering

**Fact.** The following bundled Grok Build 1.0.70 guides were checked on 2026-11-01.

- `05-configuration.md` documents `GROK_CONFIG` for `agent stdio`, with a limited
  overlay allowlist, and the direct `GROK_SUBAGENTS` and `GROK_WEB_FETCH` switches.
- `16-subagents.md` explicitly says the subagent disable controls apply to
  interactive, headless and `grok agent stdio` invocations. It documents
  `GROK_SUBAGENTS=0` and `[subagents] enabled=false`.
- `26-config-reference.md` documents `subagents.enabled` as the task tool master
  switch and `features.web_fetch` with its `GROK_WEB_FETCH` equivalent.
- `14-headless-mode.md` documents `--tools` and `--disallowed-tools` for terminal
  and headless use, including the model name `spawn_subagent` for the task tool.
- `15-agent-mode.md` does not list general tool filtering flags. Its agent profile
  option does not load the file on the daemon route. The `tools` frontmatter example
  in `16-subagents.md` describes custom agents and does not establish that this
  option can filter the default main agent on Dromon's transport.
- `18-sandbox.md` describes file and process restrictions rather than an advertised
  tool allowlist. The reference and the guides contain no established `todo_write`
  disable switch for `agent stdio`.

**Fact, before the generated hook change.** Read-only inspection of Dromon's Grok home found only asking mode and
compatibility settings in `config.toml`. The `dromon-reader` and `dromon-writer`
sandbox profiles contain `extends`, `deny` and `read_only`. Policy and folder-trust
files are present. Dromon's existing validation requires them to be empty and refuses
custom agents, hooks and discovery entries. No credential file was read.

**Fact.** Dromon now sets `GROK_SUBAGENTS=0` in every Grok process because it does
not support granting `Agent` on this engine. It sets `GROK_WEB_FETCH=0` when the
line lacks `WebFetch`, and `1` when granted. These documented process switches are
selected from the line configuration and replace inherited values. They require no
shared home edits and cannot change another line's process environment.

**Fact.** The persisted rules digest includes every tool grant as well as these
switches. A change in `Read`, `WebFetch`, allowed tool patterns, tool policy or the
Dromon tool set starts a fresh session. Tests cover inherited value replacement,
per-line web fetch grants, changed grants, matching-digest reuse and refusal retries.

**Fact.** Dromon individually rejects permission requests for unavailable
`todo_write`, `spawn_subagent` and `web_fetch`. Parameterized exchanges verify that
the turn can continue to an allowed call when each later update explicitly reports
Boolean `toolNotRun=true`. Each rejected identifier must receive its own confirming update. A missing report
at turn end cancels the turn and raises a refusal naming the tool, which holds the
line because non-execution is uncertain. Permission denial alone does not prove that a tool had no effects.

**Fact, observed live on 2026-11-01** by the operator, on grok-4.7 under the daemon at
commit fb11f22, for a line named grok-review with `builtin_tools` of Bash, Glob, Grep and
Read. The tool list the engine offered holds `web_search`, `open_page`,
`open_page_with_find`, `x_user_search`, `x_semantic_search`, `x_keyword_search`,
`x_thread_fetch`, `run_terminal_command`, `read_file`, `search_replace`, `list_dir`,
`grep`, `kill_command_or_subagent`, `todo_write`, `get_command_or_subagent_output`,
`scheduler_create`, `scheduler_delete`, `scheduler_list`, `monitor`, `search_tool`,
`use_tool`, `workflow`, `enter_plan_mode`, `exit_plan_mode`, `ask_user_question`,
`send_feedback`, `image_gen`, `image_edit`, `image_to_video`, `reference_to_video` and
`write`. It holds neither `spawn_subagent` nor `web_fetch`, so `GROK_SUBAGENTS=0` and
`GROK_WEB_FETCH=0` take effect live. `todo_write` is still advertised. Asked to call it,
the model's inference ended at 09:21:00.881Z and Dromon's cancel reached Grok at
09:21:00.883Z, and Grok's log has no `exec_done` for `todo_write`. Dromon held the line
with "the engine used a tool the line was not given: todo_write". **Inference.** The call
did not run. Whether the hook alone would have denied it is not shown, since Dromon's
notice check acted first. The line status at that point read daemon and daemon checkout
fb11f22, workdir 47c58d5 and stale false.

**Unverified, remaining.** No supported advertisement control was established for `todo_write`. A transport-specific
filter or an independently verified custom main-agent configuration is still needed
to remove that tool from the advertised set. The current individual denial remains. The permissions guide lists `todo_write`
among operations that never prompt by default unless a deny rule or hook blocks
them. Its Tool Names section recognizes only `Bash`, `Read`, `Edit`, `Write`,
`Grep`, `Glob`, `MCPTool`, `WebFetch`, `WebSearch` and `*`, and says unrecognized
rule names are skipped. A bare `todo_write` or `spawn_subagent` deny entry therefore
has no established enforcement under this guide. The operator authorized a documented `PreToolUse` hook instead.

**Fact.** The generated `config.toml` now contains one `hooks.PreToolUse` matcher
for `todo_write|spawn_subagent`. It invokes `/usr/bin/printf` with a separate `args`
array to emit a fixed JSON denial. The hook reads no input, selects no program from
`PATH`, starts no shell and loads no script from a line. The adapter checks that the
program and its parents are owned by the system and cannot be written by other users.
It refuses a workspace that contains the program, and both sandbox profiles must
hold the program directory read-only. Saved hook-disable entries are refused. Tests
execute the generated command with hostile input and `PATH`, refuse a program under
the workspace, refuse a missing executable, reject config drift and require sandbox
protection of the program directory. These are adapter and local command tests.
They do not establish live Grok enforcement.

**Fact.** The home is shared by concurrent lines. A `WebFetch` deny in that file
would also block a line granted `WebFetch`, because deny rules win over allow rules.
Dromon therefore writes no `WebFetch` deny rule. It sets `GROK_WEB_FETCH=0` for a
line without the grant and refuses a `web_fetch` call one at a time. **Fact, observed
by the operator on 2026-11-01.** The permissions guide documents a command-line rule
`--deny`, but Grok Build 1.0.70's `grok agent` refuses it with "unexpected argument
'--deny' found" and exits with status 2. Passing it closed every Grok turn of a line
without `WebFetch`, so Dromon no longer passes it.

**Fact.** The hook guide says a command with an `args` array starts no shell, a
matching explicit denial blocks, and startup errors, crashes, timeouts and malformed
output fail open. There is no documented fail-closed handler option. Dromon refuses
before launch when its program or protection checks fail. It cannot force Grok to
block a call after a hook failure inside Grok. The permission-response and update
checks remain necessary, and cannot undo a call that already ran.

**Unverified.** The guide gates user configuration hooks on folder trust and says
headless runs count the folder as trusted for their own hooks. Whether the particular
`agent stdio` launch activates this hook without a saved trust record requires the
operator's live check after restart. Dromon does not disable folder trust or introduce
a saved grant to make the hook run. A live check must establish that the hook loads,
matches both tool names and blocks their calls. Its effect remains unverified. The live
run of 2026-11-01 above shows the line ran turns with the regenerated configuration,
and does not show the hook acting.
Existing home config and profiles are not rewritten. An older config must be removed
for Dromon to regenerate it, and the reported program-directory entry must be added
to both existing profiles before turns can run.


## Model selection after loading a session

**Fact.** Round 14 operator mail reports that Grok 1.0.70 returned a model other
than `grok-4.7` after loading the grok-review session. The operator did not identify
the returned model. Dromon can read it from the session response and checks that all
reported model fields agree. A failed model selection now includes that reported
model in its refusal message.

**Fact.** Dromon now sends `session/set_config_option` with `configId` equal to
`model` and a string `value` equal to the driver model. Previously it sent a nested
object. The operator observed error code `-32602` naming `SessionConfigOptionValue`
for that object. The bundled guides `15-agent-mode.md` and `17-sessions.md` show the
nested form, which conflicts with this runtime report.

**Fact.** The operator supplied schema examples fetched from
[ACP version 1](https://agentclientprotocol.com/protocol/v1/session-config-options)
and [ACP version 2](https://agentclientprotocol.com/protocol/v2/session-config-options).
Both use bare select strings. Version 2 also requires `type` equal to `id` for a
select. Version 1 permits the bare string without that field. Dromon requests
version 1 at initialization, records the reported version on the adapter and
refuses any other version before opening a session. No version 2 compatibility is
claimed. Scripted replies report version 1. No new live initialization was captured
for this follow-up.

**Fact.** Before changing a mismatching model, Dromon requires the requested value
in the reported model options, including grouped options. Otherwise it refuses
before selection or prompting and reports the current model and listed values.
Tests cover the exact request shape, a resumed session with a matching rules digest,
missing requested values and version 2 refusal. **Fact, observed on 2026-11-01** by the operator, on grok-4.7 under the daemon at
commit fb11f22. The model pin on a resumed session did not fail, so the bare string was
accepted or was not needed, which the run does not distinguish.

**Fact.** The persisted session identifier includes a 71 byte rules suffix. Dromon
refuses an identifier whose encoded form would exceed 256 bytes before sending a
prompt. Tests cover the boundary and a multibyte identifier.

**Fact.** Project reload can introduce Grok lines or change their working directory,
read-only paths or profile requirements. It checks prospective declared configurations
with operator overrides and existing client-created lines before mutating daemon
state. Invalid profiles refuse the reload. Turn-time checks remain necessary.
