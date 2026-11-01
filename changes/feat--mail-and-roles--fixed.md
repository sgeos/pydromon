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
