Type: Added

- The operator's override of the machine status. `dromon run admit` records
  `run_admitted`, which admits runs one at a time while the status defers them, at
  `elevated` and at a status that cannot be relied on, with the operator, the time, the
  expiry and a reason. It is refused at `critical`, never lifts `critical`, lasts 60
  seconds to four hours and an hour by default, and ends by itself or with
  `dromon run admit-end`. A run it admitted names the operator in its receipt, and
  `run_status` and `dromon run status` show the override. `run_admitted` and
  `run_admission_ended` are new events, so a daemon from before them refuses a log that
  holds one.
- A run's receipt says what it tested. `run_started` records `dirty_paths`, the count
  of paths `git status` listed when it started, so zero is a clean tree and a count that
  could not be read is never worded as clean. `run_ended` records `coverage`, the counts
  of pytest's final line, read from the same line as the quoted summary. `run_status`
  and `dromon run status RUN` show the tested commit, whether the tree was clean, the
  tree digest and the counts, so an old success cannot pass for current verification.
  The counts are the output's own claim and are labelled as that. A daemon from before
  the fields refuses a log that holds one.
- One deferral for every surface. A run that cannot start has one reason and one
  condition for starting, built once and read by the refusal of a start, the reason a
  queued run keeps, `run_status`, the note to the operator and the warning a turn
  proceeds with. `run_status` now says what holds a start back, or that nothing does.
- Each gate has a temporary test root of its own. `scripts/check.sh` makes a directory
  with `mktemp -d`, gives pytest a base inside it, and removes it when the gate ends, so
  two gates at once cannot prune or repair each other's directories.
