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
| Operator question | A question a session line raises for the human operator, tracked until it is answered or withdrawn |
| Stream processor | In the Keleusma version, the per-model program that handles one turn or sub-turn step |

## Process model

**Decided.** `dromond` holds the session lines. Clients connect to the daemon. The
operator can also use sessions held inside the daemon. The client command is `dromon`,
whose subcommands cover starting and stopping the daemon, status, attaching an
interactive client, sending messages and handling operator questions.

**Proposed.** Clients reach the daemon through a Unix domain socket readable only by the
operator's user account, with no network listener by default. The client protocol is
versioned independently of any implementation, so a client written against one version
keeps working after a port.

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
| Grok Build | Agent Client Protocol | Its local user guide describes it as native to that protocol |
| Cursor Agent | Agent Client Protocol through `agent acp` | Reported in July 2026 research by the operator's sessions. Not rechecked |
| Claude Code | Claude Agent SDK | Vendor SDK. Not tested from Python here |
| Codex | Codex SDK or its app-server | Vendor interfaces. Not tested here |
| Dromon's own loop | Direct model calls | Needed where full control of each step is required |

**Decided 2026-10-25.** The first adapter targets the model Claude Opus 5.5. **Open.**
Which loop drives it, either Claude Code through the Claude Agent SDK or Dromon's own
loop calling the model directly. **Proposed.** The Claude Agent SDK, which follows the
principle above of running each model in its vendor's harness and may work with a
subscription login, subject to the hypothesis below. Each adapter should probe the engine's
capabilities at startup, because support varies by engine version.

**Hypothesis.** Subscription logins usually work only through the vendor's own harness,
while Dromon's own loop would need paid application programming interface keys. This
affects cost and should be checked per vendor.

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

**Proposed.** If the recipient is idle, the daemon starts a turn with the message. If the
recipient is busy, the message waits until the turn ends, or is injected mid-turn where
the engine supports it. The wake policy is per-project configuration, for example
immediate, batched, or only at turn boundaries.

## Operator questions

**Decided.** The harness tracks questions for the operator as a first-class feature, so
that questions are no longer lost in responses and tracking files.

**Proposed.**

- Each session line has an `ask_operator` tool, served by the daemon over MCP so that any
  engine can call it.
- A question records the asking line, the text, optional choices, links to context, and
  whether it blocks that line. A blocking question pauses only its own line.
- A question moves through the states open, answered, withdrawn and superseded. Questions
  are stored durably and survive a restart.
- An answer is delivered to the asking line as a wake.
- Clients show one queue for all session lines. `dromon questions` lists it and
  `dromon answer` answers from the command line.

## Conversation control

**Proposed.** Two agents can exchange messages without end. The daemon enforces
per-conversation turn limits, cost budgets and cooldowns, and turns a long exchange into
an operator question rather than letting it run.

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

**Proposed.** It lives in `.dromon/dromon.toml` in the project, and its format is versioned
with the protocol so it survives each port. **Open.** Its contents, which will at least
cover the session lines, their engines, wake policy, conversation limits and credentials
policy.

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
- Each session line keeps its own sandbox profile.
- Messages from other session lines, harnesses and machines are untrusted data. Only the
  operator, through a client, holds operator authority.

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
scope is not settled. **Unverified, from the seeding session's memory.** Several model
vendors' terms forbid using their outputs to train competing models, and subscription
plans tend to be stricter than paid interface use. Check each vendor's current terms
before any trace capture, and record the source model and licence of every trace.

## Open questions

1. The home of the protocol specification. Decided 2026-10-25, drafted here and ported
   later.
2. The engine for the first adapter. The model Claude Opus 5.5 was decided on
   2026-10-25. The loop that drives it is open.
3. The contents of `.dromon/dromon.toml`.
4. Mid-turn injection, which depends on each engine.
5. Teacher-model scope and licensing.
