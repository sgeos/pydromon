Type: Fixed

- Round 16, item 1. Bisecting runs put a rise in stalled child starts in continuous
  integration at the change that read `git status` on every `set_brief` and every
  kept turn. The read is no longer started for a working directory the file system
  shows to be outside any checkout, and in the test suite an autouse fixture answers
  a clean tree without starting git, so only the tests marked `real_git_status`
  start it. The digest is not cached on `HEAD`, the index and the top-level time,
  which would hide an edit in place below the top level.
- Round 16, item 2. `dromond --check` holds the live snapshot before it copies the
  event log and compares only within the copy's bound, so a snapshot the daemon
  writes during the copy is never read against the check's own appended rows. Each
  answer names the snapshot's event and the bound, and as it ends the check says
  whether the live snapshot, which a successor restores, is still the one compared.
- Round 16, item 3. A line whose turn a restart ended is noted and woken when the
  turn's report names the restart, even where priority mail or a change of access
  asked at a later safe point of the same turn, which had taken the note away. A
  report that names another stop, such as a Pi turn past `max_turns`, or none, is
  not woken, nor is a failed or cancelled turn.
- Round 16, item 7. A `compact_session` request is spent by any compaction the
  engine reports after it, as its wake already was, so a compaction the engine made
  by itself in the same turn no longer leaves the request to compact the session
  again in the next turn.
