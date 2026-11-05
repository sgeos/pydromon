Type: Added

- A turn of an engine that reports token counts and no cost, which today is Codex, is
  given a cost estimated from those counts and a price table in `dromon.prices`,
  labelled estimated, so `max_cost_usd` applies to it. The prices are placeholders not
  yet confirmed, and `PRICES_VERIFIED` says so. An engine that reports neither keeps
  `limit_enforced` false.
- A new event, `line_cost_recorded`, keeps each line's whole cost and turn total
  across releases and daemon restarts, the reported and estimated sums apart, beside
  the window counts a release resets. `line_status` gives it as `cost_usd.whole`,
  `role_status` as `cost_whole` for each seat, and `dromon status` prints it.
- `line_status` lists each unanswered message with its reason, at most eight, in
  `unanswered_mail`. Wake notes, harness notices, role notices and notes that ask
  nothing never count, and an operator note counts as answered once the line replies
  to a later note from the same operator in the same thread.
- A standing brief set before the worktree's last change leads a new or compacted
  session as stale, never as current. The warning that the daemon's loaded commit
  differs from its checkout is given in full when either commit changes, and in one
  short sentence otherwise.
- A snapshot of the state beside the log, `events.sqlite.snapshot`, owner only, with
  digests of the log's first and last rows it covers and of the state's shape. A start
  restores from it and replays only later events, and falls back to a full replay on
  any mismatch, naming the reason on standard error and in `event_log.start` of the
  daemon status. `dromond --check` compares it with a full replay. A start from a
  snapshot took about a third of a full replay on synthetic logs of 3.3 and 33 MiB.
- A role may name the roles whose work it reviews, in `roles.ID.reviews` of the
  project file or with `dromon role create` and `update --reviews`, recorded in
  `role_declared`. Each named role must exist. `role_status` and `dromon role` give
  `reviews`, `reviewed_by` and `review_diversity`, which names each pair whose owners
  run the same engine and model, and the reviewer's owner is told so at the head of
  each turn, and a new owner in its role notice. Nothing is refused for it.
- `docs/protocol/LINE_CONFIG.md` states the one timestamp form records accept, whole
  seconds in Coordinated Universal Time with a `Z`, and where callers see it. A near
  miss of it is refused naming the form.
- Every failed turn records its cause in `line_turn_ended`, one fixed word from
  `not_started`, `refused`, `engine_error`, `engine_closed`, `engine_reported`,
  `limit`, `interrupted` and `unknown`, with the daemon's exception type or a listed
  result subtype as its class where one exists and never an engine's text.
  `line_status` and the daemon's `status` give it as `last_failure`, and the daemon
  log gives one line for each failed turn. A closed engine raises the new
  `EngineClosed`, a kind of `EngineRefused`.
- A start that restored the state from a snapshot now checks it against a full
  replay of a copy of the log, off the event loop. A mismatch, named `state_differs`,
  `log_corrupt` or `check_failed` with the exception's type, holds every line in
  memory, deletes the snapshot, and is shown in the daemon log, in `event_log.start`
  and as a `RESTART REQUIRED.` row of `dromon status` until a restart. No snapshot is
  written while the check is pending or after it failed.
- The snapshot's directory is synchronized after the snapshot moves into place.
- `STATE_VERSION` is 2, and `tests/test_state_version.py` pins each version with a
  digest of the codec's types and of a replayed history, so a change to either fails
  until the version is raised.
- `brief_set` gains `workdir_dirty`, the digest of the worktree's `git status`
  entries, and each turn of a kept session opens with `STALE BRIEF.` while the commit
  or the uncommitted state differs from the brief's, until the line sets it again.
