# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, operators as addressable parties

**Done.** On the operator's instruction, operator questions became messages, and the
operator stopped being a singleton.

- Every operator is a party with an address such as `@ana`, registered by an
  `operator_registered` event. Any number may exist, each addressed separately.
- A question is a `message_sent` of kind question from a line to one operator. The
  answer is that operator's reply, and the route rule refuses an answer from any other
  operator or to any other line. The question lifecycle is tracked on the message.
- Relays forward operator messages to channels such as email or Slack through one
  general interface. The command relay runs a configured command with the notice as JSON
  on standard input, with no shell. Delivery is recorded only after a relay accepts, so
  delivery is at least once.
- The specification is revised in `docs/protocol/EVENT_LOG.md`, and
  `docs/protocol/RELAY.md` is new. The draft format version stays 1, since nothing yet
  depends on it, and the specification now says a draft may change without a new
  version until it is marked stable.

**Decisions made by this line, open to the operator's review.**

- Only a line asks a question, and only an operator receives one. Lines message each
  other with notes.
- Questions are superseded only by a later question from the same line, which may go to
  a different operator.
- Relays are outbound only. A reply from a channel is not attributed to an operator
  until an inbound adapter authenticates it, so replies come from clients for now.

**Verification.** `uv sync`, 188 tests passing with warnings treated as errors, ruff
check and format clean, and pyright strict with 0 errors and 0 warnings. The relay tests
run the command relay as a real subprocess, including a timeout, a failed start, a
nonzero exit and an attempt to inject shell syntax through the message body.

**Not verified.** Only Python 3.12.12 with SQLite 3.50.4 on macOS was tested. No real
email or chat channel was exercised.

**Next.** Task 3, the daemon skeleton.
