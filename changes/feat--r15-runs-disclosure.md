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
- Round 15 review, merged with round 15 item 4. A file check whose bytes all already
  passed is answered from the memory of passes and spends nothing of any budget, even
  once one is spent, and a spent budget still refuses unread every file whose size is
  not that of a remembered pass and matches nothing. A check whose list or line
  configuration changed while it ran is not answered from memory and its pass is not
  remembered. A file `scratch_place` writes is never remembered as passed, so a listed
  term in a placed CI log is still found, as review C1 asked.
- Round 15 re-review. A `write_scratch` the disclosure list screens is a check too, so
  its text and path spend a pass, or a failure when refused, and their bytes, and once
  either count is spent, or the bytes left are too few, the write is refused with the
  `disclosure budget` rule before it is screened or written. The tests now pin that the
  run parameter matcher kills its child at the default deadline of 2 seconds and when
  a start is cancelled.
- Merge of main at `b41fc7f`. `STATE_VERSION` is 3, since this branch's disclosure
  passes and bytes and its run tails change the codec's types and the state the pinned
  history replays to, so a snapshot of version 2 is not restored and the log is
  replayed instead.
