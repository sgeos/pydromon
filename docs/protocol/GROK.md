# Grok Build Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative for Dromon's side. The Python module
`dromon.grok` implements it against Grok Build 1.0.70. **Not verified against a live
turn.** The operator's Grok usage was exhausted, so no session was created and no
prompt was sent.

## Purpose

**Directed by the operator on 2026-10-25.** Nominal support for Grok, without testing
until Grok usage is available. A line whose driver names the `grok-build` engine runs
its turns through `grok agent --no-leader stdio`, which speaks the Agent Client
Protocol, ACP, as JSON-RPC 2.0, one message per line.

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

**Facts, observed on 2026-10-25.** An `initialize` request, which runs no model,
answered with protocol version 1, `loadSession`, MCP over HTTP and SSE, the
authentication methods `cached_token` and `grok.com` with `defaultAuthMethodId` set to
`cached_token`, and a current model of `grok-4.5`. Grok's model cache gives `grok-4.7` a
default context of 256,000 tokens and the efforts `low`, `medium`, `high` and `xhigh`.
The operator's Grok configuration sets `permission_mode` to `always-approve` and
declares MCP servers for mail and documents.

## A turn

1. **Environment.** `XAI_API_KEY`, `GROK_AUTH`, `GROK_CODE_XAI_API_KEY`,
   `GROK_AUTH_PATH`, `GROK_CONFIG` and `GROK_CONFIG_PATH` are removed. `GROK_HOME` and
   `XAI_API_BASE_URL` are refused. `GROK_SANDBOX` names Dromon's profile.
2. **Profile.** The profile below must exist and pass its checks, or the turn is
   refused with the table to add.
3. **`initialize`**, advertising no file system and no terminal, so Grok uses its own
   tools inside its sandbox. The protocol version must be 1 and
   `defaultAuthMethodId` must be `cached_token`.
4. **`session/new`**, or `session/load` with the stored session identifier, with the
   line's working directory as `cwd`, which is its `workdir` as
   [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies or the project root, Dromon's MCP
   server, and `_meta` with `yoloMode` and `autoMode` false
   and the line's instructions as `rules`.
5. **Model and mode.** The response's `models.currentModelId`, or its `model` config
   option, is handed to the driver. Its `modes.currentModeId` must be `default` or
   `ask`. Otherwise Dromon sends `session/set_mode` with `default` if Grok offers it, and
   refuses the turn if not or if no mode is reported.
6. **`session/prompt`** with the wake prompt. Message chunks are text, thought chunks
   are thinking, and each `tool_call` is checked against the line's tools.
7. **Permission requests** are answered with an `allow_once` option for a permitted call
   and a `reject` option otherwise. A tool call the line was not given, or the driver's
   tool budget, sends `session/cancel`. Other requests from the agent are answered with
   an error.

| ACP tool kind | Permitted by |
|---|---|
| `read` | `Read` |
| `search` | `Grep`, `Glob` or `Read` |
| `edit`, `delete`, `move` | `Edit` or `Write` |
| `execute` | `Bash` |
| `fetch` | `WebFetch` or `WebSearch` |
| `think` | Always, not counted |
| Any other | Only a tool of Dromon's MCP server, `dromon__NAME` |

## Sandbox profiles

A line without `Edit` or `Write` runs under `dromon-reader`, and one with either runs
under `dromon-writer`. The operator defines them, for example in `~/.grok/sandbox.toml`.

```toml
[profiles.dromon-reader]
extends = "read-only"
deny = [".dromon"]

[profiles.dromon-writer]
extends = "workspace"
read_only = ["/real/path/of/project/.git"]
deny = [".dromon"]
```

Before each turn Dromon checks that the profile extends `read-only` for a reader, or
`workspace` or `strict` for a writer, that its `deny` entries cover the state and runtime
directories as Grok resolves them, with `~` at the home directory and other relative
entries at the workspace, and that a writer's `read_only` or `deny` covers `.git`. The
workspace is the line's working directory, and a project's own `.grok/sandbox.toml` is
read there. The profile names are fixed, so Grok lines in different working directories
share one definition of each, whose entries must cover every one of their `.git` paths
and every protected directory by its real path. The bridge below still runs in the
project root, where it finds the runtime directory.
**Inference, not verified.** A `read_only` entry inside a writable workspace keeps it
read-only, as the field's name says.

## Dromon's tools

ACP has no tools hosted by the client, so a Grok line reaches its own tools, such as
`send_message`, through `dromon mcp --line LINE`, which Grok starts as a stdio MCP
server. The bridge connects to `lines.sock` in the runtime directory, mode 0600, which
the daemon always opens. A request there must carry the token the daemon issued for
that line, held only in memory, and it serves only that line's own tools.

## Not verified

- That Grok honours `yoloMode` false, reports `modes` and `models` in the shapes
  above, accepts `session/set_mode`, and names MCP tools as `server__tool` in
  `toolName`.
- That a cached sign-in is a subscription rather than usage billed per token. Dromon
  treats it as a subscription, and refuses the API strategy.
- That the operator's MCP servers stay out of a line's reach, which rests on Grok asking
  before an MCP call and Dromon refusing it, and on Dromon cancelling a turn that uses
  one.
- That Grok's permission grants remembered for a project do not approve calls Dromon
  would refuse.

## Not at parity

- No MCP servers of the line's own, and no sandbox or settings documents.
- No network blocking for a Grok line's commands on macOS.
- Grok reports no cost, and the driver's `max_turns` is not enforced.
