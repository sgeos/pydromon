# Operator Relays

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, notice version 1, written on 2026-10-25. Normative. The Python module
`dromon.relay` implements it.

## Purpose

Operators are reached through whatever channel suits them, such as email, a chat
service such as Slack, a pager or a client's question queue. A relay is the adapter for
one channel. Dromon defines one general interface and leaves each channel to an adapter,
so a new channel needs no change to the daemon or the protocol.

## Forwarding

The daemon holds a route from each operator identifier to one relay. A forwarding pass
takes every message whose recipient is an operator and that is not delivered, in the
order sent, and hands each to its operator's relay as a notice.

- If the relay accepts the notice, the daemon appends `message_delivered` for it.
- If the relay reports a failure, the message stays undelivered and the pass continues
  with the next message.
- If the operator has no route, the message stays undelivered and is reported as
  unrouted.

Delivery is at least once. A stop between a relay's acceptance and the append of
`message_delivered` forwards the message again on the next pass. Each notice carries its
message identifier, so a channel or its reader can recognize a repeat.

## Notice

A notice is one JSON object, encoded canonically as in [`EVENT_LOG.md`](EVENT_LOG.md).

| Field | Value |
|---|---|
| `v` | The notice version, the integer `1` |
| `message_id` | The message identifier |
| `sender` | The sender's address as text |
| `operator_id` | The recipient operator's identifier |
| `kind` | `note` or `question` |
| `body` | The message text |
| `choices` | The question's choices, possibly empty |
| `blocking` | Whether the asking line waits for the answer |
| `reply_to` | The message this one replies to, or `null` |

## Command relay

The general adapter runs a command once per notice. The command is an argument vector
from operator configuration and runs without a shell. The notice arrives on standard
input followed by a newline. Exit status 0 means the channel accepted the notice. Any
other status, a failure to start, or exceeding the configured timeout of at most 3,600
seconds is a failure. A short script behind this adapter can send email, post to a chat
service or write to a file.

## Trust

- Notice bodies are untrusted text from session lines. A relay passes them to its
  channel as data and never as commands or markup beyond what the channel displays.
- Routes and relay commands are operator configuration and are trusted like the
  daemon's own settings.
- A reply that arrives through a channel becomes a `message_sent` from an operator only
  after the response path authenticates its sender, as required below. Until the
  response path exists, replies come from clients.

## Response path

**Required, decided by the operator on 2026-10-25, design deferred.** Every relay
adapter supports a response path, so that an operator can reply through the channel that
carried the notice, such as answering an email or a chat message, and the reply arrives
as that operator's `message_sent` with `reply_to` set to the notice's message. Until the
response path is designed, replies come from clients.

Whatever design is chosen must provide these properties.

1. **Correlation.** A response names the notice it answers. The notice's `message_id`
   is the correlation key, and it must survive the channel, for example in a reply
   header, a thread or a token in the text.
2. **Authentication.** The adapter establishes which operator sent the response by a
   means the channel actually guarantees, such as a verified sender or a signed
   request. A response that cannot be attributed is refused, never guessed. The
   claimed identity is untrusted until verified, as with every inbound message.
3. **Route check.** The attributed operator must be the notice's recipient. The event
   log's reply rule enforces this, so a correctly attributed response from another
   person is refused.
4. **No double answers.** A channel that redelivers a response must not produce a
   second reply. The event log refuses a second answer to a question, and an adapter
   records the channel's own response identifier to recognize a redelivered note.
5. **Untrusted body.** The response text is untrusted data and is stored as text only.

**Decided by the operator on 2026-10-25.** The response path is a separate kind of
adapter. The daemon holds an inbound adapter registry, and inbound adapters plug into
it and are configured individually, as relays are for the outbound path. A project in
which some operators reply by Slack and others by email registers two inbound adapters,
each configured for its channel. The daemon turns what an adapter authenticates into an
operator's reply, subject to the properties above.

**Open.** The inbound adapter interface, how adapters deliver to the daemon, for
example a command the daemon polls or a local endpoint that a channel's webhook calls,
and the configuration format for both registries.

## Not yet specified

- The design of the response path, whose required properties are stated above.
- Several relays for one operator, such as a chat service with email as a fallback.
- Retry timing and backoff between forwarding passes.
