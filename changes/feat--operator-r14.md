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
- Round 14, continued. `dromon retire LINE` and the `retire_line` request retire a
  line the project file does not declare, recording the new event `line_retired`.
  The line is hibernated, keeps its mail, takes no turn, mail or seat again, leaves
  the one-writer rule, and a project file that declares it is refused. `status`
  marks each line with `declared` and lists `retired_lines`, and `dromon status`
  prints `not in the project file` for a line the file dropped. A run's `run_ended`
  note ends with the test summary its output gives, with each skip reason, which
  `-rfEs` in `pyproject.toml` now makes pytest print, and `run_status` links each
  failed run to the run of its command that corrected it. The launcher's stop tests
  count a run whose own bounds passed as inconclusive on a loaded machine, retry it,
  and skip with the load named when no run is conclusive.
