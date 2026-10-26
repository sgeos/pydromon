# Pi Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-26. Normative for Dromon's side. The Python module
`dromon.pi` implements it against Pi 1.4.0. **Not verified against a live turn.** No
model was run, since a turn spends the operator's subscription.

## Purpose

**Directed by the operator on 2026-10-26.** A prospective adapter, ready for one live
turn when the operator has the resources. A line whose driver names the `pi` engine
runs its turns through `pi --mode rpc`, which reads JSON commands on its standard input
and writes responses and session events on its standard output, one object per line.

## Model names

Dromon names a Pi model `pi:PROVIDER/ID`, such as `pi:openai/gpt-6-luna`, the engine and
Pi's own identifier. Pi runs other vendors' models, so its bare identifiers would read
as the models Dromon runs on their vendors' own engines, with other credentials and
costs. [`DRIVER.md`](DRIVER.md) records the scheme. The packaged model is
`pi:openai/gpt-6-luna`, the model Dromon already runs on Codex, signed in to a ChatGPT
plan. **Before a live test, the operator must confirm against the ChatGPT plan's terms
that the plan may be drawn on through Pi, a third-party harness.** Dromon does not
establish it.

## Evidence

**Facts, read on 2026-10-26** in Pi's documentation, copied into the operator's
`tmp/harness-facts/pi`, and in the installed package under
`/opt/local/lib/node_modules/@earendil-works/pi-coding-agent`.

- `security.md` says Pi has no sandbox and does not ask for approval before a tool call,
  and that its working directory does not keep commands from other paths.
- `rpc.md`, `rpc-commands.md` and `rpc-types.d.ts` give the commands `get_state`,
  `prompt` and `abort`, a response per command matched by its `id`, and the
  `disposition` of an accepted prompt. Closing standard input asks Pi to exit.
- `json.md` gives the session events, among them `turn_start`, `message_update` with
  `text_delta` and `thinking_start`, `message_end`, `tool_execution_start`,
  `compaction_end` and `agent_settled`, the event after which Pi does nothing more by
  itself. Records end at a line feed alone.
- `message-types.md` says every assistant message carries `usage` with `input`,
  `output`, `cacheRead`, `cacheWrite` and a `cost` whose `total` Pi computes.
- `mcp.md` and `extensions.md` say MCP servers come only from `mcp.json` files or from
  an extension's `pi.registerMcpServer`, that a file's server of the same name wins,
  and that `--tools` with an `mcp__` entry filters MCP tools too. The source of
  `agent-session.js` and `mcp-servers.js` confirms that the filter also covers the MCP
  resource tools.
- `extensions.md` documents a `tool_call` handler that blocks a call before it runs.
  The source of `agent-session.js` turns an exception in such a handler into a failed
  call.
- `main.js` resolves `--session` with a path directly, and with an identifier by exact
  match, then by prefix, then across projects with a confirmation read from standard
  input. A new session's file is written only once it holds a conversation.
- `resource-loader.js` reads `--append-system-prompt` as a file when its value names an
  existing path.
- `cli/auth-check.js` and `pi-ai`'s `models.js` show that `pi auth check --provider P
  --json --no-refresh` prints the status, the provider and an `authType` of `oauth` or
  `api_key`, and prints a credential only with `--credentials`.
- The `pi-ai` providers mark the sign-ins of `anthropic`, `github-copilot`,
  `kimi-coding`, `meta`, `openai`, `openai-codex` and `xai` with `isSubscription`.
  **Decided by the operator on 2026-10-26, review F7.** Dromon takes all but
  `anthropic` as subscriptions. Whether a Claude plan may be drawn through a
  third-party harness is not established, so a Claude sign-in through Pi refuses the
  turn.
- Pi keeps a provider's prompt cache warm with requests of its own, by default for up
  to an hour after a run's last request, and reports them only as `entry_appended`
  events without usage, as `core/cache-warmer.js` and `core/agent-session.js` show.
- A compaction's result carries the `usage` of its summary call, as
  `core/compaction/compaction.d.ts` declares.
- Pi reads `SYSTEM.md` and `APPEND_SYSTEM.md` from the agent directory whatever the
  project's trust, as `core/resource-loader.js` shows.
- Pi's model catalogue gives `openai/gpt-6-luna` a context window of 272,000 tokens.

**Fact, observed by the operator on 2026-10-26.** With `OPENAI_API_KEY` in the
environment, `pi --list-models` offered OpenAI's API models, so a Pi line could bill per
token silently.

## Configuration

Pi reads MCP servers, custom providers and settings from its agent directory, by default
`~/.pi/agent`, which may hold the operator's own servers. Dromon runs Pi on a dedicated
agent directory, `~/.pi/dromon-agent`, which the operator signs in to once with
`PI_CODING_AGENT_DIR=~/.pi/dromon-agent pi` and `/login`. Before each turn Dromon refuses
the line when that directory is missing, when it holds an `mcp.json`, a `models.json`,
a `SYSTEM.md` or an `APPEND_SYSTEM.md`, or when its `settings.json` is missing, sets
anything outside a known safe set of display, retry, compaction and bookkeeping
settings, or does not set `"cacheWarming": "off"`. **Review F1.** Pi's cache warmer
would otherwise send requests no event reports, which would bill an API line or draw
on a plan uncounted. Dromon never opens its `auth.json`.

## A turn

1. **Configuration.** The line must have no built-in tools and none of the
   configurations the next section refuses.
2. **Environment.** `PI_PACKAGE_DIR` is refused. Dromon's own variables are replaced.
   For a subscription, every variable Pi reads a credential from is removed, as is any
   variable whose name ends in `_API_KEY`, `_TOKEN`, `_SECRET`, `_SECRET_KEY`,
   `_ACCESS_KEY`, `_CREDENTIALS` or `_PASSWORD`, every variable of a cloud provider by
   its prefix, such as `AWS_`, `AZURE_` or `GOOGLE_`, as the Cursor review asked of all
   three engines, and, **review F5**, the variables that name an endpoint, a credential store or a
   credential's account, among them `CLOUDSDK_CONFIG`,
   `AWS_EC2_METADATA_SERVICE_ENDPOINT`, `AZURE_OPENAI_ENDPOINT` and `LLAMA_BASE_URL`.
   `PI_CODING_AGENT_DIR` names the dedicated directory, and `PI_OFFLINE` and
   `PI_TELEMETRY` switch off startup network operations and install telemetry.
3. **Credential.** `pi auth check` runs for the model's provider in that environment. A
   status other than ready refuses the turn. An `oauth` answer for a provider whose
   sign-in Pi marks as a subscription is a subscription, an `api_key` answer is usage
   billed per token, and anything else refuses the turn. The driver then accepts or
   refuses the credential before Pi starts.
4. **Start.** `pi --mode rpc` starts in the line's working directory with the model,
   the effort as `--thinking`, `--session-dir` in the dedicated directory, and
   `--no-extensions`, `--no-skills`, `--no-prompt-templates`, `--no-themes`,
   `--no-context-files`, `--no-approve` and `--offline`. Dromon's extension is loaded
   with `--extension`, and for a line with Dromon's tools so is `builtin:mcp`, with
   `--tools` listing those tools by exact name. A line without them gets `--no-tools`
   and `--no-mcp`. The line's instructions are passed with `--append-system-prompt`,
   refused when the text names an existing file or exceeds 256 KiB.
5. **Model and session.** `get_state` reports the model, handed to the driver as
   `PROVIDER/ID`, and the session file, which must be the one Dromon asked to resume.
6. **Prompt.** `prompt` sends the wake prompt, and the turn is refused unless Pi answers
   with the disposition `started`. Events are read until `agent_settled`.
7. **Events.** Text deltas are text and a thinking block is thinking. Each
   `tool_execution_start` is checked against Dromon's tools. Any other tool, an
   `extension_error`, the driver's tool budget or a `turn_start` past the driver's
   `max_turns` sends `abort`, and the turn waits at most 60 seconds for Pi to settle.
   An extension dialog is answered as cancelled.
8. **Deadline.** A turn that has not settled an hour after it started is aborted the
   same way and refused, as the Cursor review asked of all three engines.
9. **End.** Standard input is closed. Pi starts in a process group of its own, so once
   it exits, or is terminated after ten seconds, whatever remains of the group, such as
   the `dromon mcp --line` child, is terminated and then killed. The session file is
   kept for the next turn only once Pi has written it.

## Dromon's tools and its extension

Pi has no command-line option for an MCP server, so Dromon ships an extension,
`src/dromon/pi_extension.js`, which Pi loads by path. It registers Dromon's tools as the
MCP server `dromon`, started with `dromon mcp --line LINE` on `lines.sock` and given the
line's token, as on a Grok line, with `direct` exposure. It also blocks every tool call
whose name is not in the list Dromon passes, before the tool runs, and blocks every call
when the list is missing or malformed. A file-configured server named `dromon` would win
over the registration, which is why the dedicated directory may hold no `mcp.json`, and
`--no-approve` keeps a project's `.pi/mcp.json` from loading.

## Usage and sessions

The turn's tokens and cost are the sums over its assistant messages and, **review F2**,
the summary calls of Pi's own compactions, under the names Dromon uses for Claude
Code's. Pi computes the cost from its catalogue's prices, so on a
subscription it is an estimate, not a charge, and it counts towards the line's cost
limit. The context estimate is the last assistant message's input with its cache reads
and writes, from the last assistant message, never a summary call. A compaction Pi makes
by itself is reported at once and, **review F3**, `line_status` shows Pi's estimate of
the size after it as `after_estimate_tokens`, with a note that it is Pi's estimate, and
no measured size. A turn without usage says it reports none.

A session resumes from its file's path, which Dromon checks is still a file under the
dedicated directory's `sessions`. A session that is gone refuses the turn rather than let
Pi start a new one in its place.

## Configurations a Pi line refuses

- Any built-in tool. Pi has no sandbox and does not ask before a tool call, so Dromon
  could not keep a Pi line's file and shell tools from its own state, the credentials in
  the agent directory or paths outside the line's working directory.
- MCP servers of the line's own and `inherit_account_mcp`.
- `claude_code_prompt`, `project_settings`, `plugins`, a sandbox and a settings
  document, which only Claude Code honours, and a project whose own sandbox or settings
  would reach a Pi line.
- `allow_compact` and `keep_session`.

**Inference.** A project's `daemon.require_sandbox` is satisfied by a Pi line, since it
has no built-in tool that could run a command or touch a file.

## Not verified

- **Inference.** That Pi's `auth check` names the credential the run then uses. Both
  read the same store a few seconds apart, so an operator who changes the store in
  between could change the credential.
- **Inference.** That the `openai` provider's ChatGPT sign-in serves `gpt-6-luna`, and
  that `--offline` does not stop an expired sign-in from being refreshed.
- **Inference.** That Pi passes a registered server's `env` to it and connects it before
  the first prompt, as `mcp.md` says of `direct` servers.
- **Hypothesis.** That Pi emits `tool_execution_start` for a tool name the model invents.
  If it does, such a turn is refused, as it is on Grok.

## Not at parity

- No built-in tools, no MCP servers of the line's own, and no `compact_session`.
- Pi compacts by itself, by its own threshold, which Dromon reports but does not
  control, and `auto_compact_tokens` has no effect on a Pi line.
- Pi's cache warming must be off, so a Pi line's prompt cache may expire between turns
  where Pi would have kept it warm.
- No kept session. Each turn starts Pi afresh.
- Standard error is discarded, so a Pi that fails to start shows only as a closed
  connection.

## Verify first

Run these by hand, then one turn with `dromon probe --model pi:openai/gpt-6-luna`.

1. Sign in with `PI_CODING_AGENT_DIR=~/.pi/dromon-agent pi` and `/login`, choosing the
   ChatGPT sign-in for OpenAI, and confirm the directory then holds no `mcp.json` or
   `models.json` and that its `settings.json` passes Dromon's check.
2. Run `PI_CODING_AGENT_DIR=~/.pi/dromon-agent pi auth check --provider openai --json
   --no-refresh` with `OPENAI_API_KEY` unset and confirm `"authType":"oauth"`. Then set
   `OPENAI_API_KEY` and confirm whether the answer changes, which shows the order in
   which Pi prefers a stored sign-in and a key.
3. Confirm `pi --mode rpc` with Dromon's arguments answers `get_state` with
   `model.provider` `openai` and `model.id` `gpt-6-luna`.
4. Confirm that the probe's turn reports the credential `subscription`, a reply, and a
   cost and token usage, and that a `sessions` file appears in the dedicated directory.
5. With a line in a running daemon, confirm that `mcp__dromon__send_message` is offered
   and used, and that asking the model to run a shell command yields no tool call, or a
   refused turn.
6. Confirm that a second turn resumes the same session file.
7. Confirm that an interrupt from the driver, such as a small tool budget on the API
   driver, ends the run with `agent_settled` and `aborted` true.
8. **Review F6.** Confirm that the `dromon mcp --line` child Pi starts exits with Pi,
   both after an orderly turn and after Dromon terminates Pi. A child that survived
   would hold the line's token and its connection to `lines.sock`. Dromon now signals
   Pi's whole process group, so only a child that starts a session of its own could
   survive. **Fact, from
   `agents.py`.** Each line has one token for the daemon's life, held in memory and
   reused by every engine build, not a new one per build, so a surviving child's token
   works until the daemon restarts.
9. **Review F1.** Confirm that with `"cacheWarming": "off"` no request follows the
   turn's last one, and that a compacting turn's cost includes the summary call.
10. **Review F7.** Before the first live turn, the operator confirms against the ChatGPT
   plan's terms that the plan may be drawn on through Pi.
