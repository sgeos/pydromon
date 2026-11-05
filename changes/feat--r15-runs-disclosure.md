Type: Changed

- Task of 2026-11-05 and its review, disclosure budget. A `check_disclosure` that
  passes no longer spends the hourly limit of 10 failing checks and refusals, and
  counts instead against its own limit of 60 passing checks, and a line's checks may
  read at most 64 MiB an hour in all. Once either count is spent every check is
  refused, and content past the bytes left is refused by its size before it is read.
  The answer says what is left of each. The check reads and matches on a worker
  thread, off the daemon's event loop, one at a time for each line. A refused
  `set_brief` names the one field that matched, and still the generic refusal when two
  or more did, so no refusal names more than one field and none a term, line or
  position. **Rollback hazard.** `disclosure_checked` gained `checked_bytes`, so a
  daemon from before this change refuses the log once a check is recorded.
