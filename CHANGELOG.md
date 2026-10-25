# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Questions no longer block. The `blocking` field leaves `message_sent`, notices and the
  `ask_operator` tool. After asking, a line ends its turn or keeps working, as it
  decides, and the answer arrives as mail.
  Dromon's tools now reject arguments outside their schema.
- The scheduler is a game loop. Each tick updates every line once and skips a busy one,
  turns run without the tick waiting for them, and mail sent during a tick moves from
  the sent queue to the post office at its end. `dromond` gains `--tick-seconds`.
- The scheduler is tick based. A message sent in one turn is received no sooner than
  the next tick, even by an idle line, and messages never interrupt a turn. `status`
  reports the tick.
- Driver documentation no longer claims that Keleusma resets private data. It persists
  once assigned, as the `pykeleusma` line pointed out and this line confirmed.
- `line_created` gains a `strategy` field naming the line's driver strategy.
- The build backend requirement moves to `uv_build` 0.13, matching the installed uv.
- `docs/protocol/RELAY.md` requires every relay adapter to support a response path and
  states the properties its deferred design must have.
- Operators are no longer a singleton. Each operator is a party with an address such as
  `@ana`, registered by a new `operator_registered` event. An operator question is a
  `message_sent` of kind question from a line to one operator, and the answer is that
  operator's reply, which must reverse the question's route. `question_asked`,
  `question_answered` and the wake cause field are removed, and messages gain `kind`,
  `choices`, `blocking` and `reply_to`. The draft format version stays 1.
- `docs/architecture/KELEUSMA_INTEGRATION.md` records the `pykeleusma` line's answers to
  the four questions, the binding's current task status, and the tagged-tuple
  constraints it reported. One-element tuples in the examples became two-element ones.
- `CLAUDE.md` states that dates in tracked files are in Coordinated Universal Time.
- The design records two operator decisions. The protocol specification is drafted in
  this repository, and the first adapter targets Claude Opus 5.5.
- The design records what Anthropic's terms allow for subscription sign-in, the open
  choice of the first adapter that follows from it, and that standard harness features
  such as compaction land with the task that first needs them.
- `docs/process/HANDOFF.md` names the interline line `pydromon` as this line's mailbox.
- Roadmap task 1 is done. The seed was checked against Appendix B and nothing conflicts.

### Added

- OpenAI models run on Dromon through Codex, specified by `docs/protocol/CODEX.md`.
  `dromon.codex` drives `codex app-server`, maps the reported account to the driver's
  credential, disables every MCP server and connector feature Codex would load, and
  serves Dromon's tools as dynamic tools. Lines can be created on `gpt-6-luna`, and
  `dromon probe --model gpt-6-luna` runs on Codex. Codex lines take only `Bash`, no MCP
  servers and no sandbox or settings documents.
- The reserved variables of engine settings add the `OPENAI_`, `CODEX_` and `XAI_`
  prefixes.
- Drivers for GPT-6-Luna on Codex in all three strategies, with settings from Codex's
  model catalogue. Driver interface 3 adds the `engine` each driver is written for. A
  line can be created only on a model whose engine has an adapter, and each adapter
  refuses a driver written for another engine.
- `docs/architecture/DESIGN.md` records whether the Claude Code conclusions on
  subscriptions, tokens, billing and account tools hold for Codex and Grok Build.
- Engine settings at parity with the settings actually used on the operator's machine
  in Claude Code, Codex and Grok Build, specified by `docs/protocol/SETTINGS.md` with
  the review behind them. Permission rules, command hooks, `env` and `attribution`, in
  Claude Code's key names, may be set in `[settings]` for the project, per profile and
  per line, or with `dromon create` or `dromon configure --settings-config FILE`, and
  reach Claude Code through its `--settings` flag. Layers merge as Claude Code's scopes
  do. An `ask` rule denies, `env` may not change the engine's credential or provider,
  and every line's file tools are denied Dromon's state and runtime directories.
- A line's `effort`, in the line configuration, the project file and `--effort`,
  replaces its driver's reasoning effort.
- MCP servers may refer to the daemon's environment as `${NAME}` or `${NAME:-default}`.
  Only the reference is stored, and the engine expands it at the start of each turn.
- Sandboxing at parity with Claude Code, specified by `docs/protocol/SANDBOX.md`. A
  Claude Code sandbox document, in Claude Code's key names, may be set for the whole
  project in `[sandbox]`, for a profile or line in `.dromon/dromon.toml`, or for one
  line with `dromon create` or `dromon configure --sandbox-config FILE`. Layers merge as
  Claude Code's settings scopes do. An enabled sandbox fails closed, refuses
  unsandboxed retries and hosts outside the allowlist, and always denies Dromon's state
  and runtime directories. Keys that remove a boundary need the client's operator
  authority. `dromon config` reports each line's sandbox.
- Continuous integration, roadmap task 10. `.github/workflows/ci.yml` runs Ruff,
  Pyright and pytest, socket tests included, on Linux and macOS for Python 3.12 and
  3.15. Actions are pinned to commit hashes, the token is read-only, checkout keeps no
  credentials, and Dependabot proposes action updates after a seven-day cooldown.
- Per-project configuration in `.dromon/dromon.toml`, specified by
  `docs/protocol/PROJECT_CONFIG.md`. It declares lines through reusable profiles, with
  per-line limits and wake policy, and is validated strictly with located errors.
  `dromond` applies it at start, and its flags now override the file. `dromon config`
  validates it without a daemon.
- The interactive client, roadmap task 8. `dromon attach LINE` opens a curses client
  attached to one line, and `--gui` opens a Tk window instead. Both poll the new
  `line_view` operation through `dromon.attach` and accept typed messages and the
  `/answer`, `/ack`, `/release`, `/help` and `/quit` commands. Any number of clients
  may attach at once.
- Conversation control, roadmap task 7. `dromon.limits` holds a line after 12 turns in
  a row without operator mail or $2 of estimated cost, with an optional cooldown, and
  asks an operator whether to resume it. New `line_held` and `line_released` events,
  the `release` operation and `dromon release`. `dromond` gains
  `--max-autonomous-turns`, `--max-cost-usd`, `--cooldown-seconds` and `--operator`,
  replacing `--max-turns-per-line`.
- Acknowledgements and sent mail. `message_acknowledged` marks a message answered
  without a reply. Senders see each sent message as replied, acknowledged or
  unanswered, through the `list_sent` tool for lines and `dromon sent` for operators,
  and recipients acknowledge with `acknowledge_mail` or `dromon ack`.
- Mailboxes. Every party has one, with read state recorded by a new `message_read`
  event. A line's wake prompt opens with "X new message(s). Y unread. Z unanswered."
  and lists the new mail's headers, and `list_mail` and `read_mail` tools page through
  the mailbox and open messages. Operators use `dromon mail` and `dromon read`, through
  new `mail` and `read` operations. Nobody is obliged to answer anything.
- Operator questions, roadmap task 6. Lines ask through an in-process `ask_operator`
  tool. The daemon keeps the queue, `dromon questions` lists it, `dromon answer`
  replies, and the answer wakes the asking line in a later tick. New `questions` and
  `answer` operations.
- A `line_session` event records each line's engine session, so a restarted daemon
  resumes every line's conversation.
- Per-line tool configuration, `dromon.lineconfig` and `docs/protocol/LINE_CONFIG.md`.
  A line may be given built-in tools and arbitrary MCP servers under an allow-all or an
  allowlist policy, stored as a new `line_configured` event. `dromon create` takes tool
  options, and `dromon configure` and the `configure_line` operation change them.
- Layer 1 messaging and wakes, roadmap task 5. `dromon.scheduler` wakes an idle line
  when a message arrives, delivers its pending messages in one turn, and wakes it again
  after a busy turn. Lines send through an in-process `send_message` tool, and operators
  through the new `dromon create` and `dromon send` commands and the `create_line` and
  `send` operations. `dromond` gains `--max-turns-per-line` and `--hold-wakes`.
- Tool isolation for lines. `strict_mcp_config` keeps a claude.ai login's connectors
  out of every line, a permission gate allows only the line's own tools, and a turn
  whose engine offers any other tool is refused.
- The first engine adapter, roadmap task 4. `dromon.engine` runs a session line's turns
  on Claude Code through the Agent SDK, on the operator's Claude subscription only, and
  refuses any other credential. `dromon.driver` hosts per-model Keleusma drivers through
  `pykeleusma`. Six concrete drivers cover Claude Opus 5.5 and Claude Haiku 5.5, each
  with a subscription, an API and a flexible strategy baked in. `dromon probe` reports
  the engine's capabilities and defaults to Haiku 5.5, the least expensive model.
  `docs/protocol/DRIVER.md` specifies driver interface 2.
- Dependencies `claude-agent-sdk` and `pykeleusma`, the latter pinned to commit
  `19d4558` of its repository.
- The daemon skeleton, roadmap task 3. `dromond` serves one project on a Unix domain
  socket in a private runtime directory, holds the event log as its single writer and
  admits one instance through a lock. `dromon start`, `dromon status` and `dromon stop`
  manage it, with distinct exit statuses in `dromon.exits`. `docs/protocol/CLIENT.md`
  specifies the versioned line-delimited JSON protocol.
- The decision that replies arrive through inbound adapters plugged into a registry in
  the daemon and configured per channel, recorded in `docs/protocol/RELAY.md`.
- `dromon.relay` and `docs/protocol/RELAY.md`, forwarding of operator messages through
  relays, a general adapter interface for channels such as email or chat, with a
  command relay that hands each notice to a configured command as JSON.
- The data model and event log, roadmap task 2. `dromon.model` defines session lines,
  messages, wakes and operator questions as ten event kinds with explicit transition
  checks. `dromon.codec` encodes them as canonical JSON and decodes untrusted payloads
  strictly. `dromon.eventlog` stores them in an append-only SQLite log that replays to
  the same state, detects a second writer, reports corruption by sequence number and
  creates new logs readable by their owner only.
- `docs/protocol/EVENT_LOG.md`, the normative specification of the above, the first
  document of the protocol specification.
- `docs/architecture/KELEUSMA_INTEGRATION.md`, what Dromon needs from `pykeleusma`,
  mapped to that binding's tasks, for the `pykeleusma` line.
- The repository seed. The `pydromon` distribution with the import package `dromon`, the
  placeholder commands `dromond` and `dromon` with seven planned subcommands, smoke and
  command tests, the design, the naming and port decision, the 0.1.0 milestone and the
  process files. Ignored project-local `tmp/` and `secret/` directories, following the
  Keleusma repository.
