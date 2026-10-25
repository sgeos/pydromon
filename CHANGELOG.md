# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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
