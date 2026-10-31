# Operator Relays

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, notice version 1, written on 2026-10-25. Normative. The Python module
`dromon.relay` implements it. The daemon runs it from 2026-10-31, routed by the project
file's `[relays]` and `[operators]` tables, which
[`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies.

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
The kind `issue` was added within notice version 1, since it adds a value and changes
no field. **Hypothesis.** A relay script written before it passes the kind through as
text and needs no change. A script that refuses an unknown kind fails each issue's
notice, which then stays pending.

| Field | Value |
|---|---|
| `v` | The notice version, the integer `1` |
| `message_id` | The message identifier |
| `sender` | The sender's address as text |
| `operator_id` | The recipient operator's identifier |
| `kind` | `note`, `question`, or from 2026-10-31 `issue`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies |
| `body` | The message text |
| `choices` | The question's choices, possibly empty |
| `reply_to` | The message this one replies to, or `null` |

## Command relay

The general adapter runs a command once per notice. The command is an argument vector
from operator configuration and runs without a shell. The notice arrives on standard
input followed by a newline. Exit status 0 means the channel accepted the notice. Any
other status, a failure to start, or exceeding the configured timeout of at most 3,600
seconds is a failure. Its process receives only the common operating-system environment
allowlist `PATH`, `HOME`, `LANG`, `TERM` and `TMPDIR`, when present. Other daemon
variables, including credentials, are omitted. A short script behind this adapter can
send email, post to a chat service or write to a file, but must obtain any credential
through its own explicit mechanism.

The daemon runs the command without blocking its event loop. The command is the
leader of its own process group, and on a timeout, or when the daemon stops while it
runs, the whole group is killed, so a script cannot leave a child holding the notice.
Its standard output is discarded, and only the first 4,096 bytes of its standard error
are kept, of which 200 reach the reason a failure reports.

## File relay

**Added on 2026-10-31.** The built-in file relay appends each notice to one file as
one line, the notice's JSON followed by a newline, so an operator's session can follow
the file with `tail -f`, or with a tool that watches a file. No shell or script is
involved.

- The file is created with mode `0600`. It is refused, and the notice stays pending,
  when it is a symbolic link, is not a regular file, belongs to another user or is
  open to group or others.
- Each notice is written with one append and flushed to disk before delivery is
  recorded.
- The notice file may not lie in any line's working directory, so that no line can
  read what is relayed or write a notice of its own into the file. A directory Dromon
  protects, such as `.dromon/run`, is the one exception, since every enabled sandbox
  denies it to lines. The daemon checks this at start and at reload, and the relay
  checks it again before every write, against each line's working directory as
  configured then. A line configured into the directory later therefore stops the
  writes, and its notices stay pending, rather than being refused itself.
- **Fact.** The file grows without bound. Truncating it is the operator's, and
  `tail -F` follows a truncated file.
- **Assumption.** The file is on a local disk. The append runs on the event loop, so a
  file on a stalled network mount would stall the daemon.

## The daemon's relayer

**Added on 2026-10-31.** One relayer task performs the forwarding passes.

- A pass runs when mail to an operator is recorded, when a reload changes the routes,
  when a failed notice's backoff ends, and every 60 seconds in any case, so mail
  recorded by a path that does not wake the relayer, and mail waiting for a route a
  reload has just given, is still forwarded.
- Notices are forwarded one at a time, in the order sent. A slow relay delays the
  notices after it, by at most its timeout each.
- A notice whose relay fails is tried again after a backoff of 5 seconds, doubling at
  each failure up to 300 seconds. Backoff is per notice and held in memory, so a
  restart tries every pending notice at once. Every reload clears the backoff, whether
  or not it changes a route.
- Before a notice is handed to its relay, and again before its delivery is recorded,
  the relayer checks that it is still undelivered. Delivery is recorded once, by
  whichever came first.
- A relay that raises anything but a relay failure is a defect. Its notice is failed
  with the error's description, the trace goes to the daemon's standard error, and
  the relayer goes on.
- `dromon status` reports the routed operators, how many notices were relayed since
  the start, how many wait unrouted, and each failing notice with its attempts, its
  reason and when it is tried again, at most 10.

## Delivery and the mailbox

A relay's acceptance appends `message_delivered`, exactly as a client's listing does.

- **Delivery is not reading.** A relayed message stays unread, so `dromon mail --unread`
  still lists it, and `unread` still counts it, until the operator opens it with
  `dromon read` or acknowledges it with `dromon ack`. Its `unanswered` count is
  likewise unchanged.
- **A client listing first wins.** `dromon mail`, `dromon questions` and the `replies`
  query deliver the operator's undelivered mail when they list it, as
  [`CLIENT.md`](CLIENT.md) specifies, since a client showing mail is its delivery. A
  message so delivered before the relayer reaches it is not relayed at all. A message
  a client delivers while its relay runs is relayed once and recorded delivered once.
- **A sender's view.** `list_sent` shows a relayed message as `delivered`, as it shows
  one a client listed.

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
- Retry timing that survives a restart, and a notice that fails without end, which is
  tried again at the bound of 300 seconds for as long as it stays undelivered.
