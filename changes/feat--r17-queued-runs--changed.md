Type: Changed

- `check_disclosure` takes up to 64 files in one call, as many as the daemon remembers
  as passed for a line, where it took 8. A list that passes is remembered whole and,
  checked again, costs nothing. It is still one check, with the same byte bound.
- The age of the machine status is counted to the moment it is used and not to the read,
  which the daemon keeps for 15 seconds.
- The sentence that dates a missing or unreadable monitor says the monitor may have
  stopped earlier, since the daemon cannot see before it started.
- The warning of `dromon config` for a line that every turn would refuse covers every
  engine, through the adapter's own check, and the seats of a role, which are built
  with read access while another seat owns the role. A seat on an engine that cannot
  confine it is told its mail waits.
- The one-slash permission rule check reads a pattern in the first segment, leaves out a
  rule that the project or the line's working directory can satisfy, and is run by the
  daemon as well as by `dromon config`.
