# Federation Between Daemons

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

**Approved by the operator on 2026-10-28, with all seven decisions at the end as
recommended.** Phase 1 is being built in steps, starting with the peer credential check
on the existing sockets. Steps 1 to 5 are built, and a part built is marked
**Implemented** with its date and step. **Amended on 2026-10-28** with the operator's decisions on the
review line's findings, which are collected as decisions 8 to 16 at the end and marked
**Decided** where they apply. Points marked **Proposed** below are approved design that
is not yet built. Points marked **Proposed, not yet decided** came from the review or
from the amendments, await the operator, and are listed after the decisions. The
operator decided six of them later on 2026-10-28, as decisions 17 to 22.

The operator asked for communication between Dromon daemons, first between two pydromon
daemons on one machine, such as the Keleusma project's daemon and this one, and then
between daemons on different machines. A line in one project should be able to message
and wake a line or an operator in another. This document is the design. It covers
layers 2 and 3 of the communication table in [`DESIGN.md`](DESIGN.md), for daemons
rather than for harnesses Dromon does not drive. Each point is marked **Fact**,
**Inference**, **Proposed** or **Decided**, and the choices for the operator are
collected at the end.

## Terms

| Term | Meaning |
|---|---|
| Peer | Another Dromon daemon that this daemon's operator has declared by name. One daemon serves one project, so a peer is in effect another project |
| Peer name | The identifier this daemon's operator gives a peer, such as `keleusma`. It is local. The other daemon may call this one anything |
| Remote party | A line, operator or agent of a peer, addressed through the peer's name |
| Envelope | The form a message takes on the wire between two daemons |
| Origin identifier | The message identifier a message has in the log of the daemon that sent it |
| Outbox | The messages to remote parties that are neither accepted by their peer nor returned. It is derived from the event log and never stored apart from it |
| Inbound and outbound | The two directions of a peer, each with its own policy. Inbound is mail the peer delivers here. Outbound is mail sent from here to the peer |

## Scope and phases

**Proposed.** Two phases, with one message model, one envelope and one set of events for
both. Only the transport and its authentication differ.

1. **Same machine.** A Unix domain socket per daemon for inbound federation, with a peer
   credential check and a per-peer token. The peer credential check is added to every
   socket the daemon already serves, which closes the debt that `HANDOFF.md` and
   [`CLIENT.md`](../protocol/CLIENT.md) record.
2. **Different machines.** Transport Layer Security version 1.3 with mutual
   authentication and pinned public keys, as below. The engines role helps with this
   phase once the design is approved.

## Addressing

**Proposed.** A remote address is a peer name, a colon, and an address as the peer
itself writes it.

| Address | Meaning |
|---|---|
| `kaizen` | The local line `kaizen`, unchanged |
| `@ana` | The local operator `ana`, unchanged |
| `&claude` | The local external agent `claude`, unchanged |
| `keleusma:kaizen` | The line `kaizen` of the peer this daemon calls `keleusma` |
| `keleusma:@ana` | The operator `ana` of that peer, who holds no authority here |
| `keleusma:&claude` | The external agent `claude` of that peer |

- **Why the peer name comes first.** Every line's wake prompt tells it that only a
  sender beginning with `@` is an operator, and that a sender beginning with `&` is an
  external agent. A remote address always begins with a peer name, which is an
  identifier and so begins with a letter or a digit. The rule therefore stays exactly
  true, and no remote party can be read as a local operator. **Inference.** The suffix
  form the operator gave as an example, `kaizen@keleusma`, keeps the rule for lines and
  agents, but writes a remote operator as `@ana@keleusma`, which begins with `@`. A
  model told that a sender beginning with `@` is an operator could then grant a remote
  person operator authority, which is the failure the trust rules exist to prevent. This
  is decision 1 below.
- **Why `:`.** It never appears in an identifier, an operator address or an agent
  address, so a parser splits at the first colon without ambiguity. It needs no quoting
  in a shell.
- **One level only.** A remote address holds exactly one peer name, and the address
  after the colon is never itself remote. There is no relaying through a peer, so no
  routing loop can form, and no daemon carries mail for a project its operator did not
  declare.
- **No host in the address.** **Proposed.** A peer on another machine is named like a
  peer on this one, such as `keleusma-studio:kaizen`. The host, port and pinned key
  belong to the peer's configuration. **Reason.** Peer names are local, like the host
  aliases of an SSH configuration, so a peer that moves to another machine keeps its
  name and every address that uses it. A form such as `line@project@host` would imply
  global names that no daemon can check.
- **How a daemon names its peers.** Only by its operator's configuration. A daemon may
  announce a name of its own when it connects, and the receiver uses that announcement
  only to report a likely misconfiguration, never to attribute mail.
- **How a sender is named at the receiver.** The receiver writes the sender as its own
  peer name for the authenticated connection, followed by the sender's address as the
  envelope gives it. A peer therefore cannot claim to be another peer, whatever its
  envelope says.
- **Envelope addresses follow the grammar.** **Decided.** The receiver checks the
  envelope's sender and recipient against the local address grammar, an identifier, an
  `@` operator address or an `&` agent address, before it records anything. A remote
  form, a second colon, a control character such as a newline, or any other text is
  refused with the code `malformed`. **Reason.** The sender's text is placed in the
  header of a wake prompt and in `list_mail`, so an unchecked address would let a peer
  write into a local line's prompt. The tests include a second colon and a newline.

**The event log.** The address value of [`EVENT_LOG.md`](../protocol/EVENT_LOG.md) gains
a fourth form, the remote address, and its text is the form above. **Fact.** Today
`dromon.model.parse_address` treats every text that begins with neither `@` nor `&` as
a line, and an identifier cannot contain `:`, so no existing log holds a remote address
and none decodes differently. **Implemented on 2026-10-28, step 4 of phase 1.** The
model holds a remote address as a peer's identifier and a local address, and the codec
reads and writes its text form. A line's `list_mail` accepts one as a sender to filter
by. **Implemented on 2026-10-28, step 5.** A line's `send_message`, an agent's, and an
operator's `send` accept a remote address as the recipient, and `message_sent` records
it, as the sending rules below require. The sender of a `message_sent` is never remote.

## Trust

**Fact.** [`CLAUDE.md`](../../CLAUDE.md) requires that messages from other machines and
other harnesses are untrusted data, and that only the operator, through a client, holds
operator authority. The design keeps that without exception.

- **No remote party holds operator authority.** This includes a peer's operators. Mail
  from `keleusma:@ana` is data, as mail from a line is, even when `ana` is a local
  operator too. It does not reset a line's autonomous-turn count, release a held line,
  answer a question or reach any operator request.
- **Default deny, per peer and per direction.** A daemon accepts no peer that its
  operator has not declared, and a declared peer has both directions off until the
  operator turns each on.
- **Which local parties a peer may reach.** Inbound policy lists the local parties a
  peer may send to, by address, such as `["core", "@ana"]`. The default list is
  empty. Mail to any other local party is refused with a code that does not say whether
  the party exists.
- **Which local parties may send to a peer.** Outbound policy lists the local parties
  that may send to a peer. The default list is empty. A line's `send_message` to a
  remote address it may not use is refused and records nothing, as a refusal does
  today.
- **Which remote parties may be addressed.** **Decided.** Outbound policy lists the
  remote addresses allowed, as the peer writes them, in `remote`. The default list is
  empty and refuses every remote address, as the rest of the policy does. **Reason.** An
  allow-all default would let a local line guess at party names, and the peer would
  answer each guess.
- **What a token proves.** **Decided.** A peer's token is a credential of the peer
  daemon, not of any party in it. Whoever holds it can deliver mail under any sender
  address the grammar allows, including one that the sending daemon's own `senders`
  list excludes, because `senders` is enforced only where the mail is sent. The
  receiving daemon therefore enforces `reach` itself on every delivery, so the most a
  token can do is send notes to the local parties its peer may reach, within the limits
  below. A sender's address at the receiver is the sending daemon's statement, and is
  shown as that.
- **Any unsandboxed process of the same user can act as a peer.** **Decided, stated
  here as a fact of the design.** The peer credential check proves the user and not the
  program. A process of the operator's user that is not confined by a sandbox can read
  the outbound token under `.dromon/peers/` and connect to a peer's `peers.sock`. The
  token file has mode 0600 in a directory of mode 0700, and every sandboxed line is
  denied it with the rest of the state directory. Nothing in Dromon protects it from an
  unconfined process of that user, which includes an unconfined line.
- **Unconfined engines.** **Decided, and implemented on 2026-10-28 in step 2.** While a
  project declares any peer, the daemon refuses to start, and refuses to create a line
  or move one by rebinding, when the line would run on an engine that Dromon cannot
  confine, which today is Pi, Cursor or Antigravity, unless the project file accepts it
  with `[federation] accept_unconfined = true`. A reload that changes the peer tables
  is refused in any case, since they apply only at start. **Decided by the operator on
  2026-10-28, and implemented in step 3.** A Claude Code line without an enabled sandbox
  is unconfined too, so the same rule applies to it, and an enabled sandbox, which
  `daemon.require_sandbox` requires, satisfies it. **Fact.** Codex lines run under the `dromon-line` profile and Grok lines
  under the operator's checked profiles, both of which deny the state and runtime
  directories, as [`CODEX.md`](../protocol/CODEX.md) and
  [`GROK.md`](../protocol/GROK.md) specify.
- **Questions stay local.** Only notes cross between daemons. A line asks its own
  operators with `ask_operator`, and a remote operator is reached by a note. **Reason.**
  A question is a tracked obligation of a local operator, and the event log's question
  rules assume both ends are local. Remote questions are out of scope below.
- **External agents.** A local agent may send to a peer only when the outbound policy
  lists it, and its mail stays under the rules of [`AGENTS.md`](../protocol/AGENTS.md).
- **Operator configuration only.** The peer table lives in the project file, which an
  operator owns, and its secrets live under the state directory, which lines cannot
  read. No line tool and no agent tool can declare a peer or change its policy.

## Configuration

**Proposed.** A `[peers]` table in `.dromon/dromon.toml`, recorded in the event log at
start and at reload, as roles are. Secrets are never in the file.

```toml
[federation]
listen = true                       # open peers.sock, needed for any inbound peer
# tls_listen = "192.0.2.10:7411"    # phase 2, only for a tls peer with inbound, no default
accept_unconfined = false           # allow peers while a line runs unconfined, decision 9
waiting_per_recipient = 200         # mail from all peers waiting for one local party

[peers.keleusma]
transport = "unix"                  # or "tls" in phase 2
socket = "/Users/ana/projects/rust/keleusma/.dromon/run/peers.sock"
project = "/Users/ana/projects/rust/keleusma"   # its .dromon is denied to local lines
inbound = true                      # the peer may deliver mail here
outbound = true                     # this daemon may deliver mail to the peer
reach = ["core", "@ana"]            # local parties the peer may send to
senders = ["core", "review", "@ana"]   # local parties that may send to the peer
remote = ["kaizen", "@ana"]         # the peer's parties local senders may address
priority_in = false                 # honour the peer's priority flag
priority_out = false                # let local senders mark mail to the peer priority
max_body_bytes = 65536              # the largest body accepted from or sent to the peer
expire_hours = 168                  # how long mail to the peer waits before it expires
turns_per_hour = 6                  # turns the peer's mail may start for one local line
waiting = 500                       # mail from the peer waiting for all local parties

[peers.keleusma-studio]
transport = "tls"
address = "studio.example.net:7411"
pin_sha256 = "<the peer's public key digest>"
certificate = "peers/keleusma-studio.pem"   # the peer's public certificate, under .dromon
inbound = false
outbound = true
senders = ["core"]
```

- **Discovery is explicit.** A peer's socket path, or its host and port, is written in
  the configuration. The daemon never scans the file system or the network for peers.
- **Tokens and keys.** `dromon peer token NAME` issues the token a peer presents to this
  daemon, prints it once, and stores only its SHA-256 digest, as `dromon agent add` does.
  The token the peer issued for this daemon, and this daemon's own private key in phase
  2, are stored under `.dromon/peers/`, a directory of mode 0700, in files of mode 0600.
  **Decided.** The daemon refuses to start when either mode is wider, and every
  sandboxed line is denied the directory with the rest of the state directory. Both
  commands go to the operator socket. **Implemented on 2026-10-28, step 3 of phase 1.**
  `dromon peer store NAME` stores the peer's token, read from standard input so that it
  never appears in the process list or the shell's history. `dromon peer revoke NAME`
  revokes the token issued, and `dromon peer list` shows whether each token is issued
  and stored, never a token. The start check reads the store with `lstat`, so a
  symbolic link is refused, and it is not repeated while the daemon runs, so a mode
  widened after start is seen at the next start or the next store.
- **The peer's project.** A `unix` peer may name its project root in `project`, so that
  its `.dromon` is denied to local lines as below. The parent of its `socket` is denied
  in any case, since that is its runtime directory.
- **A pin is public.** A key's digest is not a secret, so it may sit in the project
  file, where review can see it.

## Same machine

**Proposed.** Phase 1.

- **Socket.** `peers.sock` in the runtime directory, with mode 0600, created only when
  `[federation] listen` is true and removed when the daemon stops. It speaks the framing
  of [`CLIENT.md`](../protocol/CLIENT.md) and serves only the federation operations
  below. The operator socket and the agent socket refuse them, so a peer reaches nothing
  else. **Implemented on 2026-10-28, step 4 of phase 1,** with the line socket refusing
  them too, as [`CLIENT.md`](../protocol/CLIENT.md) specifies under the peer socket.
- **One connection per direction.** A daemon delivers its outbound mail by connecting to
  the peer's `peers.sock`, and receives inbound mail only on its own. Each direction then
  has one listener and one connector, so the per-direction policy maps onto the
  transport directly.
- **Peer credential check.** **Implemented on 2026-10-28, step 1 of phase 1.** On
  every accepted connection, on every socket the daemon serves, the daemon reads the
  connecting process's user identifier and refuses one that differs from its own, as
  [`CLIENT.md`](../protocol/CLIENT.md) specifies under trust. **Fact, checked on
  2026-10-28 on this machine with Python 3.12.12.** Python's `socket` module offers
  `LOCAL_PEERCRED` on macOS and not `SO_PEERCRED`, which is Linux's interface. macOS
  returns a `struct xucred` of 76 bytes that Python does not decode, and its version
  and user identifier read correctly when unpacked with `struct`, which is a porting
  hazard below. The Linux path is covered by continuous integration's tests only, not
  observed here.
- **Token.** The connecting daemon presents the token this daemon issued for it, compared
  in constant time against the stored digest. **Implemented on 2026-10-28, step 4.**
  The token alone names the peer, compared against every keyed peer's digest, and
  `deliver` checks again that the peer's digest is the one its token matched, so a
  token revoked or replaced during a connection delivers nothing more. A `hello` is
  accepted for a keyed peer whatever its `inbound`, and `deliver` then refuses with
  `refused`, as the receiving rules below require. **Reason.** The peer credential check
  proves only that the caller runs as the same user. **Fact.** A line runs as that user
  too. The token separates a peer daemon from a line's shell, and from another peer.
- **What the token rests on.** The token is kept from lines by the sandbox rule that
  denies every line Dromon's state and runtime directories, and from other users by its
  file mode. It is not kept from an unconfined process of the same user, as the trust
  section states, which is why decision 9 refuses peers while a line runs unconfined.
- **Before `hello`.** **Decided by the operator on 2026-10-28, as decision 20, and
  implemented in step 5.** A connection's first line must arrive within 10 seconds and
  hold at most 65,536 bytes, and anything but an accepted `hello` closes the connection
  after its answer. The stream's own limit is that of the `hello` line, and an
  identified peer's larger lines are read in chunks up to 8,388,608 bytes, so a process
  that holds no token can make the daemon buffer about twice 65,536 bytes per
  connection, the stream's flow control, and no more. An undeclared peer name and a
  wrong token get the same refusal, so a caller cannot learn which peer names are
  declared. **Decided by the operator later on 2026-10-28, as decision 23.** The
  listener owns the cap on connections, which is 16 at once, counted before the
  `peercred` check, and a further connection is answered `rejected` and closed. The
  number 16 is the core line's choice, not the operator's, and leaves room beyond one
  connection per configured peer for one that a restarted peer left open until its idle
  timeout. **Inference.** A process that holds no token can therefore make the daemon
  buffer about 16 times twice 65,536 bytes before `hello`, and it can also hold every
  slot for 10 seconds at a time, which keeps the configured peers out while it does.
  That denial of service needs a process of the operator's own user, which the trust
  section already does not defend against.
- **The peer's directories.** **Implemented on 2026-10-28, step 2 of phase 1.** Each
  declared peer's runtime directory, the directory of its `socket`, and the `.dromon` of
  its `project` when one is named, join the project's protected directories. Every
  local line's enabled sandbox then denies them, the `dromon-line` profile of Codex
  lines denies those that exist, and Dromon's check of the Grok profiles requires them,
  as [`PROJECT_CONFIG.md`](../protocol/PROJECT_CONFIG.md) specifies under guards.
  **Fact.** Before this, a line's sandbox denied its own project's `.dromon` and nothing
  of another project's. **Limits.** Only declared peers are protected, since the daemon
  knows no other project, and a peer whose `project` is not named has only its runtime
  directory protected. A `workdir` that contains a peer's directory is refused, as for
  the project's own, so a peer nested inside a line's working directory other than the
  project root stops the start.
- **Confidentiality.** No encryption on the socket. **Reason.** Both ends run as one
  user on one machine, and the runtime directory has mode 0700, so encryption would
  defend against nothing the file permissions do not.
- **Different users on one machine.** Out of scope. The check refuses a different user,
  and a later design may allow one by configuration or use phase 2 over the loopback
  interface.

## Different machines

**Fact.** [`DESIGN.md`](DESIGN.md) requires real mutual authentication and replay
protection before layer 3, and the operator requires confidentiality and standard
primitives from standard libraries.

**Proposed.** Transport Layer Security version 1.3 with mutual authentication, in which
each side presents a self-signed certificate and checks the other's against a pinned
SHA-256 digest of its public key.

- **Keys.** `dromon peer key` makes the daemon's key pair once, Ed25519 alone as
  decision 21 settles, and prints its pin.
  Operators exchange pins out of band and write the peer's pin in the configuration.
- **Handshake.** Version 1.3 only, with early data refused, since data sent in a
  zero round trip handshake can be replayed. The pin is the identity, so no certificate
  authority is involved and the certificate's names are not checked.
- **Client authentication is required.** **Decided.** The listener requires a client
  certificate, with `ssl.CERT_REQUIRED`, and checks its pin against the peer's before it
  reads any federation frame. A connection without a certificate, or with one whose pin
  matches no declared `tls` peer, is closed. The tests include such a connection.
  **Proposed, not yet decided.** A handshake that has not finished within 10 seconds
  is closed, and at most 8 unauthenticated connections are open at once.
- **Pinning in Python.** **Inference, not tested.** Python's `ssl` checks a presented
  certificate against the certificates it trusts, dates included, and requests a
  client certificate only when it will check one. The prototype therefore trusts
  exactly the peer's own self-signed certificate, which operators exchange as a public
  file beside the pin, makes certificates valid for ten years, and compares the pin
  after the handshake as a second check. `rustls` allows a verifier that checks the pin
  alone.
- **What it gives.** Mutual authentication by the pins, confidentiality and integrity
  by the record layer, and protection against the replay of records within a
  connection. Replay across connections is handled by delivery below, through origin
  identifiers that make every resend harmless.
- **Libraries.** **Fact.** Python's standard `ssl` module, here with OpenSSL 3.5.4,
  supports version 1.3 and can require a client certificate. It cannot generate keys or
  certificates. **Fact.** `cryptography` is in `uv.lock` today, but only through the
  `crypto` extra of `pyjwt`, a dependency of a dependency, and not as Dromon's own.
  **Decided, as decision 4.** It becomes a direct dependency for key generation at the
  cross-machine step. The Rust port uses `rustls` with a verifier that compares the pin.
- **Listener.** A TCP listener only when a `tls` peer has `inbound = true`, bound to
  the address the operator configures, with no default address. **Inference.** It is
  Dromon's first network listener, which [`CLIENT.md`](../protocol/CLIENT.md) has so far
  avoided, so it is opt-in per project and serves only federation.

**SSH, considered and not recommended.** A daemon could start `ssh` to the peer's
machine with a key restricted by a forced command that bridges to the peer's
`peers.sock`. It would add no listener and no key format, and SSH gives the same three
properties. It is not recommended for these reasons.

- **Inference.** The peer's identity would then rest on a forced command in an
  `authorized_keys` file, and the bridge process would claim the peer name on a local
  socket, where a line of that machine's user could make the same claim. With pinned
  keys the identity is checked in the handshake itself.
- **Inference.** Each peer needs a login account on the other machine. A mistake in the
  key's restrictions grants a shell, which is a larger exposure than a listener that
  speaks only the federation protocol.
- **Inference.** The daemon would hold an SSH key and start a client it does not
  control, which makes it more of a credential broker than
  [`DESIGN.md`](DESIGN.md) allows without an explicit allowance.

This is decision 4 below. SSH remains a reasonable choice if the operator prefers no
listener at all.

**Deferred.** Key rotation beyond replacing a pin, revocation lists, traversal of
network address translation, discovery, and peers that reach each other only through a
third daemon.

## Delivery

### Sending

1. A local party sends to a remote address. The daemon checks that the peer is declared,
   that outbound is on, that the sender is in `senders`, that the remote address is in
   `remote`, that the body is within `max_body_bytes`, and the disclosure list, below. A
   refusal records nothing.
2. The daemon appends `message_sent` with the remote recipient. The message is now in
   the peer's outbox, and is durable before any byte leaves.
3. The forwarder for that peer takes the oldest message in its outbox and delivers its
   envelope. The peer answers `accepted` only after its own append has committed.
4. On `accepted`, the daemon appends `message_delivered`. For a remote recipient it
   means accepted by the peer, as it means accepted by a relay for an operator today.
   The sender's `list_sent` shows `delivered to peer`, not read.
5. On a permanent refusal, the daemon appends `message_returned` with the refusal's
   code, and the outbox moves to the next message. On a transient one it retries the
   same message later.

**Implemented on 2026-10-28, step 5 of phase 1,** in `dromon.forwarder`, with the model's
outbox in `State.outbox`. Choices the design left open, for the operator's review.

- **The policy is checked at send and again before each attempt.** A message whose
  sender, recipient, size or text no longer passes the policy or the disclosure list in
  force is returned with `refused`, `too_large` or `disclosure`, never sent. **Reason.**
  A restart may change the project file, and the design already re-checks the
  disclosure list there.
- **Priority mail to a peer is refused at send** until step 6, whatever `priority_out`
  says, and the envelope's `priority` is false.
- **A `tls` peer is refused at send** until phase 2. Mail already waiting for a peer
  whose transport became `tls` waits and expires, since no connector reaches it.
- **The token is read at each connection,** so a token stored with `dromon peer store`
  while the daemon runs is used at the next attempt. With none stored, the forwarder
  backs off and says so on standard error.
- **The transport seam.** A connector opens a link that carries one request and its
  answer at a time. `connect_unix` is phase 1's, and the TLS transport of the engines
  role's `dromon.federation_tls` becomes another connector in phase 2. `hello` and
  `deliver` are the forwarder's and do not change, except that `hello` carries the
  token only where the transport needs one.
- **The name announced in `hello`** is the project directory's name, lowercased, when
  it is an identifier the disclosure list does not cover, and `dromon` otherwise.
- **An answer that is not an answer,** an error response, a malformed result or a
  refusal code outside the table, is transient and backs off. A malformed one counts as
  unanswered, so the message may expire as unconfirmed. An error response says the peer
  recorded nothing, so it does not.
- **No outage note and no peer state in `status` yet.** **Not built in step 5.** Both
  are in the design under a peer that is down.

### Receiving

1. The peer's connection is authenticated, and the daemon checks that both addresses
   follow the grammar, that inbound is on, that the recipient is in `reach` and exists,
   that the kind is `note`, that the body is within `max_body_bytes`, the rate limits
   below, and the bounds on waiting mail. `reach` is checked here whatever the sending
   daemon checked, since the token does not prove the sender.
2. The daemon appends one `message_received` event, which records the message, its
   remote sender, and the peer's origin identifier. One event makes the arrival and its
   idempotency key a single append, so a crash cannot record one without the other.
3. It answers `accepted` with its own identifier for the message. The message then goes
   to the post office at the end of the tick, like any other mail, and wakes its
   recipient as local mail does.

**Implemented on 2026-10-28, step 4 of phase 1,** in `dromon.peering`, with the order
of checks that [`CLIENT.md`](../protocol/CLIENT.md) gives. Four choices the design left
open. Every `deliver` counts toward the rate limit, not only the accepted ones, so a
peer cannot make the receiver check without limit by sending refused mail. A resend is
answered as a duplicate or a conflict before the policy is read, so a policy narrowed
after the first delivery cannot refuse mail that was accepted, which would let its
sender return a message its recipient holds. A question is refused with `refused`,
since only notes cross. A note's priority is answered false until step 6. Peer mail
never clears a line held out of scheduling by a failed turn, which local mail does,
so a peer cannot restart a failing line.

### Ordering

- **Proposed.** Mail to one peer is delivered in the order it was sent here, one
  message at a time, and each peer appends what it accepts in the order it arrives. The
  receiver therefore sees each peer's mail in its send order.
- Mail to two different peers, or from two peers, has no order between them.
- A message that fails transiently holds back every later message to the same peer.
  **Reason.** Ordering is worth more between collaborating lines than throughput, and a
  queue rarely holds more than a few messages.

### Acknowledgement and idempotency

- **At least once on the wire, exactly once in the log.** A sender resends until it
  sees `accepted` or a permanent refusal. The receiver refuses a second
  `message_received` with the same peer and origin identifier, and answers the resend
  with `accepted`, the identifier it gave the first time, and `duplicate` true.
- **A conflicting duplicate is an error.** **Decided.** When a resend's sender,
  recipient, body, `reply_to` or kind differs from the message first recorded under that
  peer and origin identifier, the receiver records nothing and refuses it with the code
  `conflict`, and writes a line to its standard error. **Reason.** Answering it as a
  duplicate would drop the second body while its sender believed it delivered. Since the
  event log never reuses an identifier, a conflict means a fault or a forgery at the
  sending end.
- **Durable, unlike request keys.** The key is in the event log, so it survives a
  restart of either daemon and never expires. **Fact.** A line's request keys of
  [`LINE_CONFIG.md`](../protocol/LINE_CONFIG.md) live in memory only, and a retry across
  a restart acts again. They keep doing so for the tool call itself, and a remote
  message, once recorded, is never sent twice.
- **No reused identifiers.** **Fact.** The event log never reuses a message identifier,
  so an origin identifier names one message of its peer forever.
- **Replies.** The receiver gives each message its own identifier. When a local party
  replies to a remote message, the envelope's `reply_to` carries the original's origin
  identifier, which is the peer's own identifier for the message being answered. The
  peer then checks the reply's route against its own log, as the event log's reply
  rule does locally. A `reply_to` that the peer does not know is refused permanently.

### Waking a remote recipient

The receiver wakes its own line, through the ordinary tick, once the message is in its
post office. **Inference.** The sender cannot wake a remote line directly, and does not
need to. Delivery into the receiver's log is what the layer 3 row of
[`DESIGN.md`](DESIGN.md) already describes. The federation listener notifies the
receiver's loop as the operator socket's `send` does, so a resting loop starts a tick.

**Decided.** Mail from a peer never counts toward a line's autonomous-turn allowance
and never resets it, so a peer can neither hold a local line nor release one.
**Implemented on 2026-10-28, step 4 of phase 1.** A turn has one of three causes,
operator, autonomous or peer, and the prompt's opening line says which. A turn that
only peer mail started is charged to each peer whose mail in it had allowance left.
Peer mail past its allowance does not keep the scheduler ticking, and the scheduler's
rest, at most 60 seconds, finds it once the hour allows, so it may start its turn up to
a minute late. A turn that only peer mail started never holds the line for the
autonomous-turn limit, even when an override has lowered the limit below the count.

- **Which turns count.** A turn woken only by peer mail neither counts nor resets. A
  turn that local line or agent mail also woke counts as it does today, and a turn that
  operator mail woke resets the count as it does today. Peer mail that waits for a turn
  started otherwise is read in it and changes nothing.
- **The peer's own allowance.** Each peer may start at most `turns_per_hour` turns, 6
  by default, for one local line in any hour. Past that, the peer's mail is still
  accepted within the bounds below and waits, and it wakes the line again when the hour
  allows, or is read in a turn started otherwise. The line is not held. The allowance is
  kept in memory and a restart resets it, which, as for every limit kept in memory, only
  the operator can cause.
- **Ping-pong between daemons.** **Inference.** Two lines in two daemons that mail each
  other are bounded by each daemon's per-peer allowance, by the inbound rate limit, and
  by each line's own autonomous-turn limit for the turns its replies start locally.
- **Cost is still counted.** **Fact.** The tokens a turn spends are real whatever woke
  it, so a turn started by peer mail counts toward the line's cost limit, and that limit
  can hold the line. **Inference.** The per-peer allowance bounds how fast a peer can
  bring a line to it, not whether. **Decided by the operator on 2026-10-28, as decision
  17.** Peer turns stay under the line's existing cost hold, with no cap of their own.

### Priority mail

- **Proposed.** A remote sender carries no operator authority, so remote priority mail
  follows the rules for priority from a line in [`PRIORITY.md`](../protocol/PRIORITY.md),
  and never the unlimited rule for operators, even when the sender is a remote operator.
- It may end a turn at a safe point only when the receiver's `priority_in` is true for
  the peer. The receiver counts the whole peer as one sender for the rate limit of three
  per window, so a peer cannot multiply the limit with many sender names, and the
  recipient's limit of two from lines per window applies as it does locally.
- With `priority_in` false, or past a limit, the receiver accepts the note as ordinary
  mail and answers `accepted` with `priority` false. The sender records nothing more,
  and the note's header at the receiver says that its priority was not honoured.
- A sender may mark mail to a peer priority only when its own `priority_out` is true for
  that peer.

### Hibernated and held recipients

**Proposed.** The receiver accepts mail for a hibernated or held line, as it keeps local
mail for one, so the sender's outbox is not blocked. The mail waits, as
[`PRIORITY.md`](../protocol/PRIORITY.md) specifies, and priority mail ends no turn of a
hibernated line. The receiver does not tell the sender that the line is hibernated.
**Reason.** That would disclose the receiver's operational state to another project.

**Decided.** Waiting mail is bounded per recipient and per peer. Waiting mail is mail
received from a peer and not yet delivered to its recipient, which the log records, so
the bounds survive a restart.

- At most `waiting_per_recipient` messages from all peers wait for one local party, 200
  by default.
- At most `waiting` messages from one peer wait for all local parties, 500 by default.
- A delivery past either bound is refused with the code `full`, which is permanent, so
  the sender's queue moves on. **Inference.** `full` tells a peer that a party it may
  reach has a backlog, which is less than its state and is checked only after `reach`,
  so it reveals nothing about parties the peer may not reach.
- **Growth.** **Inference.** The bounds cap what one recipient can be sent while it
  does not run, but accepted mail stays in the log, which has no compaction, so they
  bound the rate of growth and not its total.

### When a peer is down

- **Retry.** The forwarder retries the head of a peer's outbox with exponential backoff
  from 5 seconds to 15 minutes, reset on success and on a restart. New mail ends an idle
  wait and never a backoff. **Implemented in step 5.**
- **Busy.** **Decided by the operator on 2026-10-28, as decision 18, and implemented in
  step 5.** A `busy` answer waits the seconds the peer asks, at least 1 and at most the
  backoff's 15 minutes. The 20th `busy` answer for one message ends it as an expired
  message, returned with `expired` when no copy of it can have reached the peer, and
  unconfirmed otherwise.
- **Expiry.** **Decided.** A message older than the peer's `expire_hours`, a week by
  default, leaves the outbox. If its envelope may have reached the peer without an
  answer, its outcome is unknown and the daemon records `message_unconfirmed`, not a
  return. Only a message whose envelope was never written is returned with the code
  `expired`. **Reason.** A peer may have accepted a message whose answer was lost, and
  recording it as returned would tell its sender it was not delivered, so a resend by
  hand would duplicate it. The forwarder marks a message in memory before it first
  writes its envelope, and after a restart it treats every waiting message as possibly
  written, which errs toward unknown. The note to a line sender says that the message
  may or may not have been delivered.
- **Clocks.** **Implemented in step 5.** Backoff runs on the monotonic clock. Expiry
  compares the wall clock with the `recorded_at` of the message's `message_sent`, read
  from the log at start, and with the time the forwarder was told of mail sent while it
  runs. **Porting note.** The model never reads `recorded_at`, and only this runtime
  decision does. A wall clock moved forward expires mail early, and the outcome is then
  unknown as above.
- **Bounds.** At most 1,000 messages wait for one peer. A send beyond that is refused at
  once and records nothing. **Implemented in step 5,** as a rule of `message_sent`.
- **Telling the operator.** `status` and `line_status` show each peer's state, its
  oldest waiting message and the time of its last contact. When a peer's oldest message
  has waited an hour, the daemon sends one note to its default operator for that
  outage, and no more until the peer has accepted mail again.
- **Restart.** The outbox is derived from the log, so a restarted daemon resumes it in
  order with nothing lost.

### Returned mail

`message_returned` carries a fixed code, never the peer's free text, so a peer cannot
put text into the sender's prompt by refusing.

| Code | Meaning |
|---|---|
| `unknown_peer` | The peer was removed from the configuration while the message waited |
| `refused` | The peer's policy refuses this sender or recipient, or the recipient does not exist there. One code for all three, so that a sender cannot probe which parties exist |
| `too_large` | The body exceeds the peer's limit |
| `unknown_reply` | The peer does not know the message this one replies to |
| `disclosure` | The body now holds a term of this daemon's disclosure list, below |
| `expired` | The message waited longer than `expire_hours`, or had 20 `busy` answers, and no copy of it can have reached the peer |
| `malformed` | An address in the envelope does not follow the grammar |
| `conflict` | The peer holds a different message under the same origin identifier |
| `full` | The recipient, or the peer's share of the receiver, has as much mail waiting as the bounds allow |

**Inference.** `too_large` and `full` say more than `refused` about the peer's policy and
state, and are given only after `reach` passed, which the review line found acceptable.

When the sender is a line, the same event records a note from the line to itself, as
`task_noted` does, so its next wake prompt says which message was returned and why. An
operator sees it in `dromon sent`. **Decided by the operator on 2026-10-28, as decision 19.** No batching in
version 1. Each returned or unconfirmed message gives its line sender one note, and the
notes of one tick wake the line once, as any mail does. **Implemented in step 5.** The
note's text is derived from the code by the model, so it holds only Dromon's own words.

## Disclosure

- **Every message leaving a daemon is checked against that daemon's disclosure list**
  at send, and again by the forwarder just before the envelope leaves, since a reload
  or a restart may have changed the list. A message that fails the second check is
  returned with `disclosure` and never sent.
- **Operator mail to a peer is checked too.** **Proposed.** Today operator mail is not
  checked, since it stays inside the daemon. Mail to a peer leaves the project, and
  disclosure cannot be undone, so the operator's own mail to a peer is held to the list
  as well. This is decision 3 below.
- **Inbound mail is not checked** against the receiver's list. **Inference.** Inbound
  text has already left its own daemon, and the receiver's list guards what leaves the
  receiver, which its own tools already check.
- **Metadata leaves too.** **Fact.** An envelope carries the sender's address, so a
  peer learns the names of the local lines and operators that write to it. Those names,
  and the name a daemon announces in `hello`, are checked against the disclosure list
  as the body is.
- **Peer names and policies stay local.** A peer never learns what this daemon calls
  it, which parties it may reach, or why mail was refused beyond the fixed codes above.

## Roles and seats

**Proposed.** Roles, seats, ownership, the role journal and provider budgets are local
to each daemon and never cross a peer.

- A remote party cannot be a seat, own a role, write a journal entry or take, hand over
  or relinquish ownership. The role tools refuse a remote address.
- A peer's budget statements are never imported. **Inference.** Two projects on one
  machine may draw on the same subscription, so sharing budgets would be useful, but a
  budget a peer reported would then move local roles, and the operator decided on
  2026-10-27 that only observed and typed shapes may move a role. Sharing budgets is out
  of scope below.
- Leases, scheduled wakes, processes and `line_status` stay local. A remote party sees
  none of them.

## Event log

**Proposed.** New and changed events, each applied by the model so that replay rebuilds
the outbox, the idempotency keys and each peer's policy exactly.

| Event | Fields | Applies when |
|---|---|---|
| `peer_declared` | `peer_id`, `transport` of `unix` or `tls`, `policy` canonical JSON text | The peer is new or its transport or policy changes. Recorded at start and at reload from the project file. The policy holds the directions, `reach`, `senders`, the priority flags, the limits and the pin, and never a token |
| `peer_removed` | `peer_id` | The peer is declared. Addresses through it are refused from then on, and the daemon returns every message still waiting for it with `unknown_peer` |
| `peer_keyed` | `peer_id`, `token_sha256` | The peer is declared. The digest of the token this daemon issued for the peer, or empty to revoke it. Recorded by `dromon peer token` |
| `message_sent` | Unchanged | Also applies to a remote recipient when its peer is declared and not removed, the sender is local and the kind is `note`. The remote party's existence is the peer's to check |
| `message_received` | `message_id`, `sender` remote address, `recipient` local address, `body`, `reply_to` local message identifier or `null`, `origin_id`, `received_at` | The sender's peer is declared and not removed, no earlier `message_received` kept has this peer and origin identifier, and the message rules of [`EVENT_LOG.md`](../protocol/EVENT_LOG.md) hold for a note, with the remote sender standing in for an existing one. Implemented in step 4, and `received_at` in step 5 for the bound on the keys |
| `message_prioritized` | Unchanged | Also follows a `message_received` when the receiver honours its priority |
| `message_delivered` | Unchanged | For a remote recipient, the peer accepted the message |
| `message_returned` | `message_id`, `code`, `note_id` message identifier or `null` | The message has a remote recipient and is neither delivered, returned nor unconfirmed. With a line sender, `note_id` names the note to the line, checked as `task_noted` is |
| `message_unconfirmed` | `message_id`, `note_id` message identifier or `null` | As for `message_returned`. The message expired after its envelope may have reached the peer, so its outcome is unknown |

**Implemented on 2026-10-28, step 3 of phase 1.** `peer_declared`, `peer_removed` and
`peer_keyed`, as [`EVENT_LOG.md`](../protocol/EVENT_LOG.md) specifies, recorded at start
from the project file and by `dromon peer token` and `dromon peer revoke`. Three
differences from the table. The policy holds every key of the peer's table but
`transport`, including `socket` and `project`, since a `unix` peer has no pin.
`peer_removed` clears the token digest, so a peer declared again needs a new token.
**Reason.** Default deny, since a removed peer's token should not come back with a later
declaration. Returning waiting mail with `unknown_peer` comes with the outbox, since no
mail waits for a peer yet.

**Replay.** The outbox of a peer is its messages with a remote recipient through it,
neither delivered, returned nor unconfirmed, in `seq` order. The waiting mail of the
bounds above is every `message_received` not yet delivered. The idempotency keys are
the peer and origin identifier of every `message_received`. Backoff timers, the turn
allowances, the rate limit and the outage note's flag are in memory, and a restart
resets them. That only retries sooner, may send one more outage note, and gives each
peer a fresh allowance, and only the operator restarts the daemon.

**Implemented on 2026-10-28, step 5 of phase 1.** `message_sent` to a remote party,
`message_returned` and `message_unconfirmed`, as [`EVENT_LOG.md`](../protocol/EVENT_LOG.md)
specifies. The outbox is the model's `outbox`, rebuilt by replay. At start the daemon
returns the mail of a removed peer with `unknown_peer` before its forwarder runs.

**Growth.** **Decided by the operator on 2026-10-28, as decision 22, and implemented in
step 5.** The idempotency keys are bounded by the expiry window. Applying a
`message_received` drops the keys received more than 2,184 hours before it, the longest
`expire_hours` of 90 days and one day for the difference between two daemons' clocks.
**Reason.** A sender never resends a message past its expiry, so an older key would see
no resend. `received_at` is in the event, so replay drops the same keys. **Inference.**
A sender whose clock runs more than a day behind, or a daemon that is not Dromon, could
resend past the window, and the resend would be recorded as a new message. The messages
themselves still grow with the log, which has no compaction.

## Wire protocol

**Proposed, and implemented for the receiving end on 2026-10-28 in step 4.** Federation
version 1, framed as [`CLIENT.md`](../protocol/CLIENT.md) frames requests, one canonical JSON object per line. A frame is at most 8,388,608 bytes,
the bound of an encoded event, so the largest body a policy may permit fits even when
escaping multiplies its size.

| Operation | Arguments | Answer |
|---|---|---|
| `hello` | `versions`, the federation versions the caller speaks, `name`, the name the caller calls itself, advisory only, and on `unix` the `token` | `version`, the one chosen, or a refusal. Nothing else is accepted before it |
| `deliver` | `origin_id`, `sender` and `recipient` as each daemon writes its own parties, `kind` `note`, `body`, `reply_to` origin identifier or `null`, `priority` boolean | `accepted` with `message_id`, `duplicate` and `priority`, or `refused` with a code from the table above and `retry` false, or `busy` with `retry_seconds`, which is transient |

**Proposed.** Each daemon accepts at most 60 messages from one peer in 600 seconds and
answers `busy` past that, so one peer cannot flood a project. The limit is kept in
memory, so a restart resets it, as it resets the backoff. `deliver` is answered in
order on one connection, which keeps the per-peer order of delivery.

**Proposed, not yet decided.** The receiver reports a `hello` name that differs from the
one last seen for that peer in one note to its default operator, and no more until the
daemon restarts, so a peer that changes its name on each connection sends one note.

## Porting

- **Addresses.** The remote address becomes a variant of the address `enum`, holding a
  peer name and a local address. Its text form is the same in every implementation.
- **State.** The outbox, the idempotency set and the peer table are plain data rebuilt
  from the log. The forwarder is an explicit state machine per peer, waiting, sending,
  backing off or idle, with a bounded queue, so it maps onto a Keleusma stream processor
  whose input and output the host performs.
- **Peer credentials.** **Porting hazard.** The prototype reads `LOCAL_PEERCRED` on
  macOS and unpacks `struct xucred` with Python's `struct`, and reads `SO_PEERCRED` on
  Linux. **Inference, from the review line, not checked against a current release.** A
  Rust port should use each platform's peer credential option, which the `nix` crate
  exposes as one call per system, rather than `getpeereid`, which Linux lacks. The
  standard library's `peer_cred` is still an unstable feature. The behaviour on macOS
  and on Linux is stated separately in step 1's tests.
- **Bounded memory.** **Decided, as decision 22.** The idempotency set holds only the
  keys of the expiry window, so a Keleusma implementation needs room for the mail one
  window can bring, which the rate limit bounds at 60 messages in 600 seconds for each
  peer.
- **Transport Layer Security.** Python's `ssl` in the prototype, `rustls` in Rust, and
  the host's library under Keleusma, which has no cryptography of its own and should not
  gain any.

## Failure modes

| Failure | Outcome |
|---|---|
| A peer claims another peer's name in its envelope | The receiver names the sender from the authenticated connection, so the claim is ignored |
| A local line connects to `peers.sock` | Its sandbox denies the runtime directory and the token. An unconfined line could act as a peer, so peers are refused while one runs, unless the operator accepts it |
| A holder of a peer's token sends as a party its daemon would not allow | The receiver still enforces `reach`, so the token reaches only what the peer may reach |
| A peer puts a newline or a nested address in an envelope | Refused with `malformed` before anything is recorded |
| A remote operator's mail is read as operator mail | The address begins with a peer name, never `@`, and peer mail neither counts toward nor resets the autonomous-turn allowance |
| A peer floods a line with mail to hold it | Peer mail cannot hold a line through its turn allowance. The per-peer turn allowance and the waiting bounds limit it, and the line's cost limit remains, as an open point |
| Two lines in two daemons mail each other without end | The per-peer turn allowance, the inbound rate limit and each line's own limits for the turns its replies start |
| Mail piles up for a hibernated or held line | Refused with `full` past the per-recipient and per-peer bounds |
| A crash after the receiver's append but before the sender records acceptance | The sender resends and the receiver answers `duplicate` with the first identifier |
| A peer reuses an origin identifier with another body | Refused with `conflict`, and the first message stands |
| A message expires after its envelope may have arrived | Recorded as unconfirmed, never as returned |
| A crash between `message_received` and `message_prioritized` | The note is delivered as ordinary mail, as for local priority mail |
| A disclosure term is added while mail waits | The forwarder's second check returns the mail with `disclosure` |
| A peer is down for days | Its mail waits in order and then expires, as returned or unconfirmed, and the operator is told once an hour has passed |
| A connection to a TLS listener presents no pinned certificate | Closed in the handshake, before any frame is read |
| A peer's token or key leaks | The operator issues a new token or replaces the pin, and the old one is refused at the next connection |
| A peer refuses with crafted text | Only fixed codes are recorded and shown |
| A message's transient failure blocks a peer's queue | By design, for order. Expiry bounds the wait, which is a week by default and an open point below |
| A line reads another project's event log through the file system | Already possible today. Phase 1 denies declared peers' directories to every local line |

## Out of scope

Each of these is deliberately left out of the first design, and can be added without
changing the envelope's version 1, unless noted.

- Questions to a remote operator, and answers from one.
- Read and acknowledgement receipts from a peer. A sender sees only that the peer
  accepted its mail. Receipts would also tell a peer how its mail was handled, which is
  a disclosure decision of its own.
- Relaying through a peer, broadcast and group addresses.
- Discovery of any kind, and different users on one machine.
- Remote clients, remote external agents and remote `line_status`.
- Sharing roles, seats, leases, scheduled wakes or provider budgets.
- Key rotation, revocation lists and traversal of network address translation.

## Decisions for the operator

1. **Address form.** The recommendation is `keleusma:kaizen`, `keleusma:@ana` and
   `keleusma:&claude`, so the rule that only a sender beginning with `@` is an operator
   stays true. The alternative is the suffix form `kaizen@keleusma`, with the prompt rule
   changed to recognize `@ana@keleusma` as remote.
2. **No host in the address,** with every peer named locally and its location in the
   configuration.
3. **Operator mail to a peer is held to the disclosure list,** which is the one place
   operator mail would be checked.
4. **Transport Layer Security version 1.3 with pinned keys** for different machines, with
   `cryptography` as a direct dependency for key generation, rather than SSH.
5. **The peer credential check on every existing socket** lands in phase 1, and each
   declared peer's directories are denied to local lines.
6. **Remote priority mail** is off by default per peer, and when on follows the limits
   for lines, with the whole peer counted as one sender.
7. **The defaults.** 1,000 waiting messages per peer, a week to expiry, 60 messages per
   600 seconds inbound, a 65,536 byte body, and one outage note after an hour.

**Decided by the operator on 2026-10-28, on the review line's findings.**

8. **A token is the peer daemon's credential.** The receiver enforces `reach` itself, the
   token file is owner-only and denied to every sandboxed line, and the trust section
   states that any unsandboxed process of the same user can act as a peer.
9. **Unconfined engines.** Peers are refused while any line runs on an engine that
   cannot be confined, today Pi, Cursor or Antigravity, unless the project file sets
   `[federation] accept_unconfined = true`. The operator decided later on 2026-10-28
   that a Claude Code line without an enabled sandbox counts as unconfined too.
10. **Peer mail and the turn allowance.** Peer mail never counts toward a line's
    autonomous-turn allowance and never resets it, and each peer has its own turn
    allowance per line, so a peer cannot hold a local line.
11. **Waiting mail is bounded** per recipient and per peer, and refused with `full`.
12. **Expiry of mail that may have arrived** is recorded as unknown with
    `message_unconfirmed`, not returned.
13. **Envelope addresses** are checked against the grammar and refused with `malformed`.
14. **A conflicting duplicate** is refused with `conflict`.
15. **Outbound is default deny,** through the `remote` list.
16. **The TLS listener requires client authentication** before it reads any frame.

**Open, raised by the review line or by these amendments, and not yet decided.**

- One note per change of a peer's `hello` name, as proposed above.
- The bound on unauthenticated TLS handshakes, 10 seconds as proposed above, which
  phase 2 needs. Its count is no longer open, since decision 23 gives it to the
  listener, which counts a TLS connection as it counts one on `peers.sock`.

**Decided by the operator later on 2026-10-28.**

17. **Peer turns and cost.** Turns started by peer mail stay under the line's existing
    cost hold, with no cap of their own.
18. **A cap on `busy`.** A message ends as expired after 20 `busy` answers, under
    decision 12.
19. **No batching in version 1.** Each returned or unconfirmed message gives its line
    sender one note.
20. **The bound before `hello`.** 10 seconds and one line of 65,536 bytes, and the
    connection is closed otherwise.
21. **Ed25519 alone,** without the P-256 fallback, since both daemons are Dromon and use
    the same library.
22. **The idempotency set** is bounded by the expiry window, dropping keys older than
    the longest expiry.

**Decided by the operator later on 2026-10-28, from the review of the TLS transport.**

23. **The listener owns the connection cap.** The cap is counted where a connection is
    accepted, not in a transport, so phase 2's TLS connections fall under the same
    cap. Step 5 sets it at 16.
24. **Delivery is recorded only from the peer's answer.** **Fact, from that review.**
    The TLS client returns a writable socket before the server has accepted the
    client's certificate, so a write that succeeds says nothing about delivery. The
    forwarder records a message as delivered only from an `accepted` answer to its
    `deliver`, read after an accepted answer to `hello`, on the Unix domain socket
    transport as well. A link that takes the writes and never answers leaves the
    message waiting, marked as possibly sent once a `deliver` was written.
