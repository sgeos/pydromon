# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- The subscription drivers allow a development turn: 200 engine turns and 400 tool
  calls for Opus, GPT-6-Luna and Grok, and 100 and 200 for Haiku. The API and flexible
  drivers keep their small budgets.

- pydromon is set up to develop itself with Dromon, as `docs/process/DOGFOOD.md`
  records. pytest's collection now ignores `.dromon`, which a Codex line's profile
  denies outright, so a line can run the gate in the project it works on.

- Synced with `sgeos/project-template` a second time. The repository gains
  `CODE_OF_CONDUCT.md`, linked from the README, `CONTRIBUTING.md` and `llms.txt`,
  `.github/CODEOWNERS`, `.github/release.yml`, an issue template `config.yml` that
  routes security reports to private advisories, `docs/decisions/TEMPLATE.md`,
  `scripts/worktree.sh` and an opt-in `scripts/hooks/pre-push`. `scripts/check.sh` takes
  the template's hygiene checks, which add Markdown link, text, size and executable
  checks, require shellcheck, and scan for the terms in an untracked
  `secret/disclosure-terms.txt`, and it takes `--hygiene-only`. CI grants no permissions
  by default, installs shellcheck on macOS where missing, and lints its workflows with
  actionlint and zizmor. Dependabot groups action updates weekly. Commit messages no
  longer carry a `Co-Authored-By` trailer, on the operator's instruction.

- CI groups concurrent runs by the pull request's ref and otherwise by run, as the
  Keleusma repository does, so a second merge to `main` no longer cancels the first
  merge's run. The test matrix runs at most two jobs at once, and Linux jobs require
  shellcheck on `scripts/`.
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

- `docs/architecture/KELEUSMA_DOGFOOD.md` records what Dromon needed to run Keleusma's
  four lines, the read-only trial of those lines, and a validated Keleusma project file.

- `rebind_line` and `dromon rebind LINE --model M --strategy S` move a line to another
  model and driver strategy, recorded as the new `line_rebound` event. The line keeps
  its identity, mailbox, configuration and overrides, its model override is dropped,
  its engine session is cleared and its engine rebuilt. A rebind that changes nothing,
  has no driver or adapter, or that the new engine cannot run is refused, as is one
  during a turn. It is operator only. The project file's check of a line's model and
  strategy compares with the current binding.
- `project_settings`, a line key, starts a Claude Code line with Claude Code's project
  setting source, so its working directory's `.claude/settings.json`, skills and
  `CLAUDE.md` load. At the start of every turn, what it would load is checked against
  allowlists: the settings file may set only `$schema`, `attribution` and
  `includeCoAuthoredBy`, `.claude` may hold only the settings files, `skills` and
  `CLAUDE.md`, and a skill's front matter only `name`, `description`, `license` and
  `version`. Anything else refuses the turn, hooks and permissions included. A shell
  line needs an enabled sandbox, and every such line's sandbox and `Edit` rules deny
  writes to the working directory's `.claude`. Codex and Grok lines refuse it.
- Leases. A project's `[leases]` table declares names and a time to live, and lines
  gain `acquire_lease` and `release_lease`. A lease held by any line, the caller
  included, cannot be acquired until it ends. A lease ends when released, when its time
  passes, when its holder's session is cleared or its holder is rebound, and at a
  restart, since leases live in memory. `status` shows held leases.
- Packaged drivers for Claude Fable 5.1 on Claude Code in the three strategies, copying
  the Opus 5.5 settings. No live turn has run on it.
- Lines gain `wake_after`, which schedules one wake from 60 to 86,400 seconds ahead,
  replacing any pending one. It is recorded as the new `wake_scheduled` event with its
  due time in Coordinated Universal Time, and fires at the first tick after the due
  time by recording the note as a message from the line to itself, the one
  self-addressed message the event model admits. Firing is one append, so it survives
  a crash or a restart without firing twice. `status` and `line_view` show the pending
  wake, and the scheduler's wall clock is injected.
- `daemon.require_sandbox` in the project file refuses a Claude Code line whose merged
  sandbox is not enabled, at start, `create_line`, `configure_line` and reload, naming
  the line. Codex and Grok lines satisfy it.
- `daemon.disclosure_terms` names a disclosure list, read at start and reload. A note,
  question, choice or wake note that a line or an external agent sends through
  Dromon's tools is refused, and nothing recorded, when it contains a listed term
  without regard to case, and the refusal never names the term. Operator messages are
  not checked. The settings view shows both keys, the list by its path only. A wake
  note is checked again when it fires, and fires as a fixed notice if a reloaded or
  restarted list now covers it. An anonymous external agent whose chosen name contains
  a listed term is refused. Agents registered with `agent_add` are exempt.
- A line's configuration takes `workdir`, the directory its engine runs in, so a line
  can work in a git worktree of its own. It is set in the project file, or with
  `--workdir` on `dromon create` and `dromon configure`. A relative path resolves
  against the project root, and the directory must exist and may not be, lie in or
  contain the state or runtime directory, the project root excepted. Claude Code's
  working directory, the Codex workspace and the Grok agent's working directory follow
  it, as do the line's relative instruction and sandbox paths. A changed `workdir`
  appends `line_session_cleared`, since Claude Code keeps a session per directory, and
  a client cannot change it through `set_setting`. The settings view shows it.
- `create_line` takes `engine_session`, `dromon create` takes `--session`, and a project
  file line may give `session`, so a new line can continue an existing engine session.
- Every enabled Claude Code sandbox denies writes to the hooks and configuration of the
  git checkout its line works in, found by reading `.git` and `commondir` without
  running git, and for a linked worktree also to its `.git` file and `commondir`, so a
  line cannot plant a hook that git would run outside the sandbox.
- Settings for lines and for the project can be shown and changed from a client, as
  overrides layered over the project file and recorded in the event log as the new
  `line_overridden` and `project_overridden` events. The attached client gains
  `/config`, a settings panel with each value's layer, `/model`, `/effort`, `/set`
  and `/unset` with completion of keys and values, `/clear` for a new engine session,
  `/create` for a new line and `/reload` for the project file. The daemon gains the
  `settings`, `set_setting`, `clear_session` and `reload_project` operations and the
  `line_session_cleared` event, and the command line gains `dromon settings`,
  `dromon set` and `dromon reload`. A line may switch models within its engine. The
  command line of `dromond` still wins over a project override, which is reported as
  shadowed.

- The attached clients are rebuilt on a shared core in `dromon.ui`, near parity with
  Claude Code, Codex and Grok Build. The core is a pure state machine with no input or
  output, the runtime performs its effects through a `Daemon` port, and the curses and
  Tk platforms only translate keys and paint frames. The clients follow a turn as it
  runs in an activity pane, with a mail pane on Control-O, a status bar, a card for
  open questions, a multi-row input with readline keys and history, slash commands
  with Tab completion of commands, lines, questions, messages and offered choices, a
  help panel on `?`, a line list and line switching, scrollback that keeps a reader's
  place, Escape to interrupt a turn, a double Control-C to detach, and a bell for a new
  question. `dromon.attach`, `dromon.tui` and `dromon.gui` are removed.
- The daemon keeps a bounded, in-memory activity tail per line, fed by each engine
  adapter, and serves it through the new `line_activity` operation. The new
  `interrupt` operation cancels a line's turn in flight.

- Sync with `sgeos/project-template`. `scripts/check.sh` is the local gate that CI now
  runs, with the hygiene checks, `uv sync --locked`, Ruff, pyright, pytest with its
  arguments and shellcheck. Adds `.gitattributes`, `SECURITY.md`, `CONTRIBUTING.md`, a
  pull request template, `.github/rulesets/main.json`, issue templates from the
  Keleusma and angelssword repositories, `llms.txt`, and README badges. `.gitignore`
  and `.editorconfig` take the template's entries, and CLAUDE.md and AGENTS.md its
  wording on branches, local and remote passes, scratch and verification.
- Nominal Grok support, not verified against a live turn, specified by
  `docs/protocol/GROK.md`. Drivers for Grok 4.7 on the new `grok-build` engine, and
  `dromon.grok`, which drives `grok agent stdio` over the Agent Client Protocol. A Grok
  line runs under an operator-defined `dromon-reader` or `dromon-writer` sandbox profile
  that Dromon checks, must report Grok's ask mode, has every permission request
  answered by Dromon, and reaches Dromon's tools through `dromon mcp --line` and the new
  `lines.sock`, with a per-line token the daemon holds in memory.
- Codex lines can write files. Every Codex thread runs under `dromon-line`, a Codex
  permission profile that makes the workspace writable for a line given `Edit` or
  `Write`, keeps `.git` read-only, and denies Dromon's state and runtime directories to
  reads and writes. It replaces Codex's read-only sandbox, which let a Codex line read
  the event log. The thread must report the profile as active.
- `dromon teacher up` checks available memory before loading a model, on macOS from
  `kern.memorystatus_level` and on Linux from `MemAvailable`, and refuses below the
  model's size plus `memory_headroom_gb`, 4 by default, unless given `--force`. `status`
  reports available memory.
- External agents, specified by `docs/protocol/AGENTS.md`. Another harness starts
  `dromon mcp --agent NAME` as an MCP server and reaches the daemon as the party
  `&name`, with its own mailbox, through a separate agent socket. The project file's
  `[agents]` table sets `listen`, off by default, `anonymous` and `max_access`, over the
  levels `none`, `observe`, `message`, `manage` and `operator`. `dromon agent add`,
  `list` and `revoke` register agents, whose tokens are stored only as digests. The
  event log gains `agent_registered` and `agent_revoked`.
- A project's local teacher model, specified by `docs/protocol/TEACHER.md`. The project
  file's `[teacher]` table names an Ollama model on the loopback interface, and
  `dromon teacher up`, `status` and `down` start a server when none answers, load the
  model with the declared context, and release it, stopping only a server Dromon
  started. Dromon never pulls a model. With `autostart`, `dromond` brings it up.
- Lines take `claude_code_prompt`, to run on Claude Code's own system prompt instead of
  an empty one, and `instructions`, files such as `CLAUDE.md` appended to the system
  prompt at each turn, or given to Codex as developer instructions. `dromon create` and
  `configure` gain `--claude-code-prompt` and `--instructions`.
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
