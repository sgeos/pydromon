Type: Fixed

- Round 18, item 2. A test pins that a commit run's interpreter imports Tk wherever
  the gate's does and that its fresh home takes caches. The review gate failures for
  want of Tk ran under daemons without the interpreter fix. `review-gate` on the
  current daemon then collected every test, and its 7 failures came from the run's
  paths. Six sign-in probes took the deep checkout as their project, and now run in a
  short directory of their own. A commit run's fresh home is now `run-home`, since
  the name `home` was read as the user name in every scrubbed recording.
- Round 18, item 4. The machine status policy has one wording, in
  `dromon.machinestatus`, which the warning a turn proceeds with, the refusal of a
  run, the `start_run` tool and `line_status` all give. A level raised only by free
  disk space no longer tells a line to hold a full gate that the daemon lets start.
- Review of 513928b. The floor fragment for the expiry and lowered-limit causes of
  `lease_released` searched for a value another enumeration held long before, so
  `upgrade-check` listed 23 newer one-way changes from 6567167 where there are 24. It
  now searches for a value only that change added, and a test compares the codec's
  kinds, fields and enumeration values with those of 6567167.
- Review of 513928b. `upgrade-check` no longer writes beside a log in write-ahead
  mode, and reads one in a directory it may not write. A log with a hot journal,
  which a read-only open refuses, is rolled back inside the scratch directory, with a
  warning that the journal must be copied with the log. A scratch directory left by
  a check killed with `kill -9`, which holds a copy of the log, is removed by the
  next check. `UPGRADE.md` no longer advises `sqlite3 .backup` on a running daemon,
  and says what to copy after a crash.
- `UPGRADE.md` keeps the launch records with the final copy and, after the upgrade,
  lists the processes FROM started with `ps` and ends any orphan by hand, since from
  pull request 166 a launch record written before start times were recorded is
  removed with nothing signalled.
