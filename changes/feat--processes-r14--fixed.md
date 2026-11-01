Type: Fixed

- The kill-all pre-turn check no longer holds a line on one slow `ps`. It retries a
  timeout with attempts of 10, 20 and 30 seconds within 65 seconds, and the hold names
  each measured time. `list_processes` no longer returns an empty list when `ps` timed
  out or no environment could be read, and answers `unavailable` with the reason.
- The launcher's waits for a stopped harness and its descendants to end are monotonic
  deadlines and not counts of polls, so a stop no longer stretches under load.
- The compaction roster names background tasks by identifier only, so it is no oracle
  on the disclosure list. The private temporary directories are swept at each start by
  age, the sandbox denies a line reads of other lines' directories, the kill-all walk
  has its own worker so a stop does not wait behind it, and `stop_process` reads the
  listing again before the signal so a reused pid is not signalled.
- A task-end note and a `task_recorded` record no longer screen the line's own command
  or output against the disclosure list, which answered the line, outside the counted
  checks, whether a string it chose was listed. `role_status` shows a line its own
  record as recorded and every other seat's record screened.
- `stop_process` reads the line's mark a second time after its second listing, so a
  pid reused by a process with the same parent and command, which no listing tells
  apart, is refused unless it carries the mark.
