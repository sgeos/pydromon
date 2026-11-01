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
leader of its own process group. On a timeout, or when the daemon stops while it runs,
that group is killed, and so is every descendant that a walk of the process table
finds below the command, including one that started a session of its own.
**Round 15, item 2.** The walk finds a process by its ancestry from the command alone,
so another line's or the operator's processes are out of reach. It does not find a
descendant whose parent had already exited, or one forked after the walk, and the
group is not frozen first because a stopped child blocks Python 3.15's event loop
watcher. A relay command carries no line's mark, which is why the mark is not used,
and a command that double forks or daemonizes may outlive its notice. An operator
should write a relay command that does neither. An unreadable process table, or a walk
that takes more than 3 seconds, leaves the group kill alone. Standard output is discarded. Standard error is
drained while the command runs, retaining at most 4,096 bytes, of which 200 reach a
failure reason. Acceptance is judged by the command's exit status. Once it exits, the
daemon reads what the pipe holds, in at most 16 reads, closes its read end of standard
error and does not wait for a descendant that inherited the pipe. **Round 15, item 1.**
The pipe is made by Dromon and passed to the command as its standard error, and Dromon
closes it with public interfaces only, `os.pipe`, a descriptor given as `stderr` and the
loop's `add_reader`. It no longer reaches asyncio's private subprocess transport.
**Re-review of 4da3e0e.** The read end is closed in a `finally` on the paths that end
the command, so a second cancellation that arrives while the group is being ended does
not leave the descriptor and the loop's reader behind. A loop that cannot watch the
pipe leaves neither end open. Before a descendant that a timeout or stop found is
killed, its parent and start time are read again and compared with the walk's, so a
process that took the identifier of one that ended is not killed, as
[`RUNS.md`](../architecture/RUNS.md) describes. The check runs before the group is
killed, deepest descendant first so that a parent does not orphan its child ahead of
the child's check. The checks, the group kill and the command's kill run in one worker
thread, shielded from cancellation, because each check runs `ps` for up to a second and
the event loop serves every line. A test holds a ticker on the loop while three slow
checks run. **Limits.** A cancellation that arrives while the thread works does not
stop it, which is intended, but the cancelled task returns before the kill is done, so
the command is reaped by the loop's watcher and not awaited. A command with many
descendants keeps one worker thread busy for up to a second each.

## File relay

**Added on 2026-10-31.** The built-in file relay appends each notice to one file as
one line, the notice's JSON followed by a newline, so an operator's session can follow
the file with `tail -f`, or with a tool that watches a file. No shell or script is
involved.

- The file must be under the daemon's protected state or runtime directory, which
  every enabled engine sandbox denies to lines. It is refused elsewhere. The file is
  created with mode `0600`. It is refused, and the notice stays pending, when it is a
  symbolic link, is not a regular file, belongs to another user or is open to group or
  others. It is opened nonblocking before its descriptor is checked, so a FIFO cannot
  hold the event loop.
- Each notice is written with one append and flushed to disk before delivery is
  recorded.
- The state or runtime directory prevents a line using its enabled sandbox from
  reading the relayed notices or writing its own. This guarantee depends on the
  sandbox enforcing its documented deny rules. An unsandboxed line can access the
  file because it runs as the daemon's user.
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
- Each configured relay instance with a route has one task, and its notices are
  forwarded one at a time in send order, including when several operators share it.
  Different relay tasks run concurrently, so a slow relay does not hold another
  relay's notices. The project file accepts at most 32 relay definitions, which bounds
  the number of tasks.
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
- A line may have at most five open issues. It may raise at most three issues in its
  last twenty sent messages, a window of messages, not of time. These limits are
  checked by event replay, so a restart cannot reset them. The open cap is the bound
  that holds, since only an operator's reply or acknowledgment frees a slot. When a limit is reached, `send_message` tells the line to wait
  for an operator reply or acknowledgment.
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
