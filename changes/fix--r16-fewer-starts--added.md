Type: Added

- Round 16, item 4. Every `STALE BRIEF.` and `BRIEF CURRENCY UNKNOWN.` notice names
  the commit the brief was set at and the worktree's `HEAD`, or says which is not
  known.
- Round 16, item 5. A run in a working directory records `tree`, a digest of `HEAD`
  and its changed tracked files, in `run_started`, and `run_status` says whether the
  latest run still covers the tree, no longer covers it, or cannot tell. Untracked
  files are not compared. The state version of the restart snapshot is raised to 4,
  since the field changes the codec's types. **Rollback hazard.** A daemon from before it refuses the
  log once a run records its tree.
- Round 16, item 6. Every provider budget in `line_status`, `role_status` and the
  `budgets` request gives `observed_at`, `age_seconds` and `age` beside its figure,
  so a figure carried forward can be told from a fresh one.
