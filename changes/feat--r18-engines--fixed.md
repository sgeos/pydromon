Type: Fixed

- Claude Code lines loaded the operator's auto-memory index for the project, because
  their worktrees sit inside the project directory. Every Claude Code line now starts
  with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `autoMemoryEnabled` false, and a line's
  settings may not set the variable. This was read from the bundled Claude Code
  2.1.310 and has not been checked in a live turn. `SETTINGS.md` records the order in
  which Claude Code reads the two.
