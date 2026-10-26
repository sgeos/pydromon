# Codex Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative for Dromon's side. The Python module
`dromon.codex` implements it against Codex 0.164.0. The app-server protocol is OpenAI's,
documented as experimental, and Dromon also relies on its experimental dynamic tools.

## Purpose

**Directed by the operator on 2026-10-25.** OpenAI models run on Dromon, tested with
GPT-6-Luna. A line whose driver names the `codex` engine runs its turns through
`codex app-server`, signed in by Codex itself on the operator's ChatGPT plan or with an
API key.

## Transport

Each turn starts `codex app-server` with standard input and output as the transport.
Messages are JSON-RPC requests, responses and notifications without a `jsonrpc` field,
one JSON object per line. Dromon reads one message at a time and answers every request
from the engine before reading on. The process is stopped when the turn ends.

## A turn

1. `initialize`, with `capabilities.experimentalApi` set, then `initialized`.
2. `account/read`. The account type `chatgpt` is a subscription and `apiKey` is API
   usage. Any other type is refused. The driver accepts or refuses the credential
   before any thread exists.
3. `thread/start` for a new line, or `thread/resume` with the stored thread
   identifier, with the driver's model, the line's working directory as `cwd`, which is
   its `workdir` as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies or the project root,
   `permissions` set to
   `dromon-line`, `approvalPolicy` set to `never` and `approvalsReviewer` set to `user`.
   A new thread also receives Dromon's own tools as `dynamicTools`.
4. The response's `modelProvider` must be `openai`, its `activePermissionProfile` must
   be `dromon-line`, and its `model` is handed to the driver, which refuses any model
   but its own.
5. `mcpServerStatus/list` for the thread must report every server as `disabled` with
   no tools, and `app/list` must list nothing.
6. `turn/start` with the wake prompt as text and the line's effort, or the driver's.
7. Notifications become driver events until `turn/completed`. A started
   `dynamicToolCall` of one of Dromon's tools, a `commandExecution` for a line given
   `Bash`, or a `fileChange` for a line given `Edit` or `Write`, is tool use. A `reasoning` item is thinking, and a completed `agentMessage`
   is text. Any other tool item, and a `model/rerouted` to a model the driver refuses,
   interrupts the turn with `turn/interrupt` and refuses it.
8. `item/tool/call` requests run Dromon's tool and answer with `success` and text
   `contentItems`. Approval requests are declined, and any other request is answered
   with an error.

## Isolation

Codex loads the operator's user configuration, which cannot be skipped by the
app-server. Dromon starts it with these arguments.

- `-c mcp_servers.NAME.enabled=false` for every server `codex mcp list --json` names.
  A name that is not a bare key is refused, because Codex rejects a quoted one.
- `--disable` for `apps`, `browser_use`, `browser_use_external`, `computer_use`,
  `goals`, `hooks`, `image_generation`, `in_app_browser`, `memories`, `multi_agent`,
  `plugins`, `remote_plugin`, `skill_mcp_dependency_install`, `skill_search`,
  `sleep_tool`, `tool_suggest` and `view_image`.
- `shell_tool` and `unified_exec` enabled only for a line given `Bash`, and disabled
  otherwise.
- `-c web_search="disabled"`.

**Fact, observed on 2026-10-25.** Without these arguments a GPT-6-Luna thread on the
operator's machine connected to the operator's mail, calendar, document, chat and task
servers, one of them with 47 tools. With them every server reported `disabled`, and
`config/read` reported web search and the disabled features as off. A
`-c mcp_servers={}` override did not remove any server.

## Permission profile

Every thread runs under `dromon-line`, a Codex permission profile that Dromon defines
on the app-server's command line as one inline TOML value, because Codex's `-c` parser
takes only bare keys and the profile's own keys need quoting. Codex's built-in
read-only sandbox would let a line read the event log and its stored credentials, and
its writable sandbox would let a line write them, so neither is used.

```toml
[permissions.dromon-line]
extends = ":workspace"

[permissions.dromon-line.filesystem]
":workspace_roots" = { "." = "write", ".git" = "read" }
"/real/path/of/project/.dromon" = "deny"
"/real/path/of/runtime" = "deny"
```

- The workspace root `.` is the line's working directory. Its relative `instructions`
  paths resolve there too.
- The workspace is `write` for a line given `Edit` or `Write`, and `read` otherwise.
- `.git` is always `read`, so a Codex line never commits. Codex documents that a
  profile extending `:workspace` keeps `.git` read-only, and a reported issue says it
  does not, so Dromon states it.
- Each protected directory that exists, the state directory and the runtime
  directory, is denied as its real path.
- Network access is off, as `:workspace` leaves it, so a Codex line cannot reach a
  local teacher.
- Codex's `:workspace` also leaves `$TMPDIR` and `/tmp` writable.

**Facts, verified on 2026-10-25 with Codex 0.164.0 on macOS.** Under the profile through
`codex sandbox -P`, a write in the workspace succeeded. Reading or writing `.dromon`,
writing `.git`, writing the home directory and reaching the network failed with
`Operation not permitted` or no connection. So did renaming `.dromon` or `.git` away,
replacing `.dromon` with another directory, a symbolic or hard link into it, and
changing its mode. An absolute deny on a runtime directory outside the workspace
refused reads and writes there. Through Dromon, a GPT-6-Luna line given `Edit` wrote a
file in the workspace, Codex rejected its edits to `.dromon` and `.git` with "patch
rejected: writing outside of the project", its shell read of `.dromon` failed, and in
the epagoge project the line ran the project's full gate, which passed.

**Fact, observed the same day.** Codex emits no item for an edit it rejects or for that
shell read, so Dromon's driver cannot count a call that Codex refuses before running
it.

## Credentials

Codex keeps its own login, and Dromon never reads, stores or forwards it. For a
subscription driver, Codex is started without `OPENAI_API_KEY`, `CODEX_API_KEY` and
`CODEX_ACCESS_TOKEN`, since Dromon starts the process and so controls its environment,
and the account must still read `chatgpt`. `OPENAI_BASE_URL` is refused for every
driver. **Fact, observed on 2026-10-25.** The operator's environment sets
`OPENAI_API_KEY`, and with it removed Codex reported a ChatGPT account.

## What a Codex line may have

| Configuration | On a Codex line |
|---|---|
| `builtin_tools` | `Bash`, which enables Codex's shell, and `Edit` or `Write`, which make the workspace writable for Codex's file changes and its shell |
| `mcp_servers`, `inherit_account_mcp` | Refused, not yet supported |
| `sandbox`, `settings` | Refused, including the project's own tables, which `dromond` checks at start |
| `tool_policy`, `allowed_tools` | Accepted, with no effect beyond Dromon's own tools |
| `effort` | Passed with each turn |
| `instructions` | Their text becomes the thread's developer instructions |
| `claude_code_prompt` | Refused, since Codex uses its own instructions |

A configuration a Codex line cannot run is refused when the line is created or
configured.

## Verification

**Fact, verified on 2026-10-25 with Codex 0.164.0 and GPT-6-Luna on the operator's
ChatGPT plan.** The live tests of `tests/test_codex.py` and
`tests/test_scheduler.py` showed that a line resumes its thread with its dynamic tools,
calling `record` in its second turn with a word given in its first, and that a
GPT-6-Luna line sends a message to a Claude Haiku 5.5 line, which is woken and runs its
turn. `dromon probe --model gpt-6-luna` reported the subscription credential.

## Not at parity with Claude Code lines

- No MCP servers, no sandbox or settings documents, and no way to commit, since `.git`
  is always read-only. In a linked worktree `.git` is a file, and the common git
  directory, with its hooks and configuration, lies outside the workspace. It is not
  writable either, because the profile leaves everything outside the workspace
  read-only apart from the temporary directories. That rule comes from Codex's
  `:workspace` profile, which `dromon-line` extends. Dromon's own entries grant writes
  only to the workspace root and add denies, as `permission_profile` in
  `dromon.codex` shows. **Fact, observed on 2026-10-25** under the profile, as recorded
  above. A write in the home directory failed. Two exceptions follow from the same
  rule. A common git directory under `$TMPDIR` or `/tmp` would be writable. So would one
  inside the workspace, when a line's `workdir` contains another checkout's git
  directory, since only the workspace's own `.git` is held read-only.
- No extra writable paths. **Fact, observed on 2026-10-26 in pydromon.** uv could not
  write its cache in the home directory, so a Codex line runs the gate with
  `UV_CACHE_DIR` under `$TMPDIR` and `UV_OFFLINE=1`, which works while the environment
  already matches the lock. A tool that stats the state directory, as pytest's
  collection did, fails with `Operation not permitted`, because the profile denies its
  metadata too, so pydromon's pytest configuration ignores `.dromon`.
- A call Codex refuses before running it is invisible to the driver's tool budget.
- Codex reports no cost, so cost limits do not count a Codex line's turns, and the
  driver's `max_turns` is not enforced. The tool budget still interrupts a turn.
- The app-server and dynamic tools are experimental, so a Codex update may break the
  adapter.
- **Inference, not checked.** Codex loads its usual `AGENTS.md` instruction files,
  while a Claude Code line loads no settings sources and so no `CLAUDE.md`.
