# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A read-only view of every other line in `line_status`. Each entry of `other_lines`
  now also gives the line's `condition`, the short `hold_cause` of a hold, `since`, when
  its state, hold or hibernation last changed, and its `unread` count with
  `oldest_unread_age_seconds`, and never a message's body, sender or identifier, nor
  the hold question. An entry gives `line_id`, `state` and `condition` always and
  every other field only when it says something, and no longer gives `held`, since
  `hold_cause` is given exactly when the line is held. A view too large for the
  response bound keeps as many lines as fit, with `other_lines_omitted` counting the
  rest, where before it vanished at about seventeen other lines. The lines it keeps
  are first those held, failed or with unread mail, then the rest, so a held line
  whose name sorts last is not the one dropped. The state keeps
  each line's unread mail and open questions as indexes, so `condition` and the view
  scan no messages, and the view is built only when the lists are asked for. A line had held back work it planned for a peer because its
  status gave no reliable view of the peer. `line_held` takes an optional `cause`,
  left out of the wire when absent, so an older log still decodes and an older hold
  shows `unrecorded`. The times are the event log's `recorded_at`, kept beside the
  state as the log is replayed and appended, so they survive a restart. **Decided by
  core, for review.** `unread` here counts mail still waiting for delivery, since a
  held line's mail waits undelivered. **Rollback hazard.** A daemon before this change
  refuses a log holding a `line_held` with a cause. See
  [`LINE_CONFIG.md`](docs/protocol/LINE_CONFIG.md).
- A restart preflight, round 13. `dromon restart` now reads `status`, then runs the
  installed daemon's own start checks in a process of their own, `dromond --check`
  with the running daemon's command-line options, against the project file and a copy
  of the event log in a private directory under the
  runtime directory, and refuses the restart with the new code's own message while
  the old daemon keeps serving. The check never writes the live log, takes no lock,
  makes and removes no socket, and starts nothing. `status` gives the new field
  `options`. `docs/protocol/RESTART.md` tables what the check covers and what only a
  real start reveals, which includes anything recorded during the drain. It replaces
  the import check by `--version`. Prompted by the restart of 2026-10-31 that left no
  daemon for about eight minutes. After audit's reviews, the copy is not a SQLite
  backup, which starts again at every write to the source, but the exact schema and
  the events up to the sequence number read when it begins, copied in ranges of at
  most 2,000 events and about 1 MiB with a short pause and no lock between them, so
  it finishes on a busy log and delays the daemon's commits by at most the reading
  of one range. The live log is attached read-only, which a test pins. Preflight
  directories that a killed check left are swept after a client's timeout, by each
  check when older than its bound, and by each start.
- `dromon land WORKTREE`, round 13, one operator command that takes a line's finished
  worktree to a pull request. It shows the plan with every path to stage, refuses
  protected paths under `docs/process`, `docs/roadmap`, `secret` and `.dromon`
  except those a resolved merge carries unchanged from the incoming commit, screens
  the diff, message, title and body against the disclosure list by file and line
  only, then asks, stages, commits with the operator's message, runs the full gate,
  asks, pushes without force, asks, and opens the pull request with `gh`. `--dry-run`
  changes nothing, and `--yes` answers every question. It never merges and never adds
  a `Co-Authored-By` trailer, and refuses a commit that a hook gave one. After
  audit's review, it also screens every commit the push carries since the branch
  left the remote base, their messages and the branch name, refuses an empty list or
  a missing `--terms` file, lets only a merge of the published base carry a protected
  path, re-screens the stored message after hooks, and asks before running a branch
  that changes `scripts/`, even with `--yes`. A preflight writes nothing of the
  operator's, so it neither makes the fleet index nor writes an entry in it.
- The operator's fleet, round 13, as `docs/architecture/FLEET.md` specifies. Every
  daemon, whatever its federation mode, writes an entry in a per-user fleet index at
  start and removes it at stop. The entry holds the project root, its operator socket,
  the pid, the start time and the daemon's commit. The index is the directory `index`
  inside the open-federation registry, so `DROMON_REGISTRY_DIR` moves it as well, with
  mode 0700 and entries of mode 0600. An entry's name must be the last step of its
  root. The daemon makes the index at start, before any engine is built, and every line
  of every project is denied it. A daemon whose entry cannot be written starts unlisted
  and says so. `dromon fleet list`,
  `questions`, `mail`, `issues`, `answer`, `send`, `read`, `status` and `restart` read
  the index and send the existing operations to each daemon's own operator socket,
  after `status` has shown that the daemon answering is the one the entry names, and
  take `PROJECT:ID` or `PROJECT:LINE`. `fleet restart` takes as the successor only the
  daemon the index then lists for the same root, live and saying that pid and root. A
  stale or unknown entry is shown and never asked, and a daemon that does not answer is
  reported while the rest go on. The attached client's `/projects` lists the index,
  with the root beside a name two projects share, and `/project PROJECT[:LINE]` moves
  to another project without restarting. A per-user hosts file,
  `.config/dromon/hosts.toml` or `DROMON_HOSTS_FILE`, names other machines, which
  `dromon fleet hosts` lists. It holds no credential. Reaching those machines through
  the operator's own SSH is designed and not implemented. **Rollback hazard.** An
  older daemon writes no entry, so `dromon fleet` does not see it, and an older client
  has no `fleet` command. The index directory an older release leaves is harmless and
  may be removed. **Operational note.** The daemon now makes the index at start, so a
  Grok line's turns are refused, naming the entry to add, until its profile denies the
  index, as for every protected directory.
- Runtime line creation accepts limits, kept session and compaction options.
  Configuration accepts workdir access and the project summary shows operator routing,
  relays, supervised runs, read only paths and federation policy.
- Disclosure refusals name message fields. Checks return only aggregate counts,
  without file names or line locations.
  Older daemons refuse the new `disclosure_accepted` key, so remove that table
  before rolling back. Operator accepted occurrences use file paths and exact line
  content hashes shared by the daemon and gate. Text and messages retain disclosure checks and the hourly limit.
- The gate reports bounded groups of skipped and deselected tests with their reasons.

- Open federation, round 12. `[federation] local` and `remote` take `allowlist`, the
  default, which keeps the behaviour of declared peers exactly, or `open`. With
  `local = "open"`, `listen` and a unique `name`, a daemon registers in a per-user
  registry, `.local/state/dromon/registry` under its user's home, which must have mode
  0700, and removes its entry at stop. `DROMON_REGISTRY_DIR`, an absolute path, moves
  the registry, and every test sets it to a temporary directory, so that no test
  writes the user's real one. A malformed value refuses the start rather than falling
  back to the default. A `hello` without a token is then named by the
  live registry entry of the announced name, whose pid and start time must match the
  connecting process, and a line may address `NAME:PARTY` for a registered project
  that the file does not declare. Every undeclared open peer takes the new
  `[federation.open]` table, whose lists are empty by default, so it delivers nothing
  until the operator names who may reach and send. The registry and every registered
  project's directories are denied to the project's lines, read again at each engine
  build. `allowed_networks` closes a TLS connection from outside its ranges before
  the handshake. With `remote = "open"`, the TLS listener accepts a key no declared
  peer pins, which reaches only `remote_reach` with stricter limits, and mail to
  another machine still needs a declared peer. The unconfined-engine rule applies
  while either mode is open. **Rollback hazard.** A daemon before this change cannot
  read a log holding `peer_opened`, and refuses a project file holding the new keys.
- The dependency `pyopenssl`, pinned exactly at 26.4.0 as `cryptography` is, for the
  listener of `remote = "open"` alone. Python's `ssl` refuses a self-signed client
  certificate it does not already trust and offers no callback to decide otherwise,
  and the verify callback of pyOpenSSL lets the listener accept any self-signed
  Ed25519 key over TLS 1.3 and refuse every other. It runs over memory buffers on the
  event loop, so a connection holds no thread, and its handshake is bounded by the
  10-second deadline and counted under the listener's bound of eight connections.
  The allowlist listener and every outbound connection stay on the standard library.
  **Rollback hazard.** The daemon imports it at start, so an environment without it
  cannot start any daemon of this version, open or not.
- The operator-granted capability `ViewImage`, so a line can look at a screenshot
  through whichever engine owns it. A line must name it among its `builtin_tools`, and
  without it no line can view an image on any engine. On Codex it enables `view_image`,
  which was always disabled, and Dromon checks the path of each `imageView` item. On
  Grok it permits `read_file`, the only tool that shows a local image, narrowed to
  files that pass the image check. On Claude Code it offers `Read`, which the gate allows only for files that pass the image check. The
  files must be png, jpg, jpeg, gif or webp by name and wholly one complete container of
  that type, checked over the whole file, inside the
  line's working directory and outside `.dromon`, `.git` and `secret`, and at most
  `imageaccess.MAX_IMAGE_BYTES`, 5 MiB. Image generation and editing stay refused on
  every engine. Pi, Cursor and Antigravity refuse the name, as they refuse every
  built-in tool. Review of 1045994. The check reads the whole file and requires a
  complete container, so a file that only begins as an image is refused, a Grok
  `read_file` whose first notice carries no input is refused for a line with `ViewImage`
  and without `Read`, and a Codex refusal decided before the turn is named is sent as an
  interrupt once it is, for a notice about this line's thread only. The check proves
  a complete container and not that its data is a picture, so a text chunk or compressed
  data can still carry chosen bytes, which `LINE_CONFIG.md` states and a test pins.
  ViewImage is not to be enabled on a Grok line until the first read_file notice is
  shown to carry its input. Nothing was run live, and each engine document lists the live steps
  the operator runs first.
- Supervised runs, task of 2026-10-31. The project file's `[runs.commands]` table
  declares commands, each an array, and the line tools `start_run`, `run_status` and
  `stop_run` start one as a run that outlives the turn, the seat and a daemon restart.
  A supervisor, `python -m dromon.runs`, runs it in a session of its own and writes a
  receipt when it ends. The event log records `run_started`, with the supervisor's
  process identifier and its start time from the process table, and `run_ended`, with
  the receipt and the output's digest. The daemon reconciles open runs on its process
  worker at start and at each rest, so a run that ended while the daemon was down is
  recorded, a supervisor that died without a receipt ends as `lost`, and a process
  identifier now held by another process is not taken for the supervisor. A run
  belongs to its role, only the owner starts or stops one, and `start_run` names the
  latest run as `previous`, so a successor seat reads its predecessor's record before
  starting another. A declared command runs with the daemon's authority outside every
  sandbox, which `docs/architecture/RUNS.md` states as a trust decision for the
  operator. **Rollback hazard.** A daemon older than this cannot read a log holding
  `run_started`, `run_ended` or `run_cleared`, and does not know of a supervisor still
  running.
- Lost runs block their scope, review of 2026-10-31, R2. A lost run's command may
  still run, so its scope starts no other run until reconciliation finds no process in
  the supervisor's process group or session, or an operator clears it with
  `dromon run clear RUN`, the new operator-only `run_clear` operation. Either is
  recorded as `run_cleared`, with the operator who cleared it. A process table that
  cannot be read keeps the run blocking.
- Project configuration may name relative `read_only_paths` that must exist inside each
  line's resolved workdir. Claude Code, Codex and Grok Build lines enforce those paths
  through their engine restrictions; Pi, Cursor Agent and Antigravity lines refuse the
  configuration because they cannot enforce it. Symbolic links resolving outside the
  workdir and missing paths are refused. The list defaults to empty. The project
  configuration example names `docs/process` and `docs/roadmap` as suggested protected
  paths, but this change does not alter `.dromon/dromon.toml`.
- `dromon wait --replies-to MESSAGE --timeout SECONDS [--from LINE]` polls the existing
  replies query in the client, with each request bounded by the remaining timeout. It
  returns the reply without marking it read, and reports timeout, invalid source and
  daemon loss as failures.
- The daemon relays operators' mail. The project file's `[relays.NAME]` tables declare
  command and file relays, and `[operators.ID] relay = NAME` routes an operator's mail
  to one. A relayer task forwards every undelivered message to a routed operator, off
  the event loop, records `message_delivered` only when the relay accepts it, and
  leaves failed and unrouted mail undelivered, retrying a failure after a backoff of 5
  to 300 seconds. A command relay that times out is killed with its process group. A
  reload changes the routes from the next pass. Relay delivery marks nothing read, so
  `dromon mail --unread` still lists a relayed message, and a message a client lists
  first is not relayed. `dromon status` shows the relays and each failing notice.
- A file relay, `kind = "file"`, appends each notice as one JSON line to a file
  created with mode `0600`, for `tail -f`. It refuses a symbolic link, another user's
  file and a file open to group or others, and a notice file in any line's working
  directory outside the directories Dromon protects, at start, at reload and before
  each write.
- Issues from lines. `send_message` with `issue` true raises an issue with an
  operator, recorded as a message of the new kind `issue` and relayed like any mail.
  It is open until the operator replies to it or acknowledges it, and
  `dromon mail --issues`, the `mail` query's `issues_only`, lists the open ones. An
  external agent is not offered the argument. **Rollback hazard.** A daemon older than
  this change refuses an event log that holds an issue, since its codec knows only the
  kinds `note` and `question`, so a rollback past it needs a log from before the first
  issue. The `[relays]` and `[operators]` tables are likewise refused by an older
  daemon as unknown keys, which stops its start rather than its log.
- `dromon role create`, `dromon role update` and `dromon role delete`, with the client
  operations `role_create`, `role_update` and `role_delete`, so an operator changes the
  team while the daemon runs. Update adds, removes and reorders seats and changes any
  term. Delete unbinds every seat and retires the role, whose seats stay lines with
  their leases, whose journal stays readable, and whose identifier is never declared
  again. The owner's seat is never unbound and an owned role is never retired, and the
  refusal says to vacate or hand over first. A seat of an owned role that could write
  its worktree is not unbound while it runs, since it would then write beside the
  owner. No line is created or reconfigured, and no line or external agent can call
  these. `dromon role status` shows every term, who holds the role and the retired
  roles. The new events `seat_unbound`, `role_retired` and `role_held` are rollback
  hazards, since an older daemon cannot read a log that holds one. A run-time change
  wins over the project file, which a start or a reload no longer applies to that role
  until its table agrees, and every role the file leaves is reported in the reload's
  `role_notices`, which `dromon reload` prints, and on a start's standard error. The
  rule and its cases are in `docs/architecture/ROLES.md` and
  `docs/protocol/PROJECT_CONFIG.md`. Required by the operator in round 10.
- Claude Sonnet 5.5 drivers on Claude Code for every strategy, as
  `claude-sonnet-5-5`, with the Opus 5.5 drivers' settings.
- `daemon_behind_worktree` in `line_status`, and a notice in the wake prompt once per
  worktree commit, when the daemon runs code older than the line's worktree, which is
  when its loaded commit is an ancestor of the worktree's `HEAD` and the two differ
  under its package directory. Asked by review in round 8. It is `null` for a
  worktree that does not descend from the daemon's commit, as on an unrelated branch,
  where it had said `false`, as the round 9 review, F5, required. The notice is told
  once again after a daemon restart, since the record of it is held in memory.
- The client request `replies` and `dromon mail --replies-to MESSAGE`, which give the
  replies in an operator's mailbox to one message with their bodies and mark none
  read, so a wait loop can count unread replies and read them in one query. Reported
  by another project in round 9. It delivers the operator's mail as `mail` does, so it
  appends `message_delivered` events. Every entry's fields count against its byte
  budget with the bodies, so an answer fits one protocol line whatever its fields
  hold, and entries that do not fit are counted in `left_out` for a later page, as the
  round 9 review, F11 and F13, required.
- Drivers for GPT-6.1-Sol and GPT-6.1 Terra on Codex, `gpt-6.1-sol` and
  `gpt-6.1-terra`, for every strategy, with a context window of 500,000 tokens from
  Codex's model catalogue. Required by the operator on 2026-10-30. No turn on either
  model has run through Dromon yet.
- `check_disclosure` accepts a list of up to eight files in one check. It counts
  matching lines independently in each file and sums the counts, so a match cannot
  span a file boundary. It limits the combined input to 1 MiB and spends one hourly
  check for the call. The tool states that it checks only the disclosure list
  configured for the project.
- A line's own notes, such as fired wakes, task-end notes and compaction requests, are
  no longer counted as unread in any view, and are counted apart as `own_notes`, so
  nothing is hidden. One rule in `dromon.mailbox.summary` now gives the same unread,
  unanswered and own-note counts to the wake prompt, `line_status`, `list_mail`, whose
  first line now carries them, and the client's `mail` result and line entries, which
  gain `own_notes`, and the line entries also `unread` and `unanswered`. Mail from an
  operator, another line, an agent, a peer or the harness always counts. A prompt that
  delivers an own note does not mark it read, which is decided for review. The
  `MailSummary` field `collapsed` became `own_notes`, and the prompt's sentence now
  says the notes are counted apart. Required by the operator on 2026-10-30, from the
  review line's round 5 report. From the round 6 review, `list_mail` with
  `unread_only` lists exactly what `unread` counts, a new `own_notes_only` filter
  lists exactly what `own_notes` counts, and an own note leaves `own_notes` once it
  is acknowledged or replied to as well as once it is read.
- A turn that leads with the standing brief adds one sentence when an operator has
  mailed the line since the brief was set, naming both times, so a stale brief is
  read against that mail. The state keeps the newest such message as
  `mail_since_brief`, from the order of events. Required by the operator on
  2026-10-30. From the round 6 review, `line_status` gives the same fact under its
  brief as `operator_mail_since`, and a time the event log does not give is looked
  up once only.
- Operator line configuration may set `fresh_session_each_turn` for Claude Code or
  Codex. Each turn starts without the prior engine session, context handoff or standing
  brief, while the mail that starts the turn is still delivered. The scheduler records
  each reset as `line_session_cleared`, which survives restart. The key implies
  `context_handoff = false`; loading refuses it with `keep_session = true`, explicit
  handoff enabled, or an unsupported engine adapter.
- Git over the Secure Shell protocol for chosen lines, off by default. The project
  file's `[git_ssh]` table names one agent socket, the daemon's own `SSH_AUTH_SOCK` or
  an explicit socket such as that of a deploy-key agent, a `known_hosts` file that must
  pin `github.com`, and the public key ssh offers. A line or a profile with
  `git_ssh = true` gets a `GIT_SSH_COMMAND` with `-F /dev/null`, the agent, that key
  alone, strict host key checking against that file and batch mode, on every engine
  from one function, and on Claude Code with an enabled sandbox, the socket and
  `github.com` allowed and both files denied to writes. Never a key file, and never
  the operator's home. The daemon and `dromon config` refuse a missing or foreign
  socket, a socket in a directory another user may write, an inherited agent without
  `SSH_AUTH_SOCK`, and a `known_hosts` without a well-formed `github.com` key of an
  accepted type, and a reload may not change the option. Each key is parsed as an
  SSH public key, whose own type must match its line's, and a host line with any
  negated pattern pins nothing. The daemon records the SHA-256 digest of both files
  at start and checks them before each turn of a line given the option, and a change
  refuses the turn, holds the line and asks an operator, naming the file and never
  its contents. That detects tampering at a turn's start and does not prevent it.
  Observed on 2026-10-29, ssh did not leave an enabled Claude Code sandbox, so the
  option is inferred to work only for a line whose sandbox is disabled. Required by
  the operator on 2026-10-29, with review findings M1, M2, L1, L2, L3 and L5 of the
  same day.
- Request keys are durable. A keyed call of `send_message`, `ask_operator` or
  `wake_after` records its key and the digest of its other arguments in the
  `message_sent` or `wake_scheduled` event of its act, so a retry that crosses a daemon
  restart repeats the first answer instead of sending a second message, asking a
  second question or moving the wake. The key is recorded in the same event as the
  act, the bound of 256 keys per party and tool holds after replay, and the field is
  left out of events without a key, so older logs still load. A repeated `wake_after`
  no longer names the wake the first call replaced. Asked for by the review line's
  report of round 4. **Rollback.** A daemon built before this change cannot decode a
  keyed event, so a log that holds one needs this daemon or a later one. Review r5,
  finding K1.

- Compaction now withdraws a still-pending wake scheduled by `compact_session` whether
  the engine made the compaction itself or a turn made it at Dromon's request, and a
  compaction that reports no size withdraws the daemon's own pending context-warning
  wake, which until now fired as a reminder after the compaction. A
  disclosure refusal names the field that contains a listed term when the call offered
  text in that one field only, never the term or its position, and keeps the shared
  hourly limit. A call with several fields gets the generic refusal, so a refusal still
  answers one bit, per review r4 finding F1. The prompt states
  that its autonomous-turn count is the consecutive count without operator mail.
- A role transition package whose former owner wrote no journal entry and set no
  brief no longer leaves every field unknown. Gate and coverage is filled from the
  former owner's latest task record and Task from the latest operator mail delivered
  to it, cut to 600 bytes, each marked observed by the daemon with its source. Mail from
  a line never fills a field, and each value is checked against the disclosure list.
  Asked for by the engines report of round 4.
- A turn whose line has no recorded turn-end commit is told that the moved-worktree
  check was skipped, once, with the commit checked out now. Asked for by the core
  report of round 4.

- A standing brief for every line. `set_brief` records the line's current task, open
  decisions, artifact paths and next action as one `brief_set` event, which replaces
  its previous brief and which the daemon stamps with the time and both commits. Each
  field and the whole are bounded, an unknown key is refused, and the text is checked
  against the disclosure list. The brief leads the line's first turn after its engine
  session is new, as after a compaction, a restart or a rebind, and `line_status`
  shows it, always delimited as untrusted data. A turn that ends without a report,
  as one that fails before its engine receives the prompt, leaves the lead owed to
  the next turn. Asked for by three of the round 3 reports.
- `wake_after` may name one of the line's running background tasks. Dromon records a
  task binding on the scheduled wake, and the matching `task_noted` event withdraws it
  atomically while it is still scheduled. A wake that has fired already cannot be
  withdrawn. Logs containing `line_turn_ended` or a task-bound `wake_scheduled` event
  require this daemon or a later one.
- A line can reopen its own sent mail with `read_mail`. Reading outgoing mail does not
  mark it delivered or read by its recipient, and a line cannot open mail between other
  parties.
- The event log records the worktree commit observed at each line turn's end. The moved
  worktree notice can therefore compare against the last turn across a daemon restart.
- The `daemon` object of `line_status` gives `loaded_at`, the time the daemon loaded
  its code, and the stale note and the `cite` sentence name it, so a report that cites
  a stale daemon says since when. Asked by the review role on 2026-10-28.
- Generated federation certificates now name their SubjectPublicKeyInfo digest in the
  certificate subject and include a subject key identifier. TLS contexts can trust a
  bounded set of peer certificates while authentication remains based on configured
  public-key pins.
- Claude Code turns that start after a background task ends now run under the
  scheduler's normal admission, tool checks, autonomous-turn count and cost hold. A
  self-start that cannot be admitted stays interrupted and its task note wakes a later
  turn. Task ends reported during a turn and ends of tasks the line stopped are
  recorded without creating another wake.
- Phase 2 of federation, step 1. A `[peers.NAME]` table may declare `transport =
  "tls"` with the peer's `address`, `pin_sha256` and `certificate`, and
  `[federation]` may name `tls_listen`, which is required exactly when a `tls` peer is
  inbound. Both are validated and recorded with `peer_declared`. A `unix` peer's recorded
  policy is unchanged, so an upgrade declares no peer again.
- Phase 2 of federation, step 2. `dromon peer key` makes the daemon's Ed25519
  identity once in `.dromon/peers` and prints its pin and the path of its public
  certificate, which the operator gives the peer's operator. It never replaces an
  existing identity and never returns the private key.
- Phase 2 of federation, step 3. `dromon.tls_link` reaches a `tls` peer at its
  `address` over the engines role's pinned TLS transport, presenting the daemon's
  identity and trusting only the peer's certificate and pin, behind the forwarder's
  connector seam. `hello` over TLS carries no token. The daemon's connector follows
  each peer's transport.
- Phase 2 of federation, step 4. The daemon listens on `federation.tls_listen`,
  counting each connection under the peer connection cap of 16 before its handshake,
  which must finish within 10 seconds and present the key of an inbound `tls` peer by
  its pin before any frame is read. That pin names the peer at `hello`, which takes no
  token over TLS, and again at each `deliver`.
- Several `tls` peers may be inbound, up to the transport's bound of 64 certificates
  one listener trusts, now that each certificate names its own key. The limit of one
  is lifted. Two identities made before that change share the old subject name, so one
  of them must be made again with `dromon peer key` before one listener trusts both.
- Phase 2 of federation, step 5. Mail to a `tls` peer is no longer refused at send.
  The policy is the same on both transports, which decide only how mail travels.
- `dromon agent send --agent NAME --to PARTY BODY` sends one note as a registered
  external agent through the agent socket, with the token from `DROMON_AGENT_TOKEN`, so
  a service that is not an MCP client can tell a line that its turn has come. A test
  shows that such a note wakes an idle line for exactly one turn, which counts as
  autonomous.
- A line's configuration may list its `recipients`, the parties it may send mail to,
  enforced by the daemon. Without the key every party is allowed, as before. A line may
  always send itself a note, and its questions to an operator are not limited, so an
  operator can seat several lines at an external task with no private channel between
  them.
- An external agent's note to a line may carry a `correlation` key naming the agent's
  own task. The event log records it as `message_correlated`, and records each turn
  that delivered keyed notes as `turn_correlated`, with its cost and how many keys it
  carried, so the turns and cost of one external task are totalled under its key.
  `dromon status --correlation KEY`, the agent's `dromon_status` and `dromon agent
  send --correlation KEY` show and carry them. A turn under two keys counts its whole
  cost under each, and is counted as shared.
- When a line's turn ends with background tasks of its kept session still running and
  the line sent no mail during it, the line's next prompt says so once, so that work
  going on unannounced is noticed.
- When the commit checked out in a line's worktree moved since its last turn ended, as
  after an operator commits or merges its work, the line's next prompt names the
  commits before and after and the `git log` range between them.
- A line's prompt no longer counts as unread its own notes, such as fired wakes and
  task-end notes, that an earlier turn was shown, and says how many it left out.
- A line's `check_disclosure` tool checks a text, or a file in its working directory,
  against the project's disclosure list and answers pass or fail with how many lines
  hold a listed term, never the term. Each check is recorded as `disclosure_checked`,
  and a line may check at most 10 times in an hour, since each answer could otherwise
  test candidate words one by one. A line tool's refusal for disclosure counts against
  the same limit, and when the limit is spent the line is held and an operator asked,
  with every such tool answering alike until the line is released.
- Every background task end a kept engine reports between turns is recorded as
  `task_recorded`, with its command, status, exit code, output file, the SHA-256 digest
  and size of its output and the worktree commit, and `role_status` lists each seat's
  latest three, so a reviewing seat can check a gate without running it again.
- The design of federation between Dromon daemons, `docs/architecture/FEDERATION.md`,
  approved by the operator on 2026-10-28. A line or an operator in one project can
  message and wake a party in another, addressed as `keleusma:kaizen`, with the peer
  named by local configuration. Mail from a peer is untrusted data and never operator
  authority, peers are default deny per direction, and each daemon's disclosure list
  applies to mail leaving it, operator mail included. Phase 1 is the same machine over
  Unix domain sockets with a peer credential check and a token per peer. Phase 2 is
  different machines over Transport Layer Security version 1.3 with pinned keys.
  Amended the same day with the operator's decisions on the review. The receiver
  enforces which local parties a peer may reach, peers are refused while a line runs
  unconfined unless the project accepts it, peer mail neither counts toward nor resets
  a line's autonomous turns, waiting mail is bounded, mail that expires after it may
  have arrived is recorded as unconfirmed, and outbound mail is default deny.
- Every socket the daemon serves, the operator socket, the line socket and the agent
  socket, refuses a connection from a process of another user, read from the peer's
  credentials, `LOCAL_PEERCRED` on macOS and `SO_PEERCRED` on Linux. A connection
  whose credentials cannot be read is refused too, and a platform with neither
  interface refuses to start the daemon. This is step 1 of federation phase 1, and
  closes debt that `CLIENT.md` recorded.
- The project file's `[federation]` and `[peers]` tables, validated strictly, with
  both directions of a peer off and every list empty by default. Each declared peer's
  runtime directory, and its `.dromon` when its project is named, are protected from
  local lines as the project's own are, through the Claude Code sandbox, the Codex
  profile and the Grok profile check. While peers are declared, a line on Pi, Cursor
  Agent or Antigravity is refused unless `federation.accept_unconfined` is true. A
  reload that changes either table needs a restart. This is step 2 of federation
  phase 1, and peer mail itself comes in later steps.
- The peer table in the event log and the peers' tokens, step 3 of federation phase
  1. The daemon records each declared peer at start with `peer_declared`, and a peer
  no longer declared with `peer_removed`, and `dromon peer token`, `revoke`, `store`
  and `list` issue, revoke and store the tokens peers present, keeping only the digest
  of a token issued and the token a peer issued in `.dromon/peers`, mode 0600 in a
  directory of mode 0700. The daemon refuses to start when the store is wider. While
  peers are declared, a Claude Code line without an enabled sandbox is refused as an
  unconfined line, by the operator's decision of 2026-10-28.
- Mail from peers, step 4 of federation phase 1. The remote address `peer:party` in the
  model and the codec, the `message_received` event with its idempotency key, and
  `peers.sock`, opened when `federation.listen` is true, which serves only `hello` and
  `deliver`. A peer is named by its token alone. Each delivery is checked against the
  grammar, the rate limit of 60 in 600 seconds, the peer's `inbound`, `reach` and
  `max_body_bytes`, the reply route and the waiting bounds, and answered `accepted`,
  `refused` with a fixed code or `busy`. A resend is a duplicate or a conflict. Peer
  mail neither counts toward a line's autonomous-turn allowance nor resets it, and each
  peer may start at most `turns_per_hour` turns for one line in any hour. Mail to a
  peer comes with the outbox of step 5.
- Mail to peers, step 5 of federation phase 1. A line, an agent or an operator may send
  a note to a remote address that the outbound policy allows, checked at send against
  `outbound`, `senders`, `remote`, `max_body_bytes` and the disclosure list, which now
  holds an operator's mail to a peer too. The note waits in its peer's outbox, at most
  1,000, and a forwarder per peer delivers it in order over the peer's `peers.sock`
  with `hello` and `deliver`, behind a connector seam that the TLS transport of phase 2
  will fill. `accepted` records `message_delivered`, a refusal records the new
  `message_returned` with its fixed code, and a message that expires is returned with
  `expired` or, when a copy may have arrived, recorded by the new `message_unconfirmed`.
  A line sender gets a note for each. Twenty `busy` answers end a message as expired,
  and backoff runs from 5 seconds to 15 minutes. `list_sent` and `dromon sent` show
  `delivered to peer`, `returned` with its code, or `unconfirmed`. The operator's
  decisions of 2026-10-28 are built with it. A connection to `peers.sock` must send its
  `hello` within 10 seconds in one line of 64 KiB, and `message_received` carries
  `received_at`, so the received keys older than the longest expiry and a day are
  dropped. From the review of the TLS transport, the peer listener serves at most 16
  connections at once, and a message is recorded as delivered only from the peer's
  answer, never from a write that succeeded.
- A watch on the forwarder, after step 5 of federation phase 1. An unexpected exception
  in a peer's forwarder is written to standard error with its traceback, kept until a
  message is accepted again, and followed by backoff, so the forwarder keeps running.
  The daemon also says at once when its forwarding task ends with an error or before a
  stop, and such an end no longer keeps the sockets open at shutdown.
- Federation phase 1 step 6, priority mail to and from peers and peer state. A sender
  may mark mail to a peer's line priority when the peer's `priority_out` is true, with a
  line's priority mail counted toward its own limit, and the envelope asks for it. The
  receiver honours it only under its `priority_in`, counting the whole peer as one
  sender and keeping each line's limit, and otherwise records ordinary mail whose
  header says that its priority was not honoured. `message_received` gains
  `priority_asked`, and `message_prioritized` may mark mail waiting for a peer's line.
  `status`, `dromon status` and `line_status` show each peer's state, what waits and
  since when, and its last contact. When a peer's oldest message has waited an hour,
  the harness sends the default operator one note for that outage, which ends when
  the peer accepts mail again.
- A sender address for the harness itself, `!dromon`, at the operator's decision of
  2026-10-28. No client, line, agent or peer input can name it, and only the daemon's
  own log reads it, as the sender of a `message_sent` note to a line or an operator.
  The outage note, the failover notes to the operator and to the new owner, the note
  to a successor of a handoff, and the request to prepare a handoff move to it, since
  they were recorded as from a line that did not write them. Nothing replies to it,
  and its notes are not counted unanswered.
- The scheduler's watch, at the operator's instruction of 2026-10-28, as the
  forwarder has. An unexpected exception in a tick is reported with its traceback,
  kept as `scheduler.fault` in `status` until a tick completes, and followed by a
  pause from 1 second to 1 minute, and a done callback on the scheduling task says at
  once when it ends early.
- Dromon's own Grok home. A Grok line runs with `GROK_HOME` at `~/.grok/dromon-home`,
  never the operator's Grok home, whose MCP servers and permission mode the first
  live probe showed Grok loads. Dromon writes the home's `config.toml`, which sets
  Grok's asking mode and declares no MCP server or hook, and its `sandbox.toml` with
  the `dromon-reader` and `dromon-writer` profiles, when either is missing. Before
  every turn it refuses any drift in that `config.toml`, a policy or trust file that
  sets anything, agents, hooks, plugins, rules, skills or workflows in the home, a
  profile key such as `read_write`, and a Claude Code settings file whose
  `defaultMode` is not `default`, since Grok reads that too. Grok runs with `HOME` set
  to `~/.grok/dromon-home/home` and the `XDG_` home variables removed, as an
  Antigravity line runs, so it cannot read the operator's Claude Code or Cursor files,
  and a turn is refused when that directory holds `.claude`, `.claude.json`,
  `.cursor`, `.agents` or `.grok`. A turn is also refused when the workspace or any
  level up to its repository's root holds a project `.grok` with anything in it,
  which could add servers, plugins, permission rules or sandbox profiles. It never
  writes, reads or copies a credential. The operator signs Grok in to the home once
  with the command in `docs/protocol/GROK.md`, which sets both `GROK_HOME` and
  `HOME`. A session that reports no mode then rests on these checks rather than being
  refused. Variables that would sign Grok in through another flow or send its
  requests elsewhere are refused.
- Per-line `workdir_access` supports `write` and `read`. Claude Code and Codex confine
  read-mode writes with their sandbox or permission profile. Pi, Cursor Agent and
  Antigravity refuse read mode, and Grok refuses it until a probe confirms its reader
  profile. Codex replaces a kept thread when the access mode changes. Capability and
  protocol documentation records the support matrix.
- Automatic switching of a role's owner, roles step 3, at the operator's instruction
  of 2026-10-27, as `docs/architecture/ROLES.md` describes. A role declares its seats'
  order, a failover policy, `automatic` by default or `ask`, and
  `handoff_below_percent`, 10 by default. When an owner is held at a plan limit or its
  provider's budget is stated at 0 percent, the harness passes the role to the next
  usable seat in the order, with the new cause `failover` and an entry saying why,
  wakes the new owner with the recovery checklist, and tells the operator. A budget
  stated below the threshold asks the owner to prepare a handoff. A plan limit records
  its provider's budget as 0 percent, with the reset time an engine gave. The Claude
  Code adapter now counts a rejected rate-limit window on a failed turn, and a billing
  error on any turn, as a plan limit, read from the Agent SDK's types and not observed
  live. Codex keeps its inferred codes, and Grok has no known shape, so its failures
  hold the line as before. `role_status` and `dromon role` show the order, the policy
  and the next seat, and `dromon budget set` prints what a statement set off.
- Codex's usage limit as the operator observed it on 2026-10-27, as
  `docs/protocol/CODEX.md` records. A turn that fails after an `error` notice, or with
  a `turn.error`, whose `codexErrorInfo` is `usageLimitExceeded` in camel case, is a
  plan limit, which the snake-case codes alone had missed. Its reset time is read from
  the message's wording, in the daemon host's local zone, as an inference and an
  assumption, and recorded with the budget. An `account/rateLimits/updated` notice
  whose credits have `hasCredits` and `unlimited` both false, and whose `primary` and
  `secondary` windows are both null as observed, records the Codex budget at 0 at once,
  before any turn fails, so a role whose owner is between turns fails
  over then. Grok stays unclassified.

- A seat that does not own its role is not woken while another seat owns the role,
  unless its engine can make the role's worktree read only to it. Its mail waits, as a
  hibernated line's does. No engine counts as able to yet, because the engines role's
  read-only seats are on an unmerged branch, so until that merges a seat runs only
  while it owns its role or the role is vacant. `role_status`, `roles` and
  `dromon role` show each seat's access and whether it waits. This is default deny, as
  `docs/architecture/ROLES.md` requires.
- Roles with seats on several providers, approved by the operator on 2026-10-27, as
  `docs/architecture/ROLES.md` describes, first step. The events `role_declared`,
  `seat_bound`, `ownership_changed`, `role_noted` and `budget_stated` are in the model
  and the codec, with checks that a role has at most one owner, that the owner is a
  live seat that may own it, that a change of owner names the owner it replaces so two
  that race cannot both apply, and that a handoff rests on the previous owner's latest
  handoff entry. A seat has the `role_status` tool.
- The project file's `[roles.ID]` tables, with `workdir`, `seats`, `owner` and
  `handoff_seconds`, and a line's `may_own`, as `docs/protocol/PROJECT_CONFIG.md`
  specifies. The daemon records them at start and on reload, checked against the log
  before any event is appended, and records the file's `owner` only while the role
  has never had one. A seat without a `workdir` takes its role's. `reload_project`
  returns `roles`.
- The seat tools `record_role_note`, `relinquish_ownership` and `take_ownership`, as
  `docs/protocol/LINE_CONFIG.md` specifies. The daemon stamps each journal entry with
  the role worktree's commit and its own. A seat takes its role itself only on a ground
  the harness observes. A change of owner drops the former and the new owner's
  engines, awakens and notes a successor by handoff, and starts no turn as the owner
  until the former owner's turn has ended and its kept connection has closed. Each
  seat's next turn is told of a change it did not make, and a new owner by recovery is
  given the recovery checklist. `ownership_changed` gains `back_to`, for a short
  alternation between two seats.
- The operator's role and budget commands, as `docs/protocol/CLIENT.md` specifies.
  `dromon role status [ROLE]` and `dromon role entry ENTRY` show roles, seats, the
  last change of owner and the journal. `dromon role take ROLE --seat SEAT` and
  `dromon role vacate ROLE` change the owner with cause `operator`, refused while the
  owner has a turn in flight, and a seat given the role is awakened and woken by a note
  whose turn leads with the recovery checklist. `dromon role handoff ROLE --to SEAT`
  asks the owner in priority mail to hand the role over with the handoff note's
  template. `dromon budget set PROVIDER` records `budget_stated` from an operator, and
  `dromon budget` lists each provider's statement and whether it shows the provider
  spent. The operations are `roles`, `role_entry`, `role_take`, `role_vacate`,
  `role_handoff`, `budgets` and `budget_set`, and none is offered to external agents.
  The question to the operator when an owner has not relinquished within the role's
  `handoff_seconds` is not built yet.
- The operator's decisions of 2026-10-27 on the review of step 3, as
  `docs/architecture/ROLES.md` records. Only a plan limit read from a shape observed
  live, or typed by the Agent SDK, records a budget and may move a role. A limit read
  from Codex's inferred codes or free-text phrases holds the line and asks, records no
  budget, and grounds neither a failover nor a seat's own take. `relinquish_ownership`
  inside the owner's turn takes effect when that turn ends, stamped then, and a second
  call in the same turn is refused. Automatic failover on an engine's budget report is
  kept. A seat with a turn in flight may be chosen, and gains write access from its
  next turn. The confining engines stay empty until read-only seats merge, and are
  then derived from the capability table, with Grok out until a probe.
- Compact `line_status` responses by default, with bounded output and explicit requests
  for diagnostic fields and long lists. List responses are bounded too. Lines with
  context compaction enabled wake at their context warning so compaction can happen
  before the next real turn, except when held or hibernated. A new
  `propose_change` tool records a bounded patch, file list and message for operator
  review through `dromon proposal list` and `dromon proposal show`; its file list must
  match the patch headers and excludes protected paths. It never applies a patch or
  commits. `dromon config` warns when writing-capable lines share a workdir.
- Restart to upgrade, by the operator's decision of 2026-10-27, as
  `docs/protocol/RESTART.md` specifies.
  - `dromon restart [--interrupt] [--wait-seconds S] [--end-tasks]` and the client
    operation `restart`. The daemon drains, so no turn starts and no scheduled wake
    fires, and waits for running turns to end, or with `--interrupt` asks each to end
    at its next safe point, the point priority mail uses. A turn still running at the
    bound abandons the restart, and no turn is cut. Background tasks of kept sessions
    refuse it unless `--end-tasks` is given.
  - Once no turn runs, the daemon stops and starts its successor itself, from the
    installed code and with the same command-line options, after it has released its
    lock. Sessions, hibernations, scheduled wakes, holds and mail survive in the event log.
    `dromon restart` checks that the installed daemon imports before it asks, and
    prints the successor's commit and the commit before.
  - The event `restart_noted`, a note from a line to itself that a restart ended its
    turn, which wakes it in the successor with a header saying so.
  - The daemon's `status` gives `daemon`, with its commit, and `restart`.
  - `line_status` gives `commits`, the full commits of the line's worktree and of the
    daemon with a sentence that cites both, and each of `other_lines` gives its
    `workdir_commit`.

- Hibernation, priority mail and self mail, by the operator's decision of 2026-10-27, as
  `docs/protocol/PRIORITY.md` specifies.
  - `dromon hibernate LINE` and `dromon awaken LINE`, the client operations `hibernate` and
    `awaken`, and the events `line_hibernated` and `line_awakened`. A hibernated line starts no
    turn, by mail, self mail, a scheduled wake, a task note or an answer, and keeps
    its mail, session and pending scheduled wake, which fires once it is awakened. A
    turn in flight finishes. `line_status`, the daemon's `status` and `dromon status`
    show the hibernation, and `condition` is `hibernated`.
  - `dromon send --priority`, `priority` on `send`, and `priority` on `send_message`,
    recorded as `message_prioritized`. Priority mail ends the recipient's running turn
    at its next safe point, through the line's driver, which every adapter already
    honours, and leads the next turn, which skips the cooldown. Lines' priority mail
    is limited to 3 sent and 2 received per 10 minutes, counts a restart sets to zero,
    operators' is unlimited, and external agents may not send it.
  - A line may send itself a note with `send_message`, with no rate limit, since the
    autonomous-turn limit and the cost hold bound a chain of them. Operators and
    agents still may not.
  - `dromon status` prints one row per line.

- Grok, Codex and Antigravity capability tables, and probe recording for Codex and
  Grok, after the bounded-waits branch was merged.
  - Their tables are filled under the rule of finding C1. Codex, run live on
    2026-10-25, keeps sessions, as the luna and codexkept lines showed live on
    2026-10-26, reaches Dromon's tools and denies before it runs the tools it was not
    given and the tool classes the review names as observed, the rest by its
    permission profile's documented behaviour, so its isolation is full. Grok and Antigravity were never
    run for a turn, so their own tools are unknown. Grok's isolation is unknown and
    Antigravity's partial, now by its table rather than by name. No table names a
    minimum for them, and Codex's report says it was run live but that no version is
    read, until a recording shows where Codex reports one.
  - `dromon probe --record` records Codex and Grok, whose own process wrappers now
    record their frames and standard error as `dromon.jsonlines` does, through helpers
    the three share. A recorded Codex turn and a recorded Grok turn replay through the
    real adapters in tests.
  - A scrub replaces Codex's plan usage under `rateLimits` as it does Claude Code's
    `rate_limit_info`. The key's name is taken from Codex's protocol, not yet from a
    recording. The probe thread's own token counts are kept, as for Claude Code.
  - Fixes from the review of the rows. A raw recording is written under
    `.dromon/probes`, closed to every line, and `--scrub` reads a relative name from
    there and writes its copy to `tmp/probes`. The account scrub covers any key whose
    name has `account`, `org`, `organization`, `email` or `plan` as a word, keeping each
    value's type. Standard error is piped and read alike whether or not a probe
    records. A failed write to a recording is noted and the turn goes on, and the probe
    then exits with a failure. A recorded Codex turn replays through the real process
    wrapper.
- Fixes from the review of 2026-10-27.
  - Instruction paths are checked as a configuration arrives, from a client, an
    override or the project file. A relative path must resolve inside the project
    root, and every path must name a file that exists. A project file's refusal names
    the line, the key and the line of the file.
  - A turn whose prompt could not be composed fails as any turn does, holding the line
    and asking an operator, where the failure escaped the tick.
  - A scrub replaces every account value as a whole word whatever its length, keeps
    in `rate_limit_info` only placeholders for the fields the Agent SDK reads and
    replaces every other field by its type, and refuses a user name of any length or
    an address written in full-width characters. The probe turn's token counts and
    cost are kept, by the operator's decision. The fixture was made again.
  - A request key with arguments JSON cannot hold exactly is refused before the call
    acts.
  - A context handoff labels the line's own quoted messages as its earlier words,
    which may relay others' text and are not instructions.
  - Pi's table says `unknown` for its own tools, denial before a tool runs, and its
    sign-in check, since Pi was never run live, so a Pi line's isolation is unknown.
    The probe says plainly that no minimum version is enforced for Pi and Cursor
    Agent. A version is read only where its text holds one.
- A sign-in check without a turn, row 16 of the T3 Code review. `dromon probe
  --no-turn` checks the engine's sign-in and runs no turn, for each engine whose
  capability table says it offers a check, which none does yet. Pi's check, through
  `pi auth check`, was never run live, so it runs only with `--accept-unverified-check`
  and says its answer is unverified. Other engines are refused, naming what their
  table says. Dromon reads no credential store.
- Event log size and growth, row 14 of the T3 Code review. `dromon status`, the
  `status` operation and `line_status` show the log's size on disk, the events and
  bytes added since the daemon started, the hourly rates once a minute has passed, and
  that nothing compacts the log yet. A size that cannot be read is shown as unknown,
  never as zero.
- Context handoff, row 13 of the T3 Code review. The first turn in a new engine
  session, after `rebind_line`, `clear_session` or a change of `workdir`, begins with
  a deterministic selection of the line's recent mail, at most 20 messages in 4,000
  characters, quoting only the line's own and operators' messages. The new
  `context_handed_off` event records the selection by its message identifiers, length
  and digest once the engine ran the turn, so a turn that failed first carries the
  handoff again. A line opts out with `context_handoff: false`, which the event also
  records.
- Request keys and mail durability, row 11 of the T3 Code review. `send_message`,
  `ask_operator` and `wake_after` take an optional `request_key`, so a retried call
  answers as the first did and records nothing, and the same key with other arguments
  is refused. Keys are kept in memory, at most 256 for each party and tool. Tests show
  that mail arriving during a turn, including a failed turn and one cut short by a
  daemon stop, is announced next, once each and in the order sent, and is never marked
  read or acknowledged by the daemon.
- Engine capabilities as data, row 10 of the T3 Code review. One table per engine in
  `dromon.capabilities`, with `yes`, `no` or `unknown` for each capability and the
  tested minimum version, filled for Claude Code, Pi and Cursor Agent and pending for
  Grok, Codex and Antigravity. The kept-session refusals of Pi and Cursor, each turn's
  isolation, the probe's partial-isolation engines and its report are derived from
  them. A Claude Code report without its list of tools is unknown and refuses the turn,
  where it was read as empty.
- Honest cost and posture, row 8 of the T3 Code review, in the parts outside the Grok,
  Codex and Antigravity adapters. `line_status`, `status` and the curses client keep a
  cost that turns did not report apart from zero, with an estimate that is exact, a
  lower bound or unknown, and each recent turn names its credential, credential
  source, sandbox and isolation, with `null` where the engine did not say.
- Probe evidence and minimum versions, row 7 of the T3 Code review. `dromon probe
  --record` writes every frame the adapter exchanged to a new owner-only file under
  `tmp/probes`, and `--record-stderr` adds the engine's standard error, bounded and
  masked. Claude Code, Pi, Cursor Agent and Antigravity record, and Codex and Grok are
  refused until their adapters do. `dromon.evidence` replays a recording through the
  real adapter, keeping the order of the exchange and failing on divergence rather
  than hanging. The Claude Code adapter refuses an engine below version 2.1.310, the
  one it was run against live, and each engine's minimum is in the new
  `dromon.capabilities`, as `docs/protocol/ENGINES.md` specifies.
- Scrubbed fixtures. `dromon probe --scrub FILE` writes a copy of a recording with
  account values, the home directory, session identifiers, universally unique
  identifiers and the account's rate limit usage replaced by stable placeholders, so it
  still replays, and refuses a copy
  that still holds the home directory, the user name, an email address or an account
  value. The first live Claude Code probe, scrubbed, is the fixture
  `tests/fixtures/probes/claude-code-haiku-2026-10-27.jsonl`, which a test replays
  through the real adapter.
- A failed turn reaches an operator. A turn that fails, by an error, a refusal before
  it began, or an engine's report of any subtype but `success`, holds its line with an
  operator question quoting how it ended, unless the line mailed an operator during
  that turn. Before, such a failure was recorded only in the line's activity log, and
  an operator lost hours to one on 2026-10-27.
- Bounded engine waits and Codex subscription-limit handling. Grok has separate
  initialization and turn deadlines. Antigravity has a deadline through its first
  model output, with the stalled stage named. Codex fails pending requests after a
  bounded output-close wait, pages background terminal listings, treats unfinished
  paging as unknown, reports inherited thread usage correctly and holds a line after
  a plan limit with its reported reset time.
- Grok Build 1.0.70 help was read on 2026-10-26. It exposes `--always-approve`, but
  neither `grok agent --help` nor `grok agent stdio --help` exposes a permission-mode
  flag. Dromon does not pass an unsupported option and continues to set and verify the
  session's default mode through ACP.
- Kept Codex sessions preserve unknown terminal state after a failed or oversized
  listing, and a plan limit from a listing now reaches the scheduler's hold path. Plan
  classification prefers inferred structured error codes and rejects an unrelated
  `quota` message. Grok and Antigravity use sliding inactivity deadlines under a named
  four hour absolute turn cap. Pi and Cursor keep their existing absolute turn bound.

- Daemon guarantees from rows 1, 2, 4, 5 and 12 of the T3 Code review, with the
  operator's decisions A and B of 2026-10-26.
  - **Row 1, a stop stops.** An operator's interrupt now withdraws the line's pending
    scheduled wake and holds the line with an operator question until it is released.
    Once the stopped turn has ended, a kept Claude Code connection whose interrupt
    settled while the engine still ran background work, or before the turn's result
    arrived, is closed, and so is a kept Codex app-server that still lists terminals.
  - **Row 2 and decision B, nothing outlives its owner.** Every harness, Claude Code's
    included, runs under `dromon.launcher`, through the new `dromon-launch` command. It
    ends the harness when the daemon stops it and when it sees that the daemon has
    died, however it died. The new line key `stop_policy` decides whether the
    harness's whole group and every descendant a walk of the process table finds end
    with it, `kill_all`, the default, or are left running, `leave_background`, in
    which case the launcher reports them and the daemon records them with their pids
    and commands as the new `processes_left` event, shown in `line_status`. At the end
    of a turn whose engine has ended, processes still carrying the line's mark are
    recorded as detached, where the platform lets the daemon read the mark. Grok and
    Codex had been signalled at their leader only. After review, on Linux the launcher
    is a child subreaper, so a descendant in a session of its own whose parent exited
    is still ended, and elsewhere that gap is recorded once as the new `walk_limited`
    event and shown in `line_status`. The daemon records an earlier run's marked
    survivors as it starts, and refuses a `kill_all` turn where `ps` cannot read the
    process table. The launcher's first act on a stop is `SIGSTOP` to the harness's
    group, its whole stop fits 3.5 seconds inside the Agent SDK's 5, it never signals
    a reaped pid, and the daemon signals a launcher by pid as well as group, with
    every wait bounded. The launcher answers the SDK's version check for the Claude
    Code it would start.
  - **Decision A, the keep-session ceiling.** The new line keys `keep_session_at_max`,
    `close` by default, `ask` or `keep`, and `keep_session_hard_seconds`, a day by
    default, decide what a kept Claude Code or Codex connection with background work
    does at `keep_session_max_seconds`. `ask` raises an operator question with the
    choices `extend`, `stop` and `let it finish`. Every choice ends at the hard bound.
  - **Row 4.** Every Claude Code line runs with `ENABLE_TOOL_SEARCH` pinned to `true`,
    so its tool schemas, and with them the prompt cache, stay stable between requests.
    A line's `settings.env` may not set it, nor any variable starting with `DROMON_`.
    After review the pin is also passed in the flag settings' env block, which Claude
    Code applies after the `env` key of `~/.claude.json`.
  - **Row 5, honest outcomes.** A Claude Code result between turns with no turn behind
    it is not taken for a turn the engine began. A system message subtype Dromon does
    not handle is surfaced in the line's activity, once, and a reported fallback to a
    model the driver was not written for refuses the turn, while a consent fallback
    is also put to an operator as a question. The permission gate refuses `AskUserQuestion` and
    `ExitPlanMode` with the advice to use `ask_operator`, and a Pi extension's dialog,
    still cancelled, is surfaced. A turn whose engine reports anything but success
    ends as failed, each recent turn names its outcome, and `line_status` gives the
    line's `condition`.
  - **Row 12.** A line token is renewed as a turn starts a new engine process,
    revoked when that process ends, and expires after an hour without use between
    turns, never during one.
  - **Decision A after review.** Ceiling questions still open when the daemon starts,
    or whose connection dies without a close, are withdrawn, and any failure to ask
    one closes the connection, as `close` does.
  - **After the live check of 2026-10-26.** A resumed Claude Code session's handoff
    result, a success that ran nothing, no longer ends the turn. The prompt is sent
    once more, and a second handoff refuses the turn. The engine's
    `background_tasks_changed` set is followed, and narrows the count of running
    tasks only between turns. Claude Code ends its own background shells once its
    input closes, so under `leave_background` the launcher now relays the harness's
    input, never closes it, and kills the harness alone without asking it to stop.
    The ceiling's question and the next prompt say what that policy leaves running.
    The new `dromon interrupt LINE` sends the client protocol's `interrupt`.
  - **After the re-review of the stop policy.** The daemon sweeps the process group
    of every launcher that has exited, from a launch record each launcher writes, so a
    launcher killed while its harness was frozen no longer leaves it frozen and alive,
    and on Linux the harness has a parent death signal. A stop that could not be
    verified, by a failed walk or an unfinished launcher, is recorded as the new
    `stop_unverified` event. Under `leave_background` the harness runs on for half a
    second before its freeze, so it can write out its transcript, and the input relay
    holds what a harness has not read, so it still sees the daemon's close. Left
    processes and background tasks are recorded as the executable's name with masked
    arguments, and the whole command stays only in the report file. Any success with
    no turn before the model's output is a handoff, and an interrupted turn's
    reported cost counts toward the budget. A launcher that `ps` shows unreaped, or
    whose harness has another parent, counts as exited, so a kept connection's killed
    launcher is swept while the daemon runs, and the line's engine is then treated
    as failed and closed. The scheduler's rest is bounded at a minute even with no
    wake pending, so an idle daemon sweeps too, and every sweep that acts or fails is
    written to the daemon's log.

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

### Changed

- Remote and local open federation are bounded further, from findings F1 to F3 of
  the round 12 review, decided by the operator on 2026-10-31. At most 64 undeclared
  remote keys are recorded, and a new key past that is refused at `hello` before any
  event. All undeclared keys share `[federation.open] remote_total_turns_per_hour`
  and `remote_total_waiting`, 6 and 500 by default, besides each key's own. A `hello`
  without a token must match the entry's start time as read when its connection was
  accepted, in a worker thread, and is refused when it cannot be read. With
  `remote = "open"`, `allowed_networks` is required, and a world-open listener lists
  `"0.0.0.0/0"` and `"::/0"`. A file that opens `remote` without it is now refused.
  From finding R1 of the round 12 re-review, all undeclared keys also share
  `remote_total_deliveries`, the `deliver` requests they may make together in ten
  minutes, 60 by default, which is one declared peer's rate, and at most 3,840, so 64
  keys together cannot exceed it. `FEDERATION.md` now states that an empty
  `allowed_networks` admits every address to a TLS listener that serves declared
  peers with `remote` closed, under the pin checks, and that undeclared projects on
  this machine are not capped, since each is a process of the same user that a live
  registry entry names. **Rollback hazard.** A daemon before round 12 refuses a
  project file that holds any of the new `[federation]` keys, `local`, `remote`,
  `name`, `allowed_networks` or the `[federation.open]` table, as an unknown key, and
  so refuses the whole file. **Fact,** read from `_known` at 7aaec56. Removing those keys before a rollback lets the older daemon read the file,
  though it still cannot read a log that holds `peer_opened`.
- A peer reaches only declared operators, decision 25 of `FEDERATION.md`, decided by
  the operator on 2026-10-31. An operator the project file declares, by `[daemon]
  operator` or an `[operators.ID]` table, may receive peer mail before sending
  anything, and the first note records the operator. An operator known only from the
  log, which a peer could reach before this change, is refused with `refused`, so a
  project that names an undeclared operator in `reach` must now declare it.
- Leases are durable, task of 2026-10-31. `lease_acquired` records the holder, the
  acquisition and the expiry, and `lease_released` a release or a name the project
  file no longer declares, so a restart keeps a held lease until it expires, and
  expiry stays its bound. A cleared session or a rebind ends a line's leases as the
  event log applies it. A change of a role's owner from one seat to another passes the
  previous owner's leases to the new owner with the same times, as `ROLES.md` records
  as decided. A start whose project file no longer declares a held lease's name now
  ends it. **Rollback hazard.** A daemon older than this cannot read a log holding
  `lease_acquired` or `lease_released`.
- Mute is renamed hibernate, at the operator's direction of 2026-10-27. The commands
  are `dromon hibernate LINE` and `dromon awaken LINE`, the client operations
  `hibernate` and `awaken`, the field and condition `hibernated`, the `role status`
  mark `hibernated`, the `request_handoff` result `owner_hibernated`, and the events
  `line_hibernated` and `line_awakened`. Event logs that already hold `line_muted` and
  `line_unmuted` still replay, since the codec decodes those as the new kinds, and new
  events are written only under the new names. `dromon mute` and `dromon unmute`, and
  the operations `mute` and `unmute`, are removed without aliases, since no release
  carried them.
- A line's relative `instructions` paths resolve against the project root, not the
  line's `workdir`, so a line moved to a worktree still reads the project's files. The
  engine receives the absolute path, which the composed instructions now name.
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

### Fixed

- Review of 60fcdd3. Fixed public stop notices no longer consult the disclosure list,
  so their display cannot reveal whether the list matches a known sentence. They
  still contain no engine text and consume no line disclosure allowance. The engine
  documents now state that Dromon trusts the runtime's own `_meta.toolNotRun`
  assertion that a rejected call did not run.

- Review of 7eda348. Grok and Antigravity continue after a rejected call is reported
  only when the update explicitly asserts non-execution through `_meta.toolNotRun`
  equal to Boolean `true`, without contradictory execution status. Pending and
  failed status alone now stop the turn. Stop notes use a closed Dromon vocabulary
  and discard engine labels and error text, including when reading older notes.
  Harness screening of those fixed notes leaves the line's hourly disclosure
  allowance untouched. Line-originated checks retain their existing allowance.

- Grok and Antigravity reject ordinary tool permission requests without cancelling
  the turn when a rejection option is available. Identity changes, reused permission
  identifiers, execution without approval, missing options and driver budgets still
  stop it. Budgets are checked before granting a new call. Engine refusals and driver
  interruptions leave a bounded, disclosure-filtered cause in the next prompt through
  a durable harness note that does not wake the line. Agent Client Protocol version 1
  defines no custom reason field, so a model-visible explanation for a selected
  rejection remains runtime-dependent and unverified live.

- `dromon restart` waits for its successor until a deadline, and each status request
  waits no longer than what remains of it. Each poll waited out a whole request
  timeout before, so a daemon that accepted and never answered could hold the client
  for hours, as the review of 7ff033f found. A hang now costs at most the drain wait,
  plus 70 seconds for the shutdown and startup bounds, plus one poll.
- ACP adapters now cancel and refuse a turn when the runtime omits the permission
  option needed for Dromon's intended decision. The refusal names the tool, whether
  Dromon intended to allow or refuse it, and bounded evidence of the offered option
  kinds and identifiers. An `allow_always` option never substitutes for `allow_once`.
- Grok's `get_command_or_subagent_output` may carry the kind `other`, which a live turn
  on 2026-10-31 showed, and so may `kill_command_or_subagent`, which shares its Grok
  category. Both still need `Bash`.
- Claude Code now records the positive percentage remaining in a documented,
  non-rejected rate-limit window, with its reset time when supplied. It retains each
  named window and shows the lowest remaining percentage among unexpired windows. A
  positive snapshot cannot replace a recorded zero before its reset. Handoff requests
  are recorded once per crossing below the role threshold and re-armed only above it.
  A daemon predating round 11 cannot read the log after a `budget_handoff_requested`
  event is recorded.
  Percentage conversion is exact and rounds toward less remaining. Grok Build's
  documented rate-limit classes are emitted to its `StopFailure` hook, but Dromon's
  Agent Client Protocol adapter cannot read that hook event or a documented reset
  field, so it classifies no new Grok quota errors. `GROK.md` records the available
  budget evidence for every engine and the evidence limits. `ROLES.md` and `ENGINES.md`
  describe the per-window snapshot and the fields retained in scrubbed recordings.
- Supervised runs, review of 2026-10-31. Reading a run's output opens it without
  blocking and reads only a regular file, so a named pipe a line puts there cannot
  stall reconciliation on the process worker, R1. A seat that owns its role needs
  write access to its working directory to start or stop a run, as any other line
  does, R3. A run whose supervisor cannot be told to start is recorded as `stopped`
  with no exit code and the caller is told nothing started, R6. The supervisor signals
  its command's group only from the thread that waits for it, between polls that
  found the command unreaped, so a late `SIGKILL` can no longer reach a reused group
  identifier, R7. A `ps` that exits with status 1 and a complaint is a failure to
  read the table, not an absent process. `ROLES.md` states that a change of owner
  passes every lease of the former owner, including one not taken for the role, R4,
  and `RUNS.md` states the window between checking and signalling a supervisor, R5.
- `write_scratch` refuses targets covered by project `read_only_paths` in the
  scratch directory it actually uses, including a seat's `tmp/lines/LINE`. Codex keeps
  read-only parent entries while denying protected descendants and makes read-only
  entries under `tmp/` explicit. `dromon wait` reports replies left out by the response
  size bound. Documentation states that worker joins are bounded and that engine
  enforcement after parent renames or newly created symbolic links is unverified.
- The process-inspection and launch-record-sweep workers now stop when their scheduler
  or daemon stops. Shutdown rejects new inspections, drops queued calls, and joins each
  worker for at most one second. A process-table read already in progress cannot be
  interrupted and may outlive that bound, after which its worker exits. This also
  closes workers when startup validation refuses a daemon before its service loop.
- A line can be created on, or rebound to, the packaged Pi model. The log checked a
  line's model as an identifier, which refuses the colon and slash of the
  engine-qualified names of Pi, Cursor Agent and Antigravity, so no Pi line could be
  created. The `engine` field of `line_created` and `line_rebound` is now a model
  name, an identifier or one with a colon and the harness's own identifier, as
  [`EVENT_LOG.md`](docs/protocol/EVENT_LOG.md) gives it. Every other field stays an
  identifier. Cursor Agent and Antigravity lines are still refused by their adapters,
  which allow them only in `dromon probe`, and their names no longer stand in the way
  once that refusal is lifted. **Rollback hazard.** A daemon before this change
  refuses a log holding such a line as corrupt.
- A Codex line's turn is refused when any directory of its own scratch, `tmp`,
  `tmp/lines` or `tmp/lines/LINE`, is a symbolic link or not a directory, checked
  by `lstat` each turn before its permission profile is built, so the owner of a
  role's worktree cannot redirect a read seat's grant into its own scratch. Review
  of ebedeed, R1.
- A Codex line with write access, no `Edit` or `Write` and a shell is granted its
  own scratch, where `write_scratch` writes for it, as a read line is. Review of
  ebedeed, G3.
- A line that does not write its working directory, a seat that does not own its
  role among them, has a scratch of its own, `tmp/lines/LINE`, for `write_scratch`
  and for the Codex read profile, which granted the whole of `tmp/` before. Either
  let a read-only seat, or a line with no writing tool, write a file of the owner's
  scratch, which stays `tmp/`. The tool's description says where each scratch is.
  Round 10 re-review, N1 and N2. **Behaviour change.** A line that is no role's seat
  and has no writing tool now writes under `tmp/lines/LINE` too, so its reports move
  there.
- A seat on an engine that cannot confine it, such as Grok, whose turn began while
  its role was vacant, no longer runs on beside a seat that then takes the role or is
  given it. The new owner's first turn waits for that turn, and for the seat's kept
  connection to close. Round 10 re-review, N3.
- A role's worktree inside another role's, or holding it, is refused as a shared one
  is, by `dromon role create` and `update` and in the project file, and a line that
  could write a directory inside an owned worktree, or one holding it, is refused or
  held as one in the worktree is. Round 10 re-review, N4. **Rollout hazard.** A
  project file with a role on the project root and another below it, or with a
  writing line on the root beside an owned role below it, is now refused whole at a
  start or a reload.
- Bound issue volume per line, ran different operators' relays concurrently while
  preserving each operator's notice order, and restricted file relays to protected
  state/runtime directories. File relay FIFO opens are nonblocking. Command relay
  success now follows the command's exit status even when a child holds standard error
  open. A child that starts a new session can still escape timeout process-group kills.

- A seat that does not own its role is built with its role's worktree read only on
  Claude Code and Codex, the engines that can confine it. The confining engines were
  derived from the capability table, but the step that applies a seat's read access
  to its engine was never added, so such a seat ran with its own access, write by
  default, beside the owner. A seat whose configuration Claude Code cannot make read
  only now fails its turn as the engine is built and is held. Round 10 review, F7.
- The one-writer rule holds on every path, at the operator's decision of 2026-10-31.
  A seat with a turn in flight is never unbound, hibernated or not, by `dromon role
  update` or `delete`, for an owned or a vacant role. A writer seat of an owned role is
  not unbound while it could write the worktree, hibernated or not. An owning seat is
  never configured out of its role's worktree, and a seat that runs elsewhere does not
  gain its role by a give, a take, a handoff or a failover. A line that is not a seat
  of a role is refused, by `dromon create`, `configure`, `set`, `rebind` and the
  project file, where it could write the worktree of a role another seat owns, and one
  a log left there before the rule waits, and is named by `dromon role status`. Two
  roles never share a worktree, compared by real path, at `dromon role create` and
  `update` and in the project file, which is refused whole at a start or a reload
  naming both roles. Round 10 review, F1 to F4.
- A role that returns to the project file is given the file's first owner in the same
  pass, rather than at the next start or reload. Round 10 review, F5.
- `dromon role status`, `roles` and a seat's `role_status` show whether the project
  file declares a role, each way its table differs from a role an operator holds, and
  the lines that wait beside the owner. Round 10 review, F6.
- Refused Codex items count toward the driver's tool budget whether first announced by
  `item/started` or `item/completed`. Matching item identifiers are counted once
  regardless of notice order. A refusal announced by neither notice remains invisible.
- A launcher retries a timed-out process-table walk until its 0.75-second deadline.
  If the table still cannot be read while stopping under `kill_all`, it reports the
  stop as unverified and exits unsuccessfully after ending the harness's group. It
  waits 0.25 seconds after `SIGKILL` and reports unverified if a process remains.
  Process-table inspections and detached-process scans share a worker with a bounded
  queue of eight calls. Launch-record sweeps use a separate worker with its own bounded
  queue. Detached-process scan results contain only process identifiers and command
  strings. Retiring an engine calls `Driver.close` on the owning thread, though a virtual
  machine not closed before it becomes cyclic garbage may still be finalized on any
  thread.
- Process-table failures use a typed error, and only an explicit timeout triggers the
  launcher's retry. A queued process call cancelled by its caller is skipped, though
  a call already running cannot be cancelled.
- Launcher tests start the source module from this checkout explicitly. Tests that need
  a real `ps` table are marked to skip when `ps` cannot run instead of branching their
  expected exit status on a probe result.

- Grok's `list_dir` may carry the kind `other`, which Grok gives it, as a live turn of
  58 tool calls on 2026-10-31 showed. That turn also confirmed the observed kinds of
  `read_file`, `grep`, `run_terminal_command`, `search_tool` and `use_tool`.
- A refused Grok `use_tool` call says why, and a file-backed one says to pass its arguments
  inline, so the line can correct itself.
- Grok tool calls are judged by the name Grok gives them in `_meta["x.ai/tool"]`.
  Grok's first announcement of a call has no `kind` and no `toolName`, so every Grok
  tool call was refused, including `search_tool`, and a later update that rewrites the
  title would have refused the turn. A call so named is identified by that name, and
  Grok's built-ins map to the line's tools by a table in `GROK.md`.
- Grok sessions now set the model to the exact identifier in the driver's settings
  when `session/new` or `session/load` reports another model. Dromon requires a model
  config option, requires the `models` field and model option to agree when both are
  reported, and checks the complete updated option list with the driver before
  prompting. Conflicting or unreadable model reports refuse the turn. A successful
  pin supersedes a mismatching notice held before its response, while a later mismatch
  refuses the turn. Tests cover new and resumed sessions, successful selection,
  refusal, conflicting model reports and a post-pin model notice.
- A Claude Code line reads engine messages of up to `MAX_MESSAGE_CHARACTERS`, 32 MiB of
  characters, which Dromon now passes to the Agent SDK as its buffer size in place of
  its default of 1 MiB. A turn whose engine still sends a longer message ends with a
  refusal that names the limit and a tool result carrying a large file, such as an
  Edit, a Write or a Read, as the likely cause, where it said only that the engine's
  process ended. Claude Code 2.1.310 was read to put an Edit's whole original file in
  its stream message, however small the edit. The bound, its memory cost and that
  reading are in `docs/protocol/ENGINES.md`. Reported by another project, whose line
  ended its turn on an Edit of a 2.5 MB file.
- A line given `fresh_session_each_turn` by a reload or `dromon configure` has its
  recorded session cleared at once, so `dromon settings` stops showing the old
  session as soon as the change applies. A turn in flight at that change, or at a
  change of `workdir`, records neither its session, its context estimate, a compaction
  nor a compaction wake when it ends, since they belong to the session the line no
  longer resumes. Required by the operator in the round 9 review, F1. Every cleared or
  rebound session also forgets the daemon's context estimate of the old session, with
  the compaction and the context-warning wake it would start. The first turn after
  such a reload had warned of the old session's context, which the line took to mean
  the old session had resumed, though its engine was built with none to resume.
  Reported by another project in round 9.
- `dromon config` warns about a shared working directory only for lines that can
  write it, as each engine confines them. A line needs `workdir_access` `write`, and
  on Claude Code a shell or file-write built-in or a policy allowing every tool, on
  Codex and Grok Build `Edit` or `Write`, whatever its policy, while a Pi, Cursor
  Agent or Antigravity line with write access always counts. Read-only lines and lines
  whose only tools come from MCP servers had been named as writers. Reported by
  another project in round 9, and the engines' rules required by the operator in its
  review, F16 and F17.
- The daemon is stale in `line_status` only when its own checkout holds code under
  its package directory other than the code it loaded, so a commit of documents no
  longer warns, and the warning clears once the checkout returns to that code. Git
  compares the trees only for commits not compared before, and caches the answer by
  commit. The stale note names the daemon's own checkout, not the line's worktree.
  Where the code cannot be compared, any move of `HEAD` is stale, as before.
  Reported by another project in round 9. Git runs on a worker thread, never on the
  daemon's event loop, so a status or a prompt never waits on it, and an answer git
  could not give, a timeout, a git that could not start or an error status, is kept
  for 60 seconds and then asked again. Required by the operator in the round 9
  review, F4 and F6.
- An attached client whose daemon is gone detaches by itself once 5 polls in a row
  have found nothing listening, over at least 10 seconds with the socket file missing
  or 60 seconds of refused connections, prints why, and exits with 4. A refusal waits
  the longer bound because a live daemon with a full backlog may refuse connections
  on Darwin, as the operator required in the round 9 review, F14. A slow daemon, or
  one that refuses a request, never counts. The terminal loop waits out its input
  timeout when curses returns at once, as it does once the terminal is gone. One
  client had polled a deleted project for days at full processor use. Reported by
  another project in round 9.
- `test_a_stop_under_kill_all_also_ends_a_child_that_left_the_group` and every other
  launcher test that waits for a process to go poll to a monotonic deadline derived
  from the launcher's own bounds, and a `ps` that times out under load counts as not
  yet gone rather than failing the test. One run failed at a load average of 189.
- Grok refuses a reused tool call identifier, including a repeated permission request,
  so the same identifier cannot obtain multiple grants while counting once. Tests also
  cover raw input changes in updates, non-`other` discovery calls, and a non-marker
  `.claude` directory. The Codex dangling-credential overlap test now isolates the
  `.cargo` guard it is intended to cover. The normal ACP order of announcement,
  permission request and updates is accepted and counted once, as is a permission
  request that arrives before the announcement.
- Grok lines may use `search_tool` for discovery and `use_tool` for a qualified tool
  served by that line's Dromon MCP server. Dromon checks the documented inline input
  shape, refuses file-backed or ambiguous targets, keeps the target fixed through the
  permission request, and counts the underlying Dromon tool against its budget. The
  ACP call shape remains unverified pending a first live line turn. Grok project checks
  allow only a real, empty `.claude/.cc-writes` marker and refuse other contents. The
  local guides do not establish whether Grok creates `.grok` in its isolated home, so
  no allowance was added for that path.
- Grok refusal advice for an existing sandbox profile now lists only missing deny and
  read-only entries. Tests check that an existing credential is omitted while a
  missing one is named. Codex writable-path validation also has a test that refuses a
  home-directory grant when `.netrc` is a dangling symbolic link. The Grok `.grok`
  marker remains unresolved pending a live Dromon turn, and the Codex probe conclusion
  is labeled as an inference.
- Codex and Grok profiles now share the operator credential path list and require
  existing project and workdir `secret` directories to be denied by real absolute
  path. Codex adds validated operator-granted `writable_paths` for existing absolute
  directories, only when the line has `Edit` or `Write`, and refuses grants that
  overlap credentials or other protected paths. A Codex probe signed in with
  `~/.codex/auth.json` denied, and that path is now denied by the profile. The operator
  will verify that a live line command cannot read it.
  Claude Code's existing `network.allowLocalBinding` setting is documented with a
  line configuration example. Grok symlink writes outside the workdir remain
  unverified, and no external writable path grant was added.
- The shared credential list now includes additional common stores. It is explicitly
  incomplete, and each engine exempts only its own sign-in path. A dangling link at a
  listed home path is denied by its absolute path. Protocol documents describe the
  probe result and the remaining unverified Codex path behavior, and correct when and
  where `writable_paths` are validated. Negative tests cover unsupported engines,
  missing write tools, Git overlap, links to protected paths, dangling credentials and
  a credential created between Codex turns.
- Grok home checks now allow an empty regular `hooks-paths` file and an
  `installed-plugins` directory containing only empty regular `*.lock` files, while
  refusing symbolic links and any other contents. Grok writer profiles hold the
  actual Git directories read-only and deny credential paths that exist under the
  daemon's real home when each check runs. A new profile includes only paths present
  when it is written, and the next turn refuses a credential created later. Refusal
  advice for an existing profile names values to add while preserving every other
  table and entry. `docs/protocol/GROK.md` records the observed and inferred behavior,
  including the pointer files that remain unguarded between turns.
- A line's own notes, such as fired wakes and task-end notes, leave the `own_notes`
  count once a turn of the line has ended after their delivery, whatever the turn's
  outcome, as well as once they are read, acknowledged or replied to. The count had
  grown with every note a line left alone after its work was done, and a worker line
  in another project counted 62. The rule is read from the event log alone, so it
  holds across a restart, and the wake prompt, `line_status`, `list_mail` and the
  client's views share it. A task note recorded during a running turn counts until
  that turn ends, and a note delivered between turns without a wake counts until the
  next turn ends. A prompt still marks nothing read, and mail from any other sender
  is unchanged. Required by the operator on 2026-10-30 in round 8. A turn whose
  engine returned no report, as when its prompt could not be composed, retires no
  note, since its prompt may never have reached the engine, and records
  `line_turn_ended` with the new optional field `unreported`, which a daemon that
  predates it cannot read, so a rollback after such a turn cannot load the log. The
  prompt and the counts line of `list_mail` now say that a note past the count is
  still listed by `list_mail` without a filter. Required by the operator in the round
  8 review.
- Wake prompts now state the operator at turn start that `ask_operator` will use when
  `to` is omitted, and explain that a new operator message during the turn changes
  that default.
- Role ownership notices are now suppressed across daemon restarts after the notified
  seat ends a turn that started after the change. Replay reconstructs each seat's
  turn starts and ends since the latest ownership change. A turn already running at
  the change no longer suppresses the notice, and a later ownership change starts a
  new notice for both affected seats.
- One context warning now gives one compaction wake. No context-warning wake is
  queued while the line's turn runs or a compaction decided for it is under way, any
  reported compaction withdraws a pending one whatever its size, and one that falls
  due under the automatic hold is withdrawn. The engines line received a second wake,
  queued one second into the first wake's turn, which fired after it. The wake's note
  now says why it was scheduled, with the estimated tokens, the warning and the
  source of the estimate. Required by the operator on 2026-10-30. After review, no
  context-warning wake is queued while another wake is pending or mail waits for the
  line, and the line is no longer marked as woken for one it did not get, so a wait
  for a background task that ends without a turn no longer suppresses the wake, nor
  does a wake of the line's own that replaced it and then ended without a turn. A
  compaction reported after the wake fired delivers its note without a turn of its
  own, as the engines line's log showed one. A restarted daemon takes a pending wake
  with the context note as the warning's, so a later compaction withdraws it.
- A `wake_after` bound to a running background task now acts as a wait. The line can
  end its turn and be woken by that task even after reaching the ordinary task-note
  limit, and the task note names the cancelled wake. If the task outlives the timeout, the
  scheduled wake fires. The tool descriptions explain how to use the wait.
- Claude Code and Codex lines granted a shell tool now require the daemon's `SHELL` to
  name an absolute executable. Claude Code additionally requires Bash or Zsh and
  receives the validated path as both `SHELL` and `CLAUDE_CODE_SHELL`, and the launcher
  drops inherited shell values from lines without shell tools. Codex supports Bash as
  its only shell tool and receives `SHELL` in its app-server and shell command
  environments. `line_status` reports the line adapter's selected path or validation
  reason. **Reported on 2026-10-29** by a worker line in another project whose shell
  changed from bash to zsh between turns. `check_disclosure` now explains where the
  project configures a list and that a check cannot resolve without one.
- A silent-turn reminder no longer says that the line sent no mail when it mailed
  another party in a turn its engine started by itself after a task ended. The saved
  state keeps the count of mail the line had sent to anyone but itself, and the
  reminder is dropped once that count has grown. It now opens "A turn of yours ended
  with" rather than speaking of the last turn. It is still given when every task it
  names has ended, with a closing sentence on how the work ended, which is decided
  for review. Reported by the core line on 2026-10-30.
- A silent-turn reminder no longer repeats a saved background-task count as if it
  were current. Dromon checks the same task identifiers against the live kept-session
  listing and later task records, and reports unknown when it cannot verify an end.
  `line_status` exposes the engine's `background_tasks_unknown` flag.
- A client request that times out now says that the daemon may still have carried it
  out, and `dromon send` points to `dromon sent` before a retry. **Found on
  2026-10-29,** when a send timed out at the client while the daemon was busy and the
  message had in fact been recorded, so a retry would have sent it twice.
- The role transition package now marks where each item came from, observed by the
  daemon, reported by a line, unknown, or unreadable by the daemon, each with its time.
  A field the journal entry lacks is taken from the former owner's standing brief, so
  a failover, which writes no handoff note, no longer leaves every field missing. A
  path git could not read is named as unreadable, never as absent or untracked, and
  the former owner's process state is said to be unknown where the platform cannot
  identify a line's processes. Found in the live failover of 2026-10-28.
- A hung test now ends the run after 300 seconds with the traceback of every thread,
  through pytest's `faulthandler_timeout`, instead of running out the job's 30 minute
  timeout in silence, as one macOS job did on 2026-10-28.
- A Pi test failed in two gates of one session with a Keleusma virtual machine
  dropped on another thread, and passed alone. **Inference.** A driver another test
  left open was finalized by the cycle collector on asyncio's child-watcher thread.
  The tests now turn automatic collection off and collect on the main thread every 25
  tests, so a leak is finalized on the thread that made it. The original failure was
  not reproduced, so the fix is not yet confirmed.
- Two engine deadline tests gave their fakes ten milliseconds to reach a stage, so a
  loaded host could fail them naming an earlier stage. The Antigravity first-output
  test and the Grok start test now allow half a second.
- The Grok sliding deadline test kept its limits a few tens of milliseconds apart, so a
  loaded host could fire the inactivity deadline where the absolute cap was meant to.
  Each limit is now far from the other and from the chunk delay, and the active turn
  still outlasts the sliding deadline, so the test still proves that activity resets
  it. Found by the core role's gate on 2026-10-28.
- `dromon config` now names the disclosure list the project file gives, or says that
  it gives none, in which case no line's mail or report is checked. It refuses a list
  it cannot read, as the daemon does. The terms are never shown. Found by the round 3
  reports, when `check_disclosure` refused in the team's own project for want of a
  list.

- `dromon config` no longer warns that the seats of one role share a workdir, which
  they do by design, since only the role's owner writes there. A writing line outside
  the role in the same workdir is still warned about. Found by the operator on
  2026-10-28.
- A read-only Claude Code line could leave its sandbox. A sandbox layer that set
  `allowUnsandboxedCommands` true or `failIfUnavailable` false let a shell command run
  outside the Seatbelt sandbox that denies writes to the working directory. The read
  sandbox now sets both keys itself, after every other layer, and a line whose project
  or own sandbox sets either the other way, or sets `excludedCommands` or
  `filesystem.disabled`, is refused. Each refusal of a read-only Claude line now has a
  test. Found by the review of read-only seats, findings 1 and 2.
- Grok's `read_only_workdir` capability is unknown rather than yes, since no live run
  has shown its reader profile deny a write. Review of read-only seats, finding 3. A
  Grok line with `workdir_access: read` is now refused until that capability is
  known, since the reader profile rests on Grok's built-in `read-only` base, which
  Dromon cannot inspect. Second round, findings 1 and 2.
- A Grok turn is refused when Grok loads any MCP server but Dromon's, which the
  operator's first live probe of 2026-10-27 showed it does from the user's own
  configuration, and when Grok does not list its servers. Grok's own notices for its
  servers, models and settings are read, and one that shows another server, another
  model or a mode that does not ask refuses the turn. A permission mode reported as a
  `configOptions` entry of the category `mode` is accepted. Grok's scans of Claude
  Code's and Cursor's MCP and hook files are turned off.
- `dromon probe --scrub` also replaces MCP server names, addresses, commands and
  arguments, slash command and skill names with their descriptions, host names,
  agent ids and a subscription tier, so a scrubbed Grok recording holds none of the
  operator's configuration.
- A malformed time in a role or budget event named the field `due`, and now names
  its own field, such as `resets_at`.
- `dromon send` prints the message identifier alone again. Since priority mail it had
  printed every key of the result, so a plain note ended with `priority False`. It now
  adds `priority mail` on a second line only when the note is priority mail. Found by
  the operator on 2026-10-27.
- The client protocol carries no floats, and the daemon sent two. `dromon status`
  failed once the daemon had run for a minute, because `events_per_hour` was a
  fraction, and `dromon restart` failed at once, because its `--wait-seconds` went out
  as a float. Both are whole numbers now, and `--wait-seconds` takes whole seconds. The
  encoder refuses a float wherever it appears and names its path, the daemon answers a
  response it cannot encode with an internal error under the request's identifier, and
  a request refused for holding a float is answered under its own identifier. Found by
  the operator's live check of 2026-10-27.
- The safe point of priority mail, and so of `dromon restart --interrupt`, moved to
  the moment after a tool result is delivered and before the next model call, once
  every tool use of the turn has its result, or the end of an assistant message that
  holds no tool use, by the operator's decision of 2026-10-27 after a review. It was a
  text or thinking event, which can share a message with a tool use after it. Each
  adapter feeds the new driver events `tool_result` and Pi also `message_end`, which
  the packaged drivers answer with `continue`.
- `recent_turns` gives `interrupted_for`, so a turn ended for priority mail or a
  restart says so beside an engine subtype such as `error_during_execution`.
- Claude Code's `thinking_tokens` system message, a progress estimate while the model
  thinks, is handled and no longer reported as unknown.
