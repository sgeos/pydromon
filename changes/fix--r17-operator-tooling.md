Type: Fixed

- Round 17, item 1. The gate's disclosure scan runs in a linked worktree, reading the
  main checkout's list, accepted lines and project file through
  `git rev-parse --git-common-dir`, so a full-gate run scans. It still prints only
  file and line, still skips with its message in a clone or in continuous
  integration, and skips with the reason, rather than failing, where a sandbox
  refuses the read.
- Round 17, item 2. The rollback floor table names the commit and merge of every row
  that said pending or uncommitted, read from git history, and the closing reason
  row moves into the order of its merge.
- Round 17, item 3. Parallel branches no longer conflict over the state version or
  the rollback floor table. `STATE_VERSION` is the digest of the names of the files
  in `src/dromon/statechanges/`, one per branch, each pin is one file in
  `tests/state_pins/`, and a one-way change of the event log is a fragment in
  `docs/protocol/floors/`, which `scripts/assemble_floors.py` writes into the table
  at a release with the commits git names. The gate refuses a pending row in the
  table. A snapshot from an earlier daemon is ignored once.
- Round 17, item 4. A budget observed after the daemon's current time gives
  `age_seconds` as `unknown`, as its words say, rather than a negative number, and
  the words say how far ahead it was observed.
- Round 17, item 5. `dromon restart` prints a secret's name only when it has the
  strict form of an environment variable's name, checked where it is printed, and
  `dromon mail` words its page notes plainly.
- Round 17, item 6. The daemon starts its launcher and run supervisor from the same
  `dromon` it runs, still in isolated mode, so tests of an extracted review tree test
  that tree in every child.
- Round 17, item 7. `dromon settings LINE` shows each MCP server as configured,
  connected and callable, from the configuration and the engine's report at the
  line's latest turn, with no new network call. A tool counts for the longest server
  name it begins with.
- Round 17 review. Claude Code lines are denied the project's `secret/` directories
  for reading and writing, as Codex and Grok lines are, so the disclosure list is
  closed to every line, and a document granting one back is refused.
- Round 17 review. The gate's disclosure lookup ignores git's directory variables and
  fails on a worktree whose git directory is not where its main checkout put it.
- Round 17 review. A floor fragment's `Path` must lie inside the repository, a stray
  file in the floor or state change directories is left out with a warning rather
  than failing the gate or the package's import, and the pinned state history passes
  each cap on kept disclosure checks.
