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
- **Reserved variables.** `env` may not set a variable that selects or holds the
  engine's credential or provider. These are every name that starts with `ANTHROPIC_`
  or `CLAUDE_CODE_USE_`, and `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR` and
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
- **Plugins and language servers.** The SDK loads only local plugin directories, and the
  official language server plugins declare their servers in their marketplace entry,
  not in the plugin directory. Dromon has no plugin layer, so `LSP` tools and plugin
  MCP servers are unavailable. A plugin's MCP server can be declared in `mcp_servers`.
- **MCP OAuth.** Dromon cannot perform an MCP server's OAuth flow. A server that needs
  one can be reached through a claude.ai connector with `inherit_account_mcp`, or with a
  token held in the daemon's environment and named by a `${NAME}` header.
- **Other keys.** `permissions.defaultMode`, `additionalDirectories`, hook types other
  than `command`, and the many interface settings are refused or not applicable.
- **Inference, not verified.** Claude Code merges `Read` and `Edit` rules into the
  sandbox's filesystem rules. Since the rules now reach Claude Code, this merge is
  expected to apply to Dromon lines too, but this line has not tested it.
