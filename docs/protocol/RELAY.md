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
- Relays are outbound. A reply that arrives through a channel becomes a `message_sent`
  from an operator only after an inbound adapter authenticates its sender. Inbound
  adapters are not yet specified, and until they are, replies come from clients.

## Not yet specified

- Inbound adapters and how each authenticates an operator.
- Several relays for one operator, such as a chat service with email as a fallback.
- Retry timing and backoff between forwarding passes.
