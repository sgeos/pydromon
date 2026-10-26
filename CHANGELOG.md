# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A prospective Pi engine, `dromon.pi`, for `pi:openai/gpt-6-luna`, which runs a line's
  turns through `pi --mode rpc` and is not verified against a live turn, as
  `docs/protocol/PI.md` records. Pi has no sandbox, so a Pi line takes no built-in tool
  and reaches only Dromon's own tools, through a packaged extension that registers them
  and blocks every other call. Pi runs on a dedicated agent directory, and `pi auth
  check` tells a subscription sign-in from an API key before each turn. After review,
  that directory must switch Pi's cache warming off and hold no `SYSTEM.md` or
  `APPEND_SYSTEM.md`, a compaction's summary call counts towards the turn's cost,
  endpoint and credential-location variables are stripped too, and a Claude sign-in
  through Pi is not taken to be a subscription.
- Pi, Cursor Agent and Antigravity start in a process group of their own, which Dromon
  signals whole, so a child cannot outlive the turn. A turn on any of them has a
  deadline of an hour, after which it is stopped and refused. A subscription line on
  any of them starts without any cloud provider's variable, by prefix, or any variable
  named as a credential or password.
- A prospective Cursor Agent engine, `dromon.cursor`, for
  `cursor-agent:gpt-5.6-luna-medium`, which runs a turn through `cursor-agent --print
  --output-format stream-json` on a configuration directory Dromon writes for the turn,
  as `docs/protocol/CURSOR.md` records. Cursor's built-in tools cannot all be switched
  off or shown to be denied, so a daemon refuses Cursor lines, and only `dromon probe
  --accept-partial-isolation` runs a Cursor turn. After review, any `.cursor` directory
  in the working directory or above it refuses the turn, as do most entries of
  `~/.cursor`, the turn runs on an empty data directory of its own, any event type the
  adapter does not name refuses the turn, and a tool reported only on completion is
  refused too. A resumed chat is refused, since the turn's data directory holds none,
  so a Cursor line runs a single turn until a kept data directory is designed.
- A prospective Antigravity engine, `dromon.antigravity`, for
  `antigravity:gemini-3.8-flash-high`, which runs a turn through Google's Antigravity
  ACP runtime, `agy_acp_server`, by the operator's decision of 2026-10-26, as
  `docs/protocol/ANTIGRAVITY.md` records. It replaces a print-mode adapter for
  `agy --print`, which was never released. It rests on the protocol's schema and a
  second implementation, and the runtime is not installed, so nothing is verified
  against a live turn. Dromon installs nothing. It finds the runtime the operator
  installed, refuses a missing, incomplete or older one as the ACP runtime missing,
  sets the runtime's profile beside it, and refuses a profile with a key file, hooks,
  MCP servers, approvals or another sign-in method. It chooses the Google account
  sign-in from the methods the runtime advertises. Dromon's tools reach the session as
  its MCP server, a permission request for any other tool is refused before it runs,
  and any other tool call, unknown update, request or notification refuses the turn.
  A daemon still refuses Antigravity lines, since a tool the runtime runs without
  asking can be stopped only after it starts, and `dromon probe
  --accept-partial-isolation` reaches it. The credential stays `unverified`, which no
  packaged driver accepts, unless the probe is also given
  `--accept-unverified-credential`, the operator's confirmation by hand that the
  runtime is signed in to a plan. The probe's help says that neither flag changes the
  strategy. After review, every tool call and every update to one is checked whatever
  its kind, a call whose name, title or kind changes refuses the turn, a tool is
  Dromon's own only by its structured `toolName`, and any update that names a model
  other than the driver's refuses the turn. Profile settings are an allowlist, and the
  profile must name the Google account sign-in. The runtime starts with a home
  directory of the turn's own and keeps its sign-in in the profile. Its reported usage
  is counted, labelled unverified, and a turn that compacts is refused unless its cost
  is reported. A turn with no reported cost says so in its usage note. The release location, version and checksums taken from the second
  implementation are recorded as unverified hypotheses, and the procedure has no
  download step until they are confirmed.
- `dromon.acp`, the Agent Client Protocol client that Grok Build and Antigravity share,
  holding the request loop, Dromon's answers and the shapes it reads, so that neither
  adapter keeps a second copy. A Grok tool call is now one of Dromon's own tools only
  by its structured `toolName`, not its title, so a call with only a title is judged
  by its kind, as `docs/protocol/GROK.md` records. This fails closed. Grok no longer
  lets a `think` call through unchecked, and like Antigravity it judges each tool call
  when first seen and refuses a turn in which a later update or permission request
  changes the call's name, title or kind. A refused permission request now cancels the
  turn, and every request after a cancel is answered as cancelled. A Grok that ignores a
  cancel is stopped after 60 seconds rather than holding the line, and `GROK.md` has a
  verify-first list whose first step confirms that Grok names Dromon's tools in
  `toolName`.
- Models on harnesses that run other vendors' models are named `ENGINE:ID`, as
  `docs/protocol/DRIVER.md` records.

### Changed

- `pykeleusma` is pinned to its first PyPI release, `0.2.2.0`, in place of commit
  `19d4558` of its repository. Every call Dromon makes is unchanged, and the packaged
  drivers run as before. The release has wheels for Python 3.12 or later on Linux,
  macOS and Windows, so CI no longer installs a Rust toolchain, and the README no
  longer asks for one. CI sets `UV_NO_BUILD_PACKAGE=pykeleusma`, so a wheel that
  stopped matching fails the run instead of building from source unnoticed.
- A compaction's `trigger` is only what the engine reported, with a `trigger_note`,
  so a Codex compaction, which reports none, no longer shows a `manual` that Dromon
  made up. A Codex compaction is reported as such even when Dromon had no earlier
  estimate of its size. The context note for a call no longer uses Claude Code's
  wording on Codex lines.

- The gate runs pyright for Linux and for macOS, since pyright skips another
  platform's branch, which let a Linux-only type error reach `main` in pull request 42.

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

- Round 2 of the session-line experience. A Claude Code line with `keep_session`
  keeps its engine connected between turns, so commands started in the background
  outlive the turn and the engine's `TaskStop` works across turns. Every per-turn check
  still runs, and a reused connection is made to report its `init` again, with the
  turn refused when it does not. The gate denies tool calls outside a checked turn, a
  turn the engine begins by itself is interrupted and noted, and the connection closes
  on a configuration change, a refused or failed turn, the daemon's stop, after
  `keep_session_idle_seconds`, or after `keep_session_max_seconds` while background
  tasks run. A dead engine is resumed with a note that its tasks were lost.
  `line_status` reports the kept session and its deadline, and whether the daemon's
  checkout has moved since it started, with a note that version 0.0.0 is unreleased.
  `read_instructions` returns a size, digest and sources unless `full` is set. The
  unanswered count leaves out replies to hold questions, and `list_mail` entries carry
  `repeat_of` and a fired wake's due and delivery times.

- Round 7 of the session-line experience. The engine's report of a compaction that
  `compact_session` asked for withdraws the wake that call scheduled, through the new
  `wake_withdrawn` event, and holds it back while the compacting turn runs, so it no
  longer starts a turn for nothing. After a restart the wake still fires, as its note
  says. A `self_started` or `interrupted` entry says whether the engine reported a
  cost and tokens of zero or reported nothing. The context note names the compaction
  as the estimate's source until a turn's end replaces it. On Codex, the first engine
  call after a compaction whose size was not reported gives
  `last_compaction.after_estimate_tokens`, an upper bound that includes the turn's
  prompt, which feeds the estimate and the `auto_compact_tokens` hold at once.
  From its review, a withdrawn wake's identifier cannot be taken again, a withdrawal
  that fails no longer loses the compaction's record, and the wake is held from the
  turn's dispatch rather than from its start.

- The round 6 review. Automatic compaction is held after a compaction whose size
  after is unknown or still past the threshold, until a lower context is reported,
  which `line_status` shows as `auto_compact_held`. A compaction request belongs to
  the turn it was made for, on both engines. A Codex compaction with no call seen
  reports its usage as unknown. The compaction wake's note says that a restart loses
  the request.

- Round 6 of the session-line experience. `compact_session` schedules its own wake, 60
  seconds ahead by default, unless one is pending. A turn that begins with a
  compaction gives the context warning after it, so a compacted line is not told to
  compact again. A task note states its exit code as a sentence of its own or says
  that none was reported, and reads the form `failed with exit code 1` too. The line
  key `auto_compact_tokens` compacts a turn first once the estimate reaches it.
  `recent_turns` shows a compaction's own cost and tokens, and the cost of a turn the
  engine began by itself or an operator interrupted as an entry of its own that
  counts toward the budget. Codex lines report each call's tokens, their context and
  their turn's tokens, and may compact through `thread/compact/start`. A live probe
  showed that Codex reports the compaction as a `contextCompaction` item and not by
  the `thread/compacted` notice, so the item is read, and the compaction's own usage
  notice is not counted as a call. `last_compaction` gains a `cause`, and every
  `recent_turns` entry has a `compaction` field.

- The round 5 review. A pending compaction is dropped on a configuration change and
  checked again against `allow_compact` when consumed, and none is sent after a
  refused init. A Codex completion counts only for the line's own item, in a turn
  too, and the identifier cap forgets the oldest entries one at a time, keeping any
  still needed.

- Round 5 of the session-line experience. `line_status` estimates the line's context
  from each engine call's usage and lists the calls of each recent turn, and the
  prompt warns past `context_warn_tokens`. A Claude Code line with `allow_compact` has
  `compact_session`, which begins its next turn with `/compact` and reports the
  context before and after. Task-end notes state the output's state plainly and give
  when the end was detected and delivered, and `line_status` keeps the last one's
  lateness. A Codex terminal's end carries the exit code and output of its completed
  command.

- The round 4 review. A Claude Code turn's cost and tokens come from the engine's
  own per-turn usage where its result carries it, kept through a session wrapper
  that reuses the SDK's parser, with the difference of running totals as the
  fallback, and `line_status` says which. The task output reader walks from a
  directory descriptor without following links, and reads only `.output` files in a
  `tasks` directory, in the working directory too. A test shows that the SDK's failed
  connect ends the engine process before the start is retried. The undercount a
  same-session restart can cause is recorded as a residual.

- Round 4 of the session-line experience. The cost mark is recorded as
  `line_cost_marked` after each turn that reports a new total, so a restart does not
  count a session's restored history again. A task-end note folds in the turn the
  engine began on the end, and gives the exit code and the first lines of the output,
  read only from the line's working directory or the engine's task output and quoted
  as data. A line's notes to itself leave the unanswered count. `line_status` gives
  each recent turn's tokens and says when an engine reports no cost, and says whether
  the turn in flight counts as autonomous, which the prompt now says too. Codex
  background terminals have one source for the status and `list_background`, match
  numeric identifiers, and wake the line when one ends, found by bounded polling.
  `dromon start` waits up to 60 seconds, or `--wait` seconds. An engine start that
  times out at its initialize request is tried once more, with a `notice` in the
  activity log.

- The round 3 review. A Claude Code turn's cost is the difference from the last total
  reported for the same engine session, kept across engine rebuilds, and the whole
  total when the session changes or the total falls. The engine restores a resumed
  session's total, so this also stops turns that are not kept from counting their
  session's history. A task-end note quotes the command as the line's own data. A
  kept Codex connection never interrupts without a turn identifier, lists and stops
  only terminals of commands its own turns ran, and writes each message whole under
  a lock.

- Round 3 of the session-line experience. A kept connection's turn now costs the
  difference from the previous result's running total, which had been added whole. A
  background task that ends between turns wakes its line with a `task_noted` note, at
  most once per task and three a minute. `line_status` lists running tasks with their
  commands, the cost of the last ten turns, and the last fired wake's lateness. An
  operator's action that stops a kept session's tasks says how many in its response.
  Codex lines may keep their app-server between turns, with every check run again on
  the live connection, and gain `list_background` and `stop_background` for their own
  thread's background terminals. The lines' rules ask for tracked background tasks
  instead of `nohup`.

- The round 2 review. No Claude Code turn admits a tool call before its init check,
  on any connection, and a refusal can no longer be cleared by a later init. An idle
  kept connection closes as soon as a plugin or `.claude` file it loaded changes. The
  watch survives its own errors, interrupts and closes begun outside a turn are held
  until they end, and a kept line with a shell tool needs an enabled sandbox.

- Lines gain `list_processes` and `stop_process`. Every command a line runs carries
  `DROMON_LINE_MARK`, the line's identifier and a random token of its own issued per
  daemon run, including commands that outlive a turn, so no line can present another
  line's mark. Both tools run off the event loop, and on macOS one `ps` call reads every
  environment. At start the daemon tests whether it can read a child's environment at
  all, and where it cannot, as on macOS from Darwin 27 on, both tools and
  `line_status` say that this platform does not let it identify a line's processes. A line sees and
  signals only processes of the daemon's user that carry its own mark, never the
  daemon or an engine, and the mark is read again when stopping. An unreadable
  environment counts as not the line's. On Codex the mark reaches only the commands it
  runs, through `shell_environment_policy.set`, so Codex's own processes are never
  listed. `DOGFOOD.md` records three requirements that stay in the engine for now.
- A Codex line's stored session now carries a fingerprint of its Dromon tools. A thread
  started with other tools is not resumed, since `thread/resume` takes no
  `dynamicTools`, and the new thread's first turn tells the line so.
- Lines gain `line_status`, which shows the line's hold, autonomous turns and cost with
  their limits, leases, scheduled wake, unread mail, model, working directory, engine
  session, the daemon's version and the commit it runs from, read from `HEAD` without
  running git, and the other lines' state, read only. Every turn's prompt now opens
  with the autonomous turns and cost against their limits.
- Lines gain `read_instructions`, which shows the instructions their next turn
  receives as the engine adapter composes them, and `write_scratch`, which writes a
  file under `tmp/` in the line's working directory without a file tool, walking each
  component by descriptor without following links so no swapped link can redirect it,
  and refusing text or a path the disclosure list covers.
- A message that repeats one with the same sender, recipient and body from the last
  15 minutes is marked as a repeat in the turn prompt, and a fired wake's header says
  when it was due and when it is delivered. The state keeps fired wakes' due times.
- Codex lines start with `XDG_CACHE_HOME` under the system temporary directory unless
  the operator sets it, so uv and other tools have a writable cache. The directory is
  created with mode 0700 and refused when another user owns it or others can write it.
- `plugins`, a line key, lists local plugin directories a Claude Code line loads
  through the Agent SDK's `plugins` option, so no user setting source opens. Only
  operator authority sets it. At every turn each plugin must pass allowlists of its
  entries, its manifest and listing keys and its skills' front matter, so a plugin with
  hooks, MCP servers, commands, agents, output styles or language servers refuses the
  turn. A listing entry's source must stay inside the plugin, `skills` may hold only
  skill directories, and front matter admits only allowlisted keys at column zero and
  block scalar text. A line that loads plugins or project settings needs an enabled
  sandbox whatever its tools, and the sandbox and `Edit` rules deny writes to each
  plugin directory. A line that uses a plugin's skill lists `Skill`, and
  the init check still refuses any tool the line was not given. Codex and Grok lines
  refuse it.
- `docs/architecture/KELEUSMA_DOGFOOD.md` records what Dromon needed to run Keleusma's
  four lines, the read-only trial of those lines, and a validated Keleusma project file.

- `rebind_line` and `dromon rebind LINE --model M --strategy S` move a line to another
  model and driver strategy, recorded as the new `line_rebound` event. The line keeps
  its identity, mailbox, configuration and overrides, its model override is dropped,
  its engine session is cleared and its engine rebuilt. A rebind that changes nothing,
  has no driver or adapter, or that the new engine cannot run is refused, as is one
  during a turn. It is operator only. The project file's check of a line's model and
  strategy compares with the current binding, and the settings view labels the
  model's source `rebound`.
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
