# Engine Settings

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative. The Python module `dromon.settings`
implements it. It is part of line configuration version 1 and project file version 1,
as optional keys, so documents written before it remain valid.

## Purpose

**Directed by the operator on 2026-10-25.** Dromon should reach functional configuration
parity with Claude Code, Codex and Grok Build for the settings the operator actually
uses, configurable per session line and per project. This document specifies the
engine settings that close that gap, and the review behind them.

## Review of the settings in use

**Fact.** On 2026-10-25 this line read the user and project configuration of the three
harnesses on the operator's machine, with credential values redacted and credential
files left unread. The table lists each setting found, by key, and how Dromon provides
it. Paths, project names and values are deliberately not recorded here.

| Harness | Setting in use | Dromon |
|---|---|---|
| Claude Code | `permissions.allow` with `Bash(...)`, `WebFetch(domain:...)` and `WebSearch` rules | `settings.permissions.allow` |
| Claude Code | `permissions.deny` with `Read(...)` rules for credential directories | `settings.permissions.deny` |
| Claude Code | `hooks` of type `command` on `SessionStart` and `UserPromptSubmit`, with `timeout` | `settings.hooks` |
| Claude Code | `env` | `settings.env` |
| Claude Code | `attribution.commit` and `attribution.pr` | `settings.attribution` |
| Claude Code | `sandbox`, including `excludedCommands`, `filesystem` and `network` keys | `sandbox`, as [`SANDBOX.md`](SANDBOX.md) specifies |
| Claude Code | `.mcp.json` servers | `mcp_servers`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies |
| Claude Code | `model` | The line's model, fixed at creation |
| Claude Code | `permissions.defaultMode` of `auto` | Not at parity, see below |
| Claude Code | `enabledPlugins`, LSP and MCP plugins | Not at parity, see below |
| Codex | `model_reasoning_effort` | `effort` |
| Codex | `sandbox_mode` of `workspace-write` with `network_access` off | `sandbox` with `enabled` and no allowed domains |
| Codex | `approval_policy` and `approvals_reviewer` | Not at parity, see below |
| Codex | `prefix_rule` with `decision = "allow"` | `settings.permissions.allow` with a `Bash(prefix *)` rule |
| Codex | `mcp_servers` with `url`, `bearer_token_env_var` and `enabled_tools` | `mcp_servers` with a `${NAME}` header, and `allowed_tools` |
| Codex | Project `hooks.json` on `SessionStart` and `UserPromptSubmit` | `settings.hooks` |
| Codex | `shell_environment_policy.inherit = "all"` | Dromon's default, since Claude Code inherits the daemon's environment |
| Grok Build | `permission.ask` with `Bash(...)` rules | `settings.permissions.ask` |
| Grok Build | `ui.permission_mode` of `always-approve` | `tool_policy = "allow_all"` |
| Grok Build | A sandbox profile that extends `workspace` and denies credential directories | `sandbox` with `enabled` and `filesystem.denyRead` |
| Grok Build | `mcp_servers` with `enabled` and an OAuth client secret from a variable | `mcp_servers` with `${NAME}` references, OAuth aside |
| Grok Build | `features.lsp_tools` with `lsp.json` | Not at parity, see below |
| All three | Status lines, themes, terminal titles, notices, marketplaces, trusted folders, model context windows of other vendors' models | Not applicable. These configure an interactive terminal or another vendor's model |

## Document

A settings document is a subset of a Claude Code settings file, with Claude Code's key
names, so that one can be copied from `.claude/settings.json`.

```json
{
  "permissions": {
    "allow": ["Bash(cargo test *)", "WebFetch(domain:docs.rs)"],
    "ask": ["Bash(git push *)"],
    "deny": ["Read(~/.ssh/**)"]
  },
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "notify start", "timeout": 10}]}]
  },
  "env": {"SCCACHE_DIR": "/var/cache/sccache"},
  "attribution": {"commit": "", "pr": ""}
}
```

| Key | Value |
|---|---|
| `permissions.allow`, `ask`, `deny` | Arrays of rules, each `Tool` or `Tool(specifier)`, where a tool is a built-in name or `mcp__server`, `mcp__server__*` or `mcp__server__tool` |
| `hooks.EVENT` | Array of `{matcher, hooks}`, where `matcher` is optional text and `hooks` a non-empty array of `{type, command, timeout}` |
| `env` | Table of variable name to text, which may be empty |
| `attribution.commit`, `attribution.pr` | Text, which may be empty |

- **Meaning.** Each key means what it means in Claude Code, as documented at
  <https://code.claude.com/docs/en/settings>, <https://code.claude.com/docs/en/permissions>
  and <https://code.claude.com/docs/en/hooks>. Claude Code parses rule specifiers, so
  Dromon checks only their shape.
- **Events.** `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
  `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Notification`,
  `SubagentStart`, `SubagentStop`, `Stop`, `StopFailure` and `PreCompact`.
- **Hooks.** Only `command` hooks are accepted, with `type`, `command` and an optional
  `timeout` in seconds in [1, 86400]. A hook runs outside the sandbox.
- **Reserved variables.** `env` may not set `DROMON_LINE_MARK`, the mark Dromon gives a
  line's processes, or a variable that selects or holds the engine's credential or
  provider. These are every name that starts with `ANTHROPIC_`,
  `CLAUDE_CODE_USE_`, `OPENAI_`, `CODEX_` or `XAI_`, and `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR` and
  `AWS_BEARER_TOKEN_BEDROCK`. Claude Code applies `env` to itself, and usage billed per
  token must never start silently.
- **Strict keys.** Any other key, `permissions.defaultMode` included, is an error.
  Rules are sorted and deduplicated, hooks keep their order, and documents are stored
  as canonical JSON. At most 256 entries are accepted in any array or table, and each
  text is at most 4,096 characters.

## Effort

A line's `effort` is one of `low`, `medium`, `high`, `xhigh` and `max`. It replaces the
reasoning effort its driver declares, and an omitted `effort` keeps the driver's. The
driver still bounds the turn by its tool budget and turn limit.

## Layers

| Layer | Where | Applies to |
|---|---|---|
| Project | `[settings]` in `.dromon/dromon.toml` | Every line, including lines created through the client |
| Profile | `[profiles.NAME.settings]` | Lines of that profile in the file |
| Line | `[lines.ID.settings]`, or `--settings-config FILE` on `dromon create` and `dromon configure` | One line |

Layers merge from least to most specific, as Claude Code merges its settings scopes.
Rule arrays are united, so no layer removes another layer's deny rule. Hooks are
appended per event, without repeating an identical entry. A variable or an attribution
text in a more specific layer replaces the less specific one. `effort` is a plain line
key, so a line's replaces its profile's.

A `--settings-config` file is a Claude Code settings file. Its supported keys are
taken, its `sandbox` key becomes the line's sandbox, `$schema` is ignored, and any other
key is an error. A sandbox given both there and through `--sandbox-config` is refused.

## What reaches the engine

When a line's turn starts, Dromon merges the layers, derives the settings and hands them
to Claude Code through its `--settings` flag, where Claude Code applies them with its
own semantics.

1. **Ask rules.** An `ask` rule is handed over as a `deny` rule. Claude Code would ask a
   person, a Dromon line has nobody at the prompt, and a question never pauses a line.
   Claude Code's `dontAsk` mode treats an unanswerable prompt the same way. The line can
   ask the operator with `ask_operator`, and the operator can change the rule.
2. **Protected directories.** Every line's settings deny `Read` and `Edit` on the
   project's state directory and runtime directory, given as written and as real paths
   in Claude Code's absolute form `//path/**`. Claude Code applies `Read` rules to the
   tools that read files and `Edit` rules to the tools that write them. This holds
   whether or not the line's sandbox is enabled, and a deny rule wins over any allow
   rule.
3. **Allow rules and hooks.** Claude Code approves a call that matches an `allow` rule,
   or that a `PreToolUse` or `PermissionRequest` hook approves, without consulting
   Dromon's permission gate. Such a call is still limited to the tools the line's
   configuration offers, because the engine offers no others.

**Fact, verified on 2026-10-25 on macOS with Claude Haiku 5.5.** Through the SDK with
`setting_sources=[]`, a `deny` rule refused a Bash call before the permission callback
was consulted. Through Dromon's engine, in the live tests of `tests/test_settings.py`,
an `ask` rule refused a Bash call while an unmatched Bash call ran, and a
`UserPromptSubmit` hook ran, saw a variable set in `env`, and its output reached the
model.

## Project settings

**Decided by the operator on 2026-10-26.** A Claude Code line with `project_settings`
set, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, starts with Claude Code's
`project` setting source instead of none. Dromon's own settings document still arrives
through `--settings` as above.

**Fact, read on 2026-10-26 in the Agent SDK 0.2.174 installed in this environment.**
The docstring of `ClaudeAgentOptions.setting_sources` in `claude_agent_sdk/types.py`
lists `"project"` as the project settings, `.claude/settings.json`, and says that the
list must include `"project"` for `CLAUDE.md` files to load. The SDK's command builder
in `claude_agent_sdk/_internal/transport/subprocess_cli.py` passes the list to Claude
Code as `--setting-sources=project`. The docstring of `ClaudeAgentOptions.skills` says
that enabling skills makes the SDK load the user and project sources so that Claude
Code discovers installed skills. What Claude Code then reads is its own behaviour. It
was not observed live by this line.

**Inferences, not verified live.**

- The working directory's `.claude/settings.json`, its `CLAUDE.md` files and its
  project skills reach the line. `.claude/settings.local.json` is the separate `local`
  source and does not.
- A skill is used through the `Skill` tool, so a line that should use skills lists
  `Skill` in `builtin_tools`, since the engine is offered only the configured tools.
- A `CLAUDE.md` also named in `instructions` reaches the line twice.

**What still holds, confirmed in code.** The engine is started with `tools` set to the
line's configured built-in tools and with strict MCP configuration, as before. The init
check compares every tool the engine reports with the configuration and refuses the
turn on any other, whatever a project file declares. A test drives a turn whose engine
offers `Skill` and `Bash` beyond a configured `Read` and sees it refused.

**What a project allow rule could do.** Claude Code approves a call that matches an
`allow` rule without consulting Dromon's permission gate, as section 3 above records
for Dromon's own rules, and it merges such arrays across sources. A project
`permissions.allow` rule could therefore approve a configured tool's call that the gate
would refuse, such as an MCP tool of a configured server outside `allowed_tools`, or a
shell call that asks to leave the sandbox.

**Allowlists.** **Decided by the operator on 2026-10-26.** What the project source loads
must not run code or grant anything. At the start of every turn, before the engine
starts, Dromon checks what it would load under the line's working directory and refuses
the turn, naming the file and the key or entry, unless all of these hold.

| What | Accepted | Why only these |
|---|---|---|
| Keys of `.claude/settings.json` | `$schema`, `attribution`, `includeCoAuthoredBy` | The first names a schema for editors, and the others only choose attribution text in commits and pull requests |
| Entries of `.claude` | `settings.json`, `settings.local.json`, `skills`, `CLAUDE.md` | The local settings are a source Dromon never loads. Commands, agents, hooks and output styles can grant tools or run commands |
| Front matter keys of `.claude/skills/NAME/SKILL.md` | `name`, `description`, `license`, `version` | Keys such as `allowed-tools`, `hooks` and `model` grant a tool, run code or change the model |

Every other settings key refuses the turn, explicitly including `hooks`, `mcpServers`,
`enableAllProjectMcpServers`, `enabledMcpjsonServers`, `statusLine`, `permissions` of
every kind, `sandbox`, `env` and the plugin keys, and any key Claude Code adds later. A
line's hooks, such as Keleusma's mailbox reminders, belong in Dromon's own settings
document, which the operator controls. A missing `.claude` is accepted, and one that
cannot be read, or a settings file that is not a JSON object, refuses the turn.

**Skills.** `skills` may hold only directories, each with a `SKILL.md`, so a loose file
or an empty directory refuses the turn. A skill's front matter is the block between a
first line `---`, after any byte order mark, and the next line `---`, and one that never
ends refuses the turn. Each of its lines must be blank, a comment, or an allowlisted key
written without quotes at column zero. An indented line is accepted only as the body
of a block scalar, after an allowlisted key whose value is `|` or `>` with an optional
chomping indicator, `+` or `-`, and until the next line at column zero. There YAML
reads it as literal text, so a line that looks like a key grants nothing. An explicit
indentation indicator such as `>2` is refused.
Any other line, an indented or quoted key or a flow mapping included, refuses the turn.
This is deliberately stricter than YAML, since no YAML parser is used, and a front
matter that YAML would read as setting a refused key cannot pass it.

**What the line may not write.** **Decided by the operator on 2026-10-26.** A line that
sets `project_settings` or `plugins` must run with an enabled sandbox, whatever its
tools, so that what it loads is protected by the sandbox and not by permission rules
alone. That sandbox denies writes to the working directory's `.claude` and to each
plugin directory, and the line's settings deny the same paths to the built-in editing
tools with `Edit` rules.

**Edits during a turn.** The checks run at the start of each turn, before the engine
starts. What changes afterwards, during the turn, is read by the engine unchecked
until the next turn's check, for example a skill file read only when the skill is
used. **Inference, from the rules above, not tested live.** The line itself cannot make
such a change. Its shell commands run in the enabled sandbox, which denies writes to
those paths, and its built-in editing tools are refused by the `Edit` rules, which a
deny keeps from being approved. A process outside the line, such as the operator or a
line whose sandbox grants those paths, can still change them during a turn, and that
change is read unchecked until the next turn.

**Fact, read on 2026-10-26 in Claude Code 2.1.310, bundled with the Agent SDK 0.2.174
in this environment.** The CLI's permission rule validator maps a rule for `Write`,
`MultiEdit` or `NotebookEdit` with a path to `Edit`, warns that such a rule "is not
matched by file permission checks", only `Edit(path)` rules are, because "Edit rules
cover all file-editing tools", and for a deny rule adds a notice that it "never
matches". This was read from the bundled executable's embedded source, not observed in
a live turn. So Dromon hands over only the `Edit` rule, which covers `Write`, and no
`Write` rule.

## Plugins

**Decided by the operator on 2026-10-26.** A Claude Code line's `plugins`, as
[`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, are local plugin directories passed to
the engine through the Agent SDK's `plugins` option, which the SDK hands to Claude Code
as `--plugin-dir`. **Fact, read on 2026-10-26** in the command builder of the installed
Agent SDK 0.2.174. No setting source is opened for them.

A plugin can bring hooks, MCP servers, commands, agents, output styles and language
servers, any of which runs code or grants a tool. So at every turn each plugin must
pass these allowlists, or the turn is refused, naming the plugin and the key or entry.

| What | Accepted | Why only these |
|---|---|---|
| Entries of the plugin directory | `.claude-plugin`, `skills`, `README`, `LICENSE`, each also with `.md` or `.txt`, and `.in_use` | `.in_use` is Claude Code's own record of the sessions using an installed plugin. Directories such as `hooks`, `commands` or `agents` hold components |
| Entries of `.claude-plugin` | `plugin.json`, `marketplace.json` | The manifest and the listing that installed plugins carry |
| Keys of `plugin.json` | `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords` | Each describes the plugin. Component keys such as `hooks`, `mcpServers`, `commands`, `agents`, `outputStyles`, `lspServers` and `skills`, and any unknown key, are refused |
| Keys of `marketplace.json` | `$schema`, `name`, `description`, `owner`, `plugins`, whose entries may set the `plugin.json` keys and `source` | A listing entry can otherwise declare components |
| Entries of `skills` | Directories, each with a `SKILL.md` | As for project skills |
| Front matter of each `skills/NAME/SKILL.md` | The project skill allowlist above | As for project skills |
| `source` of each listing entry | A relative path that stays inside the plugin directory, such as `./` | A remote, absolute or escaping source would load a plugin no allowlist saw |

`plugin.json` is required. **Fact, observed on 2026-10-26.** The one user plugin a
downstream project's instructions require, installed on this machine, passes these
allowlists unchanged. It holds only the accepted entries, descriptive manifest keys and
one skill whose front matter sets `name`, `license` and `description`. That its skill
loads without a manifest key is an inference from its layout, and was not observed in
a live turn.

## Rules are guards, the sandbox is the barrier

Claude Code matches a Bash rule against the command as written, so `ask` or `deny` on
`Bash(git commit *)` refuses `git commit` with an explanation but not every spelling,
such as `git -C . commit`. **Inference, from the rule syntax, not tested by this line.**
To keep a line from committing, also deny writes to the repository's `.git` in its
sandbox and `Edit` there in its rules. **Fact, verified on 2026-10-25 in epagoge.** With
`filesystem.denyWrite` naming `.git`, a sandboxed Claude Haiku 5.5 line's
`python3 -c "open('.git/...', 'w')"` failed with `Operation not permitted`, and its
`touch .git/...` was refused by the permission layer before it ran, while the project's
full gate still passed from the same line.

## Trust

A hook runs a command with the daemon's privileges and outside the sandbox, and can
approve tool calls, so hooks carry the trust of a stdio MCP server, which the project
file can already declare. The project file therefore deserves the review any file that
runs commands does. `env` values are stored in the event log like MCP credentials, so a
secret belongs in the daemon's environment, where an MCP server can refer to it as
`LINE_CONFIG.md` specifies.

## Not at parity

- **Automatic approval.** Claude Code's `auto` mode and Codex's automatic reviewer let a
  model judge each call. Dromon has no reviewer. Its nearest equivalents are an
  allowlist with `allow` and `deny` rules, or `allow_all` inside an enabled sandbox, and
  a reviewer would spend usage the operator has not agreed to.
- **Project and user settings.** Only the project source is available, through
  `project_settings`, and only within the allowlists above. The user and local sources
  are never loaded.
- **Plugins and language servers.** A line loads only local plugins that pass the
  allowlists above, so plugins with hooks, servers, commands or agents, and with them
  `LSP` tools and plugin MCP servers, are unavailable. A plugin's MCP server can be
  declared in `mcp_servers`.
- **MCP OAuth.** Dromon cannot perform an MCP server's OAuth flow. A server that needs
  one can be reached through a claude.ai connector with `inherit_account_mcp`, or with a
  token held in the daemon's environment and named by a `${NAME}` header.
- **Other keys.** `permissions.defaultMode`, `additionalDirectories`, hook types other
  than `command`, and the many interface settings are refused or not applicable.
- **Inference, not verified.** Claude Code merges `Read` and `Edit` rules into the
  sandbox's filesystem rules. Since the rules now reach Claude Code, this merge is
  expected to apply to Dromon lines too, but this line has not tested it.
