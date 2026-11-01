Type: Added

- Round 14, operator tooling, in part. `dromon read` and the `read` request open a
  message the operator sent, such as one to a line, marking nothing, and `read`
  returns `recipient`. `dromon restart` also prints the commit the successor's
  checkout holds, and warns when it differs from the commit loaded. `dromon
  sync-worktrees` runs `uv sync --locked` in every role's worktree, reporting each,
  and `dromon land` warns when the branch changes `uv.lock`. `dromon renew LINE`
  starts a line's next turn in a fresh engine session, keeping its mail, brief and
  files. A start makes or checks every private directory Dromon owns with mode
  0700 before any turn, and refuses naming each wide one. A daemon given
  `DROMON_REGISTRY_DIR` registers in that fleet index only, as `FLEET.md` now
  decides. A release of changelog fragments may add only lines that Unreleased or
  the fragments it deletes held.
