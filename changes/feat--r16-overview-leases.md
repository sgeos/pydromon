Type: Added

- Round 16, item 1. Lines gain `overview`, one read-only view of the line's task,
  checkpoint, leases held, cost carried across releases and ceilings, joined from
  what `line_status`, `role_status` and `list_leases` answer. It records nothing and
  stays within 16 KiB. The operator reads the same view with the `overview` request
  and `dromon overview LINE`.
- Round 16, item 2. `acquire_lease` takes an optional `seconds`, from 60 to the
  project's `leases.ttl_seconds`, so a line holds a lease for as long as it asks
  rather than always for the whole limit. `list_leases` gives each lease's `seconds`
  and the `limit_seconds`, and `line_status` gives `seconds` with each held lease.
  The duration is read from the lease's two recorded times, so `lease_acquired` is
  unchanged.
- Round 16, item 3. `run_started` records `workdir_commit`, the commit a run in a
  worktree started on, and the run's one `Run ended` note, `run_status` and `dromon
  run status` name it. A test pins that a run's end sends one note and no other.
  `STATE_VERSION` is raised to 4, and a daemon from before this change refuses a log
  that holds the field. The note labels the commit as what the worktree's own `HEAD`
  says, which the line can move, since the line can write the files it is read from.
- Round 16, item 4. Tests pin that a line waiting for a run with `wake_after` keeps
  its wait across a daemon restart and across a hold at its turn or cost limit, and
  `RUNS.md` records what ends a wait held inside a turn and what is not fixed.
- Round 16, item 5. `CLIENT.md` records that anything able to use the operator's
  client socket holds operator authority, a person or the operator's automation.
- Round 16, item 6. `dromon restart` warns of each secret the project grants a line
  whose value in the client's shell differs from the running daemon's, since the
  restarted daemon inherits the running one's environment and keeps its values.
  `start_context` lists the granted names, the client sends its own keyed digest of
  each under a fresh `nonce`, and the daemon answers only `same`, `differs` or
  `not_granted`, so neither a value nor a digest leaves the daemon. A malformed
  `nonce` or digest is a bad request. Only `dromon stop` and `dromon start` take the
  shell's values.
- Round 16, item 7. `dromon send` and `dromon fleet send` without `--operator` send
  as the operator `@operator` names, the project's `daemon.operator`, rather than as
  the login name, so a line's reply to `@operator` threads. The `send` request's
  `operator` is optional, and its result names the sender.
