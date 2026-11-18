Type: Fixed

- Claude Code lines loaded the operator's auto-memory index for the project, because
  their worktrees sit inside the project directory. Every Claude Code line now starts
  with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `autoMemoryEnabled` false, and a line's
  settings may not set the variable. This was read from the bundled Claude Code
  2.1.310 and has not been checked in a live turn. `SETTINGS.md` records the order in
  which Claude Code reads the two.
- A turn whose engine closed its connection or failed to start named only that, so a
  line whose engine refused its configuration at start could not tell why. Codex,
  Claude Code and Grok now keep the last 4 KiB of their engine's standard error, and
  the error of such a turn ends with as much of it as the 240 characters of the
  closing reason allow, scrubbed of configured secrets before it is divided at a kept
  turn's mark or put on one line, masked, with the home directory written `~` there
  and in the error's first line, with the launcher's own notes giving
  way to the engine's, and with a kept engine's output from before the turn labelled
  as such. A Claude Code process that ends under the Agent SDK is now recorded as
  `engine_closed` rather than as an engine error, and its standard error lines reach
  the daemon's through a bounded writer thread, so a blocked standard error never
  holds the event loop. `ENGINES.md` records the rule and its limits.
