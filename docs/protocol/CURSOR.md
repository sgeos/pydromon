# Cursor Agent Engine

Cursor Agent refuses a line configured with `workdir_access: read`, because its
adapter cannot confine writes to the working directory.

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-26. Normative for Dromon's side. The Python module
`dromon.cursor` implements it against Cursor Agent 2026.10.01. **Not verified against a
live turn. A daemon refuses every Cursor line, and only `dromon probe
--accept-partial-isolation` runs a Cursor turn**, as the operator decided on
2026-10-26.

## Purpose

**Directed by the operator on 2026-10-26.** A prospective adapter, ready for one live
probe turn. A line whose driver names the
`cursor-agent` engine runs each turn as `cursor-agent --print --output-format
stream-json`, which writes one JSON event per line and exits after its `result` event.

## Model names

Dromon names a Cursor model `cursor-agent:ID`, with Cursor's own identifier, as
[`DRIVER.md`](DRIVER.md) records. The packaged model is
`cursor-agent:gpt-5.6-luna-medium`, which Cursor lists as `GPT-5.6 Luna 1M`. Cursor
names the effort in the model identifier, so a Cursor line takes its driver's effort and
refuses a different one.

## Evidence

**Facts, observed by the operator on 2026-10-26.** `cursor-agent --version` printed
`2026.10.01-e373342`, `cursor-agent --help` listed the options below, and
`cursor-agent models` listed the account's models with their display names. The help
lists no `acp` command, which [`../architecture/DESIGN.md`](../architecture/DESIGN.md)
had recorded from earlier research.

**Facts, from the operator's summary** of `cursor.com/docs/cli/reference/output-format`,
fetched on 2026-10-26. The stream's events are `system` with subtype `init`, `user`,
`assistant`, `tool_call` with subtypes `started` and `completed`, and a terminal
`result`. A failed run may end without a `result`. No cost or token usage appears.

**Facts, read on 2026-10-26** in the installed code under
`~/.local/share/cursor-agent/versions/2026.10.01-e373342`.

- The `init` event is written with `apiKeySource` set to the key's source or, when no
  key was given, `login`, with `model` set to the model's display name, and with
  `permissionMode` always `default`, so that field carries no information.
- Thinking is written as `thinking` events with subtype `delta`, although the
  documentation says print mode suppresses it.
- A completed shell `tool_call` may carry a snapshot of the process environment, so
  Dromon never stores an event.
- An MCP call is a `mcpToolCall` whose arguments hold `providerIdentifier` and
  `toolName`.
- The configuration directory is `CURSOR_CONFIG_DIR`, else `XDG_CONFIG_HOME/cursor`,
  else `~/.cursor`, and holds `cli-config.json`. The default configuration allows
  `Shell(ls)`, with `approvalMode` set to `allowlist` and the sandbox disabled.
- Permission rules are `Read(GLOB)`, `Write(GLOB)`, `Shell(COMMAND)`,
  `WebFetch(DOMAIN)` and `Mcp(SERVER:TOOL)`. The matcher turns each `*` into any run of
  characters over the whole path, so `Read(*)` matches every path. A read is checked
  against the deny rules before it runs and refused as `permissionsConfig`.
- A `.cursor/cli.json` is merged from each directory between the git root and the
  working directory. The user's MCP servers are read from `~/.cursor/mcp.json` in the
  home directory whatever the configuration directory. A plugin directory may hold an
  `mcp.json`, and MCP configurations interpolate `${env:NAME}`.
- **Cursor review, F2.** The code also names, under a project's `.cursor`,
  `sandbox.json`, `settings.json`, `environment.json`, `worktrees.json`,
  `mcp-approvals.json`, `sandbox-policies`, and the directories `rules`, `agents`,
  `commands` and `skills`. What each controls was not read.
- **Cursor review, F7.** The data directory is `CURSOR_DATA_DIR`, else `~/.cursor`, and
  holds per-project data under `projects`.
- The installed code reads `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
- On macOS the login is kept in the keychain, or with `AGENT_CLI_CREDENTIAL_STORE` set
  to `file` in `~/.cursor/auth.json`, neither in the configuration directory.
- Print mode offers about forty built-in tools, among them read, grep, glob, list,
  shell, edit, delete, task subagents, semantic search, web search, web fetch, image
  generation, screen recording and MCP resource tools. No option switches them off.

## Isolation, decided by the operator on 2026-10-26

Cursor's tools fall into two groups, by what Dromon can show from the installed code.

| Group | Tools | How Dromon holds them |
|---|---|---|
| Denied before they run | read, grep, glob, list and other file reads, by `Read(*)`. Edit, write and delete, by `Write(*)`. Shell and shell input, by `Shell(*)`. Web fetch, by `WebFetch(*)` | Cursor checks the rule before the tool runs. Shown for read, grep, glob and list, which the code checks against the read rules before running, and for commands, which it checks against the shell rules. Inferred for writes and web fetches |
| Stopped only after they start | Task subagents, semantic search, web search, image generation, screen recording, computer use, lint reading, MCP resource tools, plan, todo and any other tool | No rule Dromon can show covers them. Dromon sees the `started` event, ends Cursor's whole process group and refuses the turn, after the tool has begun |

**Decision.** A daemon refuses a Cursor line when it is created or configured, so no
Cursor line runs unattended. `dromon probe --model cursor-agent:gpt-5.6-luna-medium
--accept-partial-isolation` runs one turn with the deny rules, Cursor's sandbox
enabled, and a stop and refusal on any tool, since a probe has none of Dromon's tools.
Without the flag the probe refuses with this reason, and the flag is refused for any
other engine. Dromon never passes `--force`, `--yolo`, `--approve-mcps` or `--api-key`.
**Re-review, N4.** The flag does not change the strategy, so `--strategy api` keeps the
operator's key and bills per token, as the probe's help says.

## A probe turn

1. **Configuration.** The line must have no built-in tools and none of the
   configurations listed below. **Re-review, N1.** A line created on a chat, or a
   line's second turn, is refused before Cursor starts, since the turn's data
   directory of step 4 is removed with the turn and holds no earlier chat.
2. **Environment.** `CURSOR_API_ENDPOINT` and `CURSOR_API_URL` are refused. For a
   subscription, Dromon removes `CURSOR_API_KEY`, `CURSOR_AUTH_TOKEN` and its expiry,
   every variable of a cloud provider by its prefix, such as `AWS_` or `AZURE_`, and
   every variable whose name ends in a suffix such as `_TOKEN`, `_PASSWORD` or
   `_ACCESS_KEY`. **Cursor review, F4.**
3. **Workspace.** **Cursor review, F2.** The turn is refused when the working
   directory or any directory above it, other than the home directory, has a
   `.cursor` at all, since Cursor reads many files there. In the home directory,
   `~/.cursor` is Cursor's own, and the turn is refused when it holds any of
   `agents`, `cli.json`, `commands`, `environment.json`, `hooks.json`,
   `mcp-approvals.json`, `mcp.json`, `permissions.json`, `plugins`, `rules`,
   `sandbox-policies`, `sandbox.json`, `settings.json`, `skills` or
   `worktrees.json`. Only their existence is checked.
4. **Configuration and data directories.** Dromon makes a fresh directory for the
   turn, sets `CURSOR_CONFIG_DIR` to it and `CURSOR_DATA_DIR` to an empty directory
   inside it, **Cursor review, F7**, and removes both afterwards. An operator's own
   value of either is replaced. Its `cli-config.json` allows
   each of Dromon's tools as `Mcp(dromon:TOOL)`, denies `Read(*)`, `Write(*)`,
   `Shell(*)` and `WebFetch(*)`, keeps `approvalMode` at `allowlist`, and enables the
   sandbox. For a line with Dromon's tools, a plugin directory beside it declares the
   MCP server `dromon`, `dromon mcp --line LINE` on `lines.sock`, with the token
   written as `${env:DROMON_LINE_TOKEN}`, which Cursor fills in from its environment.
5. **Start.** `cursor-agent --print --output-format stream-json --model ID --workspace
   DIR --trust --sandbox enabled`, with `--plugin-dir` for a line with tools, and the
   prompt after `--`. Never `--resume`, by step 1, nor `--force`, `--yolo`,
   `--approve-mcps`, `--api-key`, `--endpoint` or `--header`. Without
   `--approve-mcps` a plugin's server may stay unapproved in print mode, which matters
   only to a daemon line, and a daemon refuses those. Every turn opens a new chat, with
   the line's instructions ahead of the prompt.
6. **Init.** The first event must be `init`. Its `apiKeySource` of `login` is a
   subscription, `env` or `flag` is an API key, and anything else ends the turn. The
   driver then judges the credential and the display name.
7. **Events.** `assistant` text is text, the first of a run of `thinking` events is
   thinking, and each tool call, of any subtype, must be one of Dromon's tools by its
   provider and tool name, while only a `started` one is counted. Any other tool, an
   event type other than `system`, `user`, `assistant`, `thinking`, `tool_call` and
   `result`, or the driver's tool budget, ends the process group and the turn, which
   waits at most 60 seconds for the stream to close.
8. **Deadline.** **Cursor review, F6.** A turn that has not ended an hour after it
   started is stopped and refused. This bounds the turn instead of `max_turns`, which
   Dromon cannot count on Cursor, as the section on usage says.
9. **Process group.** **Cursor review, F5.** Cursor starts in a process group of its
   own. Every stop signals the whole group, and whatever remains of it once Cursor has
   exited is terminated and then killed. A child that starts a session of its own
   leaves the group and would not be reached.
10. **End.** The `result` event's subtype `success` without `is_error` is success, and
   anything else an error. A stream that ends without a result is refused. The chat
   identifier is reported, and a further turn on it is refused by step 1.

## Usage, billing and sessions

Cursor reports no cost and no token usage, and Dromon says so. **Platform limit.** A
Cursor plan with usage-based pricing may charge for usage beyond the plan's allowance
under the same login, which nothing in the stream reveals, so a subscription driver
cannot tell a plan's included usage from paid usage. The operator controls that setting
in Cursor's dashboard. The driver's `max_turns` is not enforced, since the stream does
not mark the model's calls. **Decision, Cursor review, F6.** It stays unenforced, and
the turn's deadline of an hour bounds a turn instead.

**Sessions, re-review N1.** Cursor keeps a chat in its data directory, which Dromon
makes empty for each turn and removes afterwards, so a Cursor line runs a single turn.
Multi-turn Cursor lines first need a data directory kept for the line under a path
Dromon owns, and checked before each turn, as the workspace is, for anything Cursor
would load that Dromon has not read. Until then a resume is refused rather than
attempted.

## Configurations a Cursor line refuses

- Any built-in tool, a line effort other than its driver's, MCP servers of its own and
  `inherit_account_mcp`.
- `claude_code_prompt`, `project_settings`, `plugins`, a sandbox or settings document,
  `allow_compact` and `keep_session`.
- An existing chat to resume, by step 1 of a probe turn.

## Not verified

- **Inference.** That `cli-config.json` with only these keys is accepted, and that
  Cursor writes into it during the turn without harm.
- **Inference.** That the plugin's server is named `dromon` in `providerIdentifier` and
  in `Mcp(dromon:TOOL)` rules, and that `${env:NAME}` is filled in for a plugin's server.
- **Inference.** That `Shell(*)` and `Write(*)` deny every command and write, as
  `Read(*)` is shown to deny every read.
- **Inference.** That `--trust` approves nothing beyond the workspace, given the checks
  of step 3.
- **Hypothesis.** That the tool call's JSON has one of the two shapes the adapter
  reads, `{"KIND": {...}}` as documented or `{"tool": {"case": KIND, ...}}` as held in
  memory.
- **Inference.** That Cursor runs with an empty data directory, and that it keeps its
  chats there, which is why a resume is refused.
- **Inference.** That no child Cursor starts leaves the process group, and that the
  event types listed above are all a turn carries, without which a turn is refused.

## Verify first

Run these in a throwaway directory before the probe.

1. Run `cursor-agent status` and confirm the login, and that `CURSOR_API_KEY` is unset.
2. Run one print turn by hand with `CURSOR_CONFIG_DIR` set to a directory holding
   Dromon's `cli-config.json`, ask the model to read `/etc/hosts`, to list a directory
   and to run `ls`, and confirm each is refused as a permission denial.
3. Confirm the `init` event's `apiKeySource` is `login` and its `model` is exactly
   `GPT-5.6 Luna 1M`.
4. Only if daemon lines are ever reconsidered, confirm whether a plugin's MCP server
   connects without `--approve-mcps`, that its tool call shows `providerIdentifier`
   `dromon`, and that the token reached it.
5. Confirm the shape of a `tool_call` event in the stream.
6. Ask the model to search the web or start a subagent, and confirm that the probe
   stops and refuses the turn, how far the tool got first, and with `ps` that no
   process of the turn remains.
7. Confirm that a turn on the empty data directory signs in, and record every event
   type the stream carries.
8. Ask the model to grep and to glob, and confirm that `Read(*)` denies both.
9. Run `dromon probe --model cursor-agent:gpt-5.6-luna-medium
   --accept-partial-isolation` and confirm the credential `subscription` and a reply.
10. Only if multi-turn lines are wanted, confirm where Cursor keeps a chat, that
   `--resume CHAT` in print mode continues it on a kept data directory and reports the
   same `session_id`, and what it does for an unknown chat.
11. Check the account's usage-based pricing setting in Cursor's dashboard.
