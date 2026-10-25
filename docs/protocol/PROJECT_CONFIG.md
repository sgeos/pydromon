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
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | required | Must be 1 |
| `daemon.operator` | identifier | `operator` | The default operator |
| `daemon.tick_seconds` | number in [0, 60] | 1 | The tick cadence |
| `limits.*` | as in `CLIENT.md` | 12, 2, 0 | The conversation limits of every line |
| `wake.every_ticks` | integer in [1, 3600] | 1 | The wake policy of every line |
| `profiles.NAME` | table | none | A reusable line template with every line key but `profile` |
| `lines.ID` | table | none | A line, named by a line identifier |
| `lines.ID.profile` | profile name | none | The profile the line starts from |
| `lines.ID.model` | model | required | `claude-opus-5-5`, `claude-haiku-5-5`, `gpt-6-luna` on Codex as [`CODEX.md`](CODEX.md) specifies, or `grok-4.7` on Grok Build as [`GROK.md`](GROK.md) specifies |
| `lines.ID.strategy` | strategy | `subscription` | `subscription`, `api` or `flexible` |
| `lines.ID.limits` | table | the file's | Limits for this line, each key defaulting to the file's |
| `lines.ID.wake` | table | the file's | The wake policy for this line |
| tool keys | as in `LINE_CONFIG.md` | as there | `builtin_tools`, `mcp_servers`, `tool_policy`, `allowed_tools`, `inherit_account_mcp`, `effort`, `claude_code_prompt`, `instructions` |
| `teacher` | table | none | The project's local teacher model, as [`TEACHER.md`](TEACHER.md) specifies |
| `agents` | table | off | Which external agents may connect, and with what access, as [`AGENTS.md`](AGENTS.md) specifies |
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
- **Located errors.** Every error names the file. A syntax error carries its line and
  column. Any other error carries the dotted key path and the line of its table or key
  when it can be found. The daemon refuses to start on an invalid file.
- **Application.** At start, a declared line that does not exist is created, and one
  whose tools differ is reconfigured. A line's model and strategy are fixed at
  creation, so a file that changes either is refused and the daemon does not start.
  A client may still switch a line's model within its engine, as an override. Lines
  created through the client and absent from the file are left as they are.
- **Reload.** `reload_project` applies the file again without a restart, with the same
  rules, after checking all of it, so a refused reload applies nothing. It refuses a
  change to the sandbox, the engine settings, the teacher or the external agent policy,
  which apply only at start. A line the file no longer declares returns to the
  project's limits and wake policy.
- **Codex and Grok lines.** A project file whose own `[sandbox]` or `[settings]` table
  would reach a Codex or Grok line is refused at start, since such a line runs in its
  engine's own sandbox.
- **Restart.** The policies of limits and wake live in the daemon and are re-read on
  every start. The lines and their configurations are events in the log.

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
