# Design

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

The planned shape of Dromon, seeded on 2026-10-25 from a design conversation with the
operator. Each point is marked **Decided** when the operator stated it, **Proposed** when
it is the seeding session's suggestion, and **Open** when it still needs a decision.
Nothing here is implemented.

## Purpose

**Decided.** Dromon is a harness that holds several agent session lines at once. It runs
as a daemon, has non-trivial per-project configuration, and lets its session lines
communicate. It grew out of multi-session-line development on Keleusma and other
projects, where agents that could talk to each other did better work. It is built to
solve the communication problem directly, including waking a session that has a new
message, which a shared mailbox alone cannot do. The harness also tracks questions for
the operator as a first-class feature.

## Terms

| Term | Meaning |
|---|---|
| Session line | One long-lived agent working on a line of work, together with its history and state |
| Engine | The model and agent loop that a session line runs on, either an external harness or Dromon's own loop |
| Daemon | `dromond`, the process that owns the session lines, the event log and the client socket |
| Client | A curses or graphical program, or the `dromon` command, that connects to the daemon |
| Wake | Starting a session line's next turn because something arrived for it |
| Operator | A person with authority over a project's lines, addressed as `@name`. A project may have several |
| Operator question | A question a session line sends to one operator, tracked until it is answered, withdrawn or superseded |
| Stream processor | In the Keleusma version, the per-model program that handles one turn or sub-turn step |

## Process model

**Decided.** `dromond` holds the session lines. Clients connect to the daemon. The
operator can also use sessions held inside the daemon. The client command is `dromon`,
whose subcommands cover starting and stopping the daemon, status, attaching an
interactive client, sending messages and handling operator questions.

**Implemented 2026-10-25, roadmap task 3.** Clients reach the daemon through a Unix
domain socket in a runtime directory readable only by the operator's user account, with
no network listener. One daemon serves one project. See
[`../protocol/CLIENT.md`](../protocol/CLIENT.md).

**Proposed.** The client protocol is versioned independently of any implementation, so
a client written against one version keeps working after a port.

**Decided 2026-10-25.** The protocol specification is drafted in this repository and its
mature parts are later ported to the lasting `dromon` repository. See
[`../decisions/NAMING_AND_PORTS.md`](../decisions/NAMING_AND_PORTS.md).

## Session lines and engines

**Proposed.** Coordinate existing harnesses rather than replace them, so that each model
runs in the harness its vendor tuned for it. Dromon then owns the loop around each
engine, which is what makes wakes possible. Candidate interfaces, with how far each was
checked.

| Engine | Interface | Evidence on 2026-10-25 |
|---|---|---|
| Grok Build | Agent Client Protocol | Implemented 2026-10-25 for Grok 4.7 from Grok Build 1.0.70's documentation, not verified against a live turn |
| Cursor Agent | Agent Client Protocol through `agent acp` | Reported in July 2026 research by the operator's sessions. Not rechecked |
| Claude Code | Claude Agent SDK | Implemented 2026-10-25 with Agent SDK 0.2.174 and Claude Code 2.1.310, on the subscription |
| Codex | Its app-server | Implemented 2026-10-25 with Codex 0.164.0 and GPT-6-Luna, on the ChatGPT plan |
| Dromon's own loop | Direct model calls | Needed where full control of each step is required |

**Decided 2026-10-25.** The first adapter targets the model Claude Opus 5.5. The operator
prefers Dromon's own loop calling the model directly, on the condition that it can run on
a Claude subscription as well as on separately billed application programming interface
usage.

**Fact, read on 2026-10-25** in Anthropic's Claude Code "Legal and compliance" page and
the Agent SDK overview. Subscription sign-in is "intended exclusively" for subscribers'
"ordinary use of Claude Code and other native Anthropic applications". Developers,
including those using the Agent SDK, "should use API key authentication", and "may not
collect, store, or intermediate Claude.ai credentials or session tokens". Signing in to
the unmodified Claude Code binary with one's own subscription is permitted.

**Inference.** Dromon's own loop calling the Messages API can run only on an API key with
usage billing, so the operator's condition is not met for it. A subscription can drive a
line only through the unmodified Claude Code binary, through its headless mode or the
Agent SDK, with Dromon never reading or storing the sign-in token. Whether the Agent SDK
itself may run on a personal subscription is not stated plainly. The same page says Pro
and Max limits "assume ordinary, individual usage of Claude Code and the Agent SDK",
while the Agent SDK overview points developers to API keys.

**Decided 2026-10-25 by the operator.** The operator's only credential is the
subscription, so lines run on Claude Code through the Agent SDK, driven from outside by
Dromon. The per-model drivers are written in Keleusma and run through `pykeleusma`.
**Later the same day,** the operator directed that OpenAI models run on Dromon. Codex on
the operator's machine is signed in to a ChatGPT plan, which Codex lines use, as the
Codex section below records.

**Implemented 2026-10-25, roadmap task 4.** `dromon.engine` runs each turn through
`ClaudeSDKClient` with no user or project settings and no tools unless configured. It
refuses an environment that selects an API key, an API profile or a cloud provider, and
refuses a turn whose engine reports any `apiKeySource` other than `"none"`. Each engine
message becomes an event for the line's driver, whose actions the adapter performs.
`dromon probe` runs one short turn and reports the engine's capabilities. The driver
interface is [`../protocol/DRIVER.md`](../protocol/DRIVER.md).

**Fact, observed live on 2026-10-25.** With a claude.ai login, a Claude Code session
offers the account's connectors, such as mail sending and file sharing, even with
`setting_sources=[]` and no built-in tools. Lines therefore run with
`strict_mcp_config=True`, a permission gate that allows only the line's own tools, and a
check that the engine reports no tool beyond them.

**Decided 2026-10-25 by the operator.** Arbitrary MCP servers can be exposed to a line,
under an allow-all or a whitelist model, with the settings held per line. This is
[`../protocol/LINE_CONFIG.md`](../protocol/LINE_CONFIG.md), stored in the event log as
`line_configured`. The default remains no tools beyond Dromon's own.

**Decided 2026-10-25 by the operator.** Drivers are concrete. Subscription use, API use
and flexible use are separate drivers that bake their strategy in, rather than one
driver choosing behaviour from parameters. Claude Haiku 5.5, the least expensive
subscription model, is used for testing.

Each adapter should probe the engine's capabilities at startup, because support varies by
engine version.

### Codex and Grok Build against the Claude Code conclusions

**Reviewed 2026-10-25 at the operator's request**, which asked whether the conclusions
above for Claude Code hold for Codex and Grok Build. Vendor documents were read on that
day. xAI's consumer terms refused the request, so nothing here rests on them.

| Conclusion for Claude Code | Codex | Grok Build |
|---|---|---|
| Dromon's own loop cannot use the subscription | Does not hold, see below | Unknown |
| Dromon never reads, stores or forwards the sign-in token | Holds when Dromon drives the Codex binary | Holds, as the conservative default |
| Unattended subscription use is unclear | Similar, API keys are the recommended default | Documents invite automation |
| Per-token billing must never start silently | Holds, with OpenAI's variables | Holds, with xAI's variables |
| Account tools must be kept out of a line | Holds, without an equivalent isolation switch | Holds, without an equivalent isolation switch |

- **Fact.** OpenAI's Sign in with ChatGPT documents ChatGPT plan usage "for open-source
  and locally hosted apps", which may request "permission to use the user's ChatGPT
  plan for eligible Responses API requests". The host app then holds and renews the
  OAuth tokens itself. Source <https://developers.openai.com/siwc/token-sharing-open-source>
  and <https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server>.
- **Inference.** Dromon is open source and runs locally, so its own loop could run
  OpenAI models on a ChatGPT plan, which Anthropic's terms rule out for Claude. Doing so
  would make Dromon hold tokens, the reverse of the Claude rule, and needs the
  operator's decision. The program and the app-server it pairs with are described as
  preview and "experimental".
- **Fact.** OpenAI's Codex authentication page says "API keys are still the recommended
  default for automation", and its app-server page says to use the Codex SDK for jobs
  and CI. Codex keeps its login in `~/.codex/auth.json` or the system credential store,
  and says to "treat ~/.codex/auth.json like a password". Source
  <https://learn.chatgpt.com/docs/auth> and <https://learn.chatgpt.com/docs/app-server>.
- **Fact.** xAI's Grok Build overview calls headless use "ideal for scripts,
  automations, or integration into other apps" and names the Agent Client Protocol for
  use "in other apps". xAI's own posts endorse subscription sign-in inside OpenCode and
  Kilo Code. No xAI program for other apps to use the subscription was found. Source
  <https://docs.x.ai/build/overview> and <https://x.ai/news/grok-opencode>.
- **Fact.** On the operator's machine, the user configurations of both Codex and Grok
  Build declare mail, document and calendar MCP servers.
- **Inference.** A line on either engine started on the operator's configuration would
  inherit those servers. A separate configuration directory would also leave the login
  behind, and copying the login would breach the token rule, so isolating a line is an
  open design problem for both. Codex's `-c` overrides may disable servers by name.
- **Owed before a Codex or Grok line runs.** The environment check and the reserved
  variables of `SETTINGS.md` name only Anthropic's credentials, and must add at least
  `OPENAI_API_KEY` and `XAI_API_KEY`, and each adapter needs a way to read the
  credential the engine actually used, as `apiKeySource` does for Claude Code. A
  third-party report, not verified, says a Codex session on a ChatGPT plan may fall back
  to a smaller model at a limit, which the driver's model check exists to catch.
- **Concrete drivers carry over.** Each model and credential strategy keeps its own
  driver. Dromon's own loop on OpenAI's program would add a credential the current
  strategies do not name.

**Implemented 2026-10-25, on the operator's direction.** Drivers for GPT-6-Luna on
Codex are packaged in all three strategies, under driver interface 3, which adds the
engine each driver is written for.

**Implemented 2026-10-25, on the operator's direction to run OpenAI models.**
`dromon.codex` runs a line through `codex app-server`, as
[`../protocol/CODEX.md`](../protocol/CODEX.md) specifies. The items owed above are met
as follows. The account Codex reports is the credential handed to the driver. A
subscription line's Codex starts without API variables, and the reserved variables name
OpenAI, Codex and xAI prefixes. Every MCP server Codex lists is disabled by name, and
connector features are disabled, then verified. Dromon's tools reach the model as
dynamic tools. The app-server was chosen over `codex exec`, which OpenAI recommends for
automation, because only the app-server lets Dromon serve its own tools in process and
see each item as it starts. **Porting hazard.** The app-server and dynamic tools are
experimental. **Later the same day,** Codex lines gained write access under a Dromon
permission profile that denies Dromon's state and runtime directories and keeps `.git`
read-only, as `CODEX.md` records.

**Implemented 2026-10-25, on the operator's direction for nominal Grok support while
Grok usage was exhausted.** `dromon.grok` runs a line through `grok agent stdio` over the
Agent Client Protocol, as [`../protocol/GROK.md`](../protocol/GROK.md) specifies. Grok's
custom sandbox profiles live only in its own files, so the operator defines Dromon's two
profiles and Dromon checks them before each turn. ACP has no client-hosted tools, so
Dromon's tools reach a Grok line through `dromon mcp --line` and a per-line token on a
line socket. Every behaviour that could not be confirmed refuses the turn.

## Communication

**Decided.** Communication has three layers. In the operator's words, they are
inter-harness, across harnesses on the same machine, and across harnesses on different
machines. The first layer is between session lines inside one Dromon daemon.

| Layer | Proposed transport | Trust | Wakes |
|---|---|---|---|
| 1. Inside one daemon | In-process queues | Full | Guaranteed, because the daemon owns every loop |
| 2. Across harnesses on one machine | Unix domain socket, MCP or the Agent Client Protocol | The local user, checked by the socket's peer credentials | Only for harnesses Dromon drives. A harness it does not drive still cannot be woken |
| 3. Across machines | A link between daemons | Needs real authentication, such as mutual TLS, and replay protection | Delivered to the remote daemon, which wakes its own line |

**Proposed.** One message envelope serves all three layers. Each message carries an
identifier that makes a resend harmless, so a message can cross from layer 1 to layer 3
without being rewritten. Each layer adds only its routing and its delivery guarantee.

## Wakes

**Decided.** The harness issues wakes.

**Decided 2026-10-25 by the operator.** Delivery is tick based, and messages are not
interrupts. A message sent in one turn is received no sooner than the next. Mid-turn
injection is therefore not used.

**Decided 2026-10-25 by the operator, refining the above.** The scheduler is a game
loop. Every tick iterates over all lines and updates each once. Messages sent during a
tick are collected in a sent queue, and at the end of the tick they move to the post
office, which makes them valid for delivery from the next tick on. A busy line is
skipped, and that counts as its update for the tick.

**Implemented 2026-10-25.** Each tick visits the lines in a fixed order. An idle,
line under its cap with post office mail starts a turn, which runs as a task
without the tick waiting for it, so a turn may span many ticks and a slow line never
holds the others back. Ticks follow every `--tick-seconds`, one second by default,
while anything is in flight, and the loop rests when nothing is. After a restart every
undelivered message starts in the post office. Lines send through an in-process
`send_message` tool whose sender the daemon fixes, and operators through `dromon send`.
A per-line turn cap stops a runaway exchange until the conversation limits of task 7.

**Inference.** The tick is the unit a batched update loop of Keleusma stream processors
schedules, so the Keleusma version can keep these semantics exactly.

**Proposed.** A wake policy in per-project configuration, for example every tick or
every few ticks.

## Operator questions

**Decided.** The harness tracks questions for the operator as a first-class feature, so
that questions are no longer lost in responses and tracking files.

**Decided 2026-10-25 by the operator.**

- `ask_operator` sends a message to an operator's mailbox, and the operator replies as
  another session line would. The reply reaches the asking line as an ordinary message
  and wakes it.
- The operator is not a singleton. A project may have several people and many session
  lines, and every operator is addressed separately.
- Questions can be forwarded to channels such as email or Slack through a general
  interface that adapters implement for each use case.

**Implemented 2026-10-25.** Operators are parties with addresses such as `@ana`. A
question is a message of kind question from a line to one operator, and the answer is
that operator's reply, which must reverse the question's route. The lifecycle states
open, answered, withdrawn and superseded are tracked on the message. Forwarding goes through relays, with a command relay
as the general adapter. See [`../protocol/EVENT_LOG.md`](../protocol/EVENT_LOG.md) and
[`../protocol/RELAY.md`](../protocol/RELAY.md).

**Decided 2026-10-25 by the operator.** Nobody is compelled to answer mail or questions.
Deferring is always an option, and doing nothing is valid. A human or an agent with a
hundred messages needs tools to look through them. The wake message reads along the
lines of "X new message(s). Y unread. Z unanswered."

**Implemented 2026-10-25.** Every party has a mailbox of the messages delivered to it.
A message is unread until its recipient opens it, recorded as `message_read`, and
unanswered until the recipient replies to it. A line's wake prompt opens with that
summary sentence and lists the new mail's headers, without bodies, and the line's
`list_mail` and `read_mail` tools page through its mailbox and open messages. Operators
use `dromon mail` and `dromon read`. Nothing marks mail as needing an answer, and the
prompt does not ask for one.

**Decided 2026-10-25 by the operator.** A message can be acknowledged, which marks it
answered without sending a reply. Senders see which messages were acknowledged as
opposed to replied to, because they may be expecting a reply.

**Implemented 2026-10-25.** `message_acknowledged` records an acknowledgement. Lines use
`acknowledge_mail` and see their sent mail's fate with `list_sent`, and operators use
`dromon ack` and `dromon sent`. Each sent message reads as waiting, delivered or read,
and as replied, acknowledged or unanswered. Acknowledging a question does not close it,
so a sender can tell that its question was seen but not yet answered.

**Decided 2026-10-25 by the operator.** There is no such thing as a blocking question.
After asking, a line may end its turn or keep working. That is up to the line-agent.

**Implemented 2026-10-25, roadmap task 6.** `ask_operator` is an in-process tool beside
`send_message`. It records a question to one operator, by default the operator who last
messaged the line, and returns at once, so no tool call waits on a person. The daemon
keeps the queue in the event log. `dromon questions` lists an operator's open questions
and `dromon answer` replies. The answer is a reply message that wakes the asking line in
a later tick and closes the question. Each line's engine
session is recorded as `line_session`, so after a restart the answer reaches a line that
still remembers its question. Live, a Haiku line asked a question, the daemon
restarted, the question was listed and answered, and the line woke and recalled a
codeword it had been given before the restart.

**Proposed.**
- Which operator a line asks by default is per-project configuration.
- Clients show each operator their own queue. `dromon questions` lists it and
  `dromon answer` replies from the command line.

**Decided 2026-10-25 by the operator.** The daemon maintains the `ask_operator` queue.
Interacting with it is a `dromon` client concern in the immediate term. A command-line
client alone is not enough. The client wants a graphical interface, or a way to launch
one, so that the operator can start the daemon and then attach a dozen interfaces to
different session lines in different terminals at once.

**Decided 2026-10-25 by the operator, as direction.** The front end and the back end are
split. The graphical client is the front end and the daemon is the back end, unlike
harnesses such as Claude Code and Codex, which unify the two in one program. The
operator's comparison is a multiplayer online game, whose clients attach to a shared
server, against a single-player game that is client and world at once.

**Decided 2026-10-25 by the operator.** The terminal client is probably curses, chosen
after looking at what Claude Code, Codex and Grok Build use.

**Fact, from secondary sources read on 2026-10-25 and not checked against the official
repositories.** Claude Code renders its terminal interface with a heavily customized
Ink, the React renderer for terminals, on Node.js. Codex's interactive mode is the
`codex-tui` Rust crate, built on Ratatui and Crossterm. Grok Build is Rust, with its
interface in an `xai-grok-pager` crate whose underlying library the sources do not
name. All three are full-screen terminal programs rather than separate graphical
applications.

**Implemented 2026-10-25, roadmap task 8.** `dromon attach LINE` opens a curses client,
the standard library's counterpart to Ratatui and Crossterm and so a direct map onto
the Rust port, and `dromon attach LINE --gui` opens a Tk window, which also ships with
Python. Both are thin shells over `dromon.attach`, which polls the new `line_view`
operation and turns typed input into operator commands. Every client is an independent
connection, so any number may attach to any lines at once.

**Inference.** The client protocol of [`../protocol/CLIENT.md`](../protocol/CLIENT.md) is
then the game's network protocol. It needs pushed updates for attached clients, and
clients on other machines once communication layer 3 exists, so both belong in its
later versions.
- **Decided 2026-10-25, design deferred.** Every relay adapter supports a response path,
  so an operator can reply through the channel that carried the question. The required
  properties are in [`../protocol/RELAY.md`](../protocol/RELAY.md), chief among them
  that a reply is attributed to an operator only after the channel authenticates them.
  Replies arrive through inbound adapters registered with the daemon and configured per
  channel, so replying by Slack and by email needs two registered adapters.

## Standard harness features

**Decided 2026-10-25.** Standard features of an agent harness, such as compaction of long
conversations, are added as the work reaches them rather than designed up front. Each
lands with the roadmap task that first needs it, together with its events in the
protocol specification.

## Conversation control

**Proposed.** Two agents can exchange messages without end. The daemon enforces
per-conversation turn limits, cost budgets and cooldowns, and turns a long exchange into
an operator question rather than letting it run.

**Decided 2026-10-25 by the operator.** Pick sensible limits, to be tuned later.

**Implemented 2026-10-25, roadmap task 7.** `dromon.limits` counts, for each line, its
turns in a row without mail from an operator, by default at most 12, and the estimated
cost of its turns since its last release, by default at most $2. An optional cooldown
sets the least time between a line's turns. A line past either limit is held with
`line_held`: it takes no turns and its mail waits, and the daemon asks, on the line's
behalf, the operator who last messaged it, or else the daemon's default operator,
whether to resume it. Any reply to that question, or `dromon release`, releases the line
with `line_released` and resets its counts. A runaway ping-pong between two lines is
held within the limit, and both lines raise questions.

**Inference.** Counting turns without operator mail, rather than turns per pair of
lines, catches a runaway among any number of lines, and a single operator message
resets it, which matches the intent of an operator supervising the work.

## Stream processors and scheduling

**Decided.** In the Keleusma version, each model has its own per-turn or per-sub-turn
stream processor. Model-specific behaviour, such as available functionality and context
window size, is hard-coded into that model's processor, so the concrete behaviour is
written for each model. Each processor runs on its own thread, or several session lines
are batched on one thread with an update loop, as entities are updated in a game.

**Inference.** Keleusma proves an upper bound on the time and memory of each stream step.
A batched update loop can therefore schedule many session lines on one thread with a
known worst-case tick, which most harnesses cannot guarantee.

**Proposed.**

- The model call is network input and output with no time bound. The host performs it
  and feeds each streamed event, such as a batch of tokens, a tool call or the end of a
  turn, into the processor as one step. Keleusma's yield and resume model fits that
  shape.
- Each processor declares the model version it targets, so a mismatch is caught when a
  line starts rather than in the middle of a session.
- In this prototype, each model gets a class with one shared interface, for example
  `step(event) -> actions`, so the later Keleusma port is close to a one-for-one
  translation.

## Configuration

**Decided.** Per-project configuration is non-trivial.

**Decided 2026-10-25 by the operator, as direction.** Dromon's configuration for a
multi-agent project may grow as large and as detailed as the project needs, with
Kubernetes as a loose point of comparison. Everything about a session line is
configurable per line, and per project when one harness serves several projects.
Building that machinery is expected to take sustained work. The per-line tool
configuration of [`../protocol/LINE_CONFIG.md`](../protocol/LINE_CONFIG.md) is its first
piece.

**Decided 2026-10-25, task 9.** It lives in `.dromon/dromon.toml`, specified by
[`../protocol/PROJECT_CONFIG.md`](../protocol/PROJECT_CONFIG.md) as file version 1. It
declares the default operator, the tick cadence, the conversation limits, the wake
policy, reusable line profiles, and each line's engine, strategy, tools, limits and wake
policy. Unknown keys are errors, and every error is located in the file. Flags override
the file, and the file overrides the defaults.

**Fact.** The format follows a review of Claude Code, Codex and Grok Build settings on
2026-10-25, recorded in the specification with its sources. **Open.** User and managed
layers, credentials policy and references to secrets held outside the file.

**Porting hazard.** The prototype reads TOML with Python's `tomllib` and locates syntax
errors by parsing its message text. The Rust port should use a parser that reports
spans, such as the `toml` crate, and keep the same messages.

## Persistence

**Implemented 2026-10-25, roadmap task 2.** An append-only event log in SQLite holds
messages, wakes, questions and line state. It makes recovery after a crash and replay in
a newly attached client straightforward, and the event list doubles as the specification
of the data model. The specification is [`../protocol/EVENT_LOG.md`](../protocol/EVENT_LOG.md).

## Security and trust

**Proposed.**

- The client socket is readable only by the operator's user account.
- The daemon runs outside every session line's sandbox, so it can act as a credential
  broker under policy, for example pushing to git on a line's request. That also makes it
  a high-value target. Every brokered action needs an explicit per-project allowance.
- Each session line keeps its own sandbox profile. **Decided 2026-10-25, by the
  operator's direction to reach parity with Claude Code.** A line's shell commands run
  in Claude Code's own sandbox, configured per project and per line in Claude Code's
  vocabulary, with unattended defaults and with Dromon's state and runtime directories
  protected, as [`../protocol/SANDBOX.md`](../protocol/SANDBOX.md) specifies. **Porting
  hazard.** The Rust port inherits the dependency on Claude Code's sandbox runtime for
  the Claude Code engine, and another engine needs a sandbox of its own.
- Each session line carries engine settings. **Decided 2026-10-25, by the operator's
  direction to reach parity with the settings actually used in Claude Code, Codex and
  Grok Build.** Permission rules, command hooks, environment variables and attribution
  use Claude Code's vocabulary, layer per project, profile and line, and reach Claude
  Code through its settings flag, as [`../protocol/SETTINGS.md`](../protocol/SETTINGS.md)
  specifies. An `ask` rule denies, since nothing pauses a line, and every line's file
  tools are denied Dromon's state and runtime directories. MCP servers may refer to
  secrets in the daemon's environment. **Porting hazard.** Claude Code parses the rules
  and runs the hooks, so another engine needs its own rule matcher and hook runner to
  honour the same documents.
- Messages from other session lines, harnesses and machines are untrusted data. Only the
  operator, through a client, holds operator authority.
- External agents reach the daemon over MCP. **Decided 2026-10-25, by the operator's
  direction.** Another harness, such as a Claude Code session, starts `dromon mcp` as an
  MCP server and becomes the party `&name` with its own mailbox. The project file sets
  whether an agent socket exists, the access of an anonymous connection and a ceiling,
  from off to every connection trusted as an operator, and registered agents carry
  tokens stored as digests, as [`../protocol/AGENTS.md`](../protocol/AGENTS.md)
  specifies. An agent's messages carry no operator authority unless its level is
  `operator`.

## Keleusma

**Decided.** The prototype uses the `pykeleusma` wrapper. The final implementation is in
Keleusma, entirely or almost entirely.

**Proposed.** Keleusma's bounded execution suits the harness's policy layer, such as
message routing, wake policy and permission checks, where a hook must never hang. Use it
there once `pykeleusma` can run scripts, which it could not on 2026-10-24. The needs,
mapped to the binding's tasks, are in [`KELEUSMA_INTEGRATION.md`](KELEUSMA_INTEGRATION.md).

## Teacher models

**Open.** Before the operator clarified that Dromon is its own project, they mentioned
supporting teacher models that help train new language models. Its place in Dromon's
scope is not settled.

**Decided 2026-10-25, by the operator's direction to dogfood Dromon in the epagoge
project.** Dromon can spin up a project's local teacher, a model the project's own
generators call through Ollama on the loopback interface, as
[`../protocol/TEACHER.md`](../protocol/TEACHER.md) specifies. A teacher is a served model,
not a session line. Lines reach it through the project's tools, and Dromon only loads
and releases it. Lines also gained `claude_code_prompt` and `instructions`, because a
Claude Code line had an empty system prompt and read no `CLAUDE.md`, so it could not
follow a project's own rules.

**Fact, observed on 2026-10-25 in epagoge.** With the teacher loaded by `dromon
teacher up` and `dromond` running there, a Claude Haiku 5.5 line in Claude Code's
sandbox queried the teacher over loopback and answered a question from the project's
`CLAUDE.md`, and a GPT-6-Luna line answered one from its instructions. Both replied to
the operator by `send_message`. Dromon's state there is excluded from git through the
repository's local exclude file, so the project's own checks never see it.

**Fact, observed later on 2026-10-25 in epagoge, after its line reviewed the setup.** The
project's full gate passed from a sandboxed Claude Haiku 5.5 line, with 678 tests and a
clean disclosure scan. With the sandbox denying writes to `.git`, the same line could not
write there, so no spelling of a commit succeeds from its shell. `dromon teacher up` now
refuses to load a model that would leave less than its headroom of available memory. **Unverified, from the seeding session's memory.** Several model
vendors' terms forbid using their outputs to train competing models, and subscription
plans tend to be stricter than paid interface use. Check each vendor's current terms
before any trace capture, and record the source model and licence of every trace.

## Open questions

1. The home of the protocol specification. Decided 2026-10-25, drafted here and ported
   later.
2. The engine for the first adapter. Decided 2026-10-25, Claude Opus 5.5 on Claude Code
   through the Agent SDK, on the subscription, with a Keleusma driver.
3. The contents of `.dromon/dromon.toml`.
4. Mid-turn injection. Decided 2026-10-25, not used, because delivery is tick based.
5. Teacher-model scope and licensing. A local teacher served by Ollama is decided.
   Training on traces from hosted models remains open.
6. Whether Dromon retires the interline store, or keeps it for sessions Dromon does not
   drive. Moved here from the handoff on 2026-10-25. Since the same day, a session
   Dromon does not drive can reach Dromon's lines as an external agent, which removes
   one reason to keep a separate store.
