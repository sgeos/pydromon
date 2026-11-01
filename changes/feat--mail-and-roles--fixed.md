Type: Fixed

- A turn built before a change of owner no longer runs on with its old access. Each
  running turn whose line's access the change altered ends at its next safe point, and
  the next turn is built with the new access. A seat that takes its role inside its
  own turn is sent a note that wakes that next turn.
- A failed line stays failed across a daemon restart, since `line_turn_ended` now
  records a failed turn.
- A role whose worktree does not resolve still claims it, so a second role can no
  longer take the directory holding a missing worktree.
- One `line_status` call copies the event log's state once, not 27 or 49 times. At
  100,000 messages the default call fell from 83 to 58 ms and the detailed one from
  129 to 51 ms on the line's machine.
- A failure the engine reports without raising marks the line failed in an ordinary
  turn, as it did in a turn the engine started by itself, so a line that mailed an
  operator before it failed stands `failed` and waits for new mail.
- `line_status` names the brief's stamp `workdir_commit_when_set` and
  `daemon_commit_when_set`, so a commit the brief was set under is not read as the
  running daemon's.
- A line can no longer hide its own ask from an operator. `asks_reply` false is
  honoured only for a note to a line, and a correction never asks less than the note
  it corrects.
- `list_leases` names a lease's note `holder_note_unverified`, and its caveat says the
  note is the holder's unchecked claim.
- Rollback floor. Once a log holds `message_sent.asks_reply`, a correction,
  `message_acknowledged.reason`, `line_turn_ended.failed`, which every failed turn
  writes, or `lease_acquired.note`, no daemon older than commit `4330502` can open it.
  `EVENT_LOG.md` gives the floor of each.
