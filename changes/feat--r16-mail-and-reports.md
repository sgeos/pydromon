Type: Added

- Round 16, item 3. Dromon reads the shared machine's status file, which a monitor
  writes, before it starts a supervised run, a commit run or a line's turn, when the
  project names it with `daemon.machine_status`. At `critical` no turn starts and a run
  is deferred unless only free disk space raised the level. At `elevated` for memory,
  swap, load or a reason Dromon does not know a run is deferred and a turn proceeds with
  a warning in its prompt. A status that is stale, missing or unreadable is treated as
  `elevated` and reported. A deferred run is refused with a message that tells the line
  to wait and ask again, a deferred turn tells the line and the operator once, and
  `line_status` in detail gives the status. Nothing is read unless the key is set, no
  command is run and no network is used.
  The file is read on a worker thread and the loop waits for it at most half a second,
  so a file on a mount that hangs cannot block the daemon, and until the read returns the
  status is unreadable, which counts as elevated.
