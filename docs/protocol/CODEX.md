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
   identifier, with the driver's model, the project root as `cwd`, `sandbox` set to
   `read-only`, `approvalPolicy` set to `never` and `approvalsReviewer` set to `user`. A
   new thread also receives Dromon's own tools as `dynamicTools`.
4. The response's `modelProvider` must be `openai`, and its `model` is handed to the
   driver, which refuses any model but its own.
5. `mcpServerStatus/list` for the thread must report every server as `disabled` with
   no tools, and `app/list` must list nothing.
6. `turn/start` with the wake prompt as text and the line's effort, or the driver's.
7. Notifications become driver events until `turn/completed`. A started
   `dynamicToolCall` of one of Dromon's tools, or a `commandExecution` for a line given
   `Bash`, is tool use. A `reasoning` item is thinking, and a completed `agentMessage`
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
| `builtin_tools` | Only `Bash`, which enables Codex's shell inside its read-only sandbox |
| `mcp_servers`, `inherit_account_mcp` | Refused, not yet supported |
| `sandbox`, `settings` | Refused, including the project's own tables, which `dromond` checks at start |
| `tool_policy`, `allowed_tools` | Accepted, with no effect beyond Dromon's own tools |
| `effort` | Passed with each turn |
| `instructions` | Their text becomes the thread's developer instructions |
| `claude_code_prompt` | Refused, since Codex uses its own instructions |

A configuration a Codex line cannot run is refused when the line is created or
configured. The read-only sandbox is required because Codex's writable sandbox would let
a line write Dromon's state directory.

## Verification

**Fact, verified on 2026-10-25 with Codex 0.164.0 and GPT-6-Luna on the operator's
ChatGPT plan.** The live tests of `tests/test_codex.py` and
`tests/test_scheduler.py` showed that a line resumes its thread with its dynamic tools,
calling `record` in its second turn with a word given in its first, and that a
GPT-6-Luna line sends a message to a Claude Haiku 5.5 line, which is woken and runs its
turn. `dromon probe --model gpt-6-luna` reported the subscription credential.

## Not at parity with Claude Code lines

- No MCP servers, no file editing, and no sandbox or settings documents.
- Codex reports no cost, so cost limits do not count a Codex line's turns, and the
  driver's `max_turns` is not enforced. The tool budget still interrupts a turn.
- The app-server and dynamic tools are experimental, so a Codex update may break the
  adapter.
- **Inference, not checked.** Codex loads its usual `AGENTS.md` instruction files,
  while a Claude Code line loads no settings sources and so no `CLAUDE.md`.
