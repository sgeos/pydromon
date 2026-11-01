Type: Fixed

- A Claude Code line with `ViewImage` and without `Read` could read any file in its
  working directory, not only images. The operator's live run of 2026-11-01 showed a
  renamed text file answered by the engine, and a png of 6,000,287 bytes over the
  5 MiB limit viewed, although `image_refusal` refuses both. The rule ran in the
  permission callback, which Claude Code does not call for a `Read` inside its
  working directory. It now also runs in a `PreToolUse` hook that the engine runs
  before every `Read`, and a refusal there is final. `LINE_CONFIG.md` and `ENGINES.md`
  record the defect, the inference about its cause and what is left to verify live.
- Round 14 review. The ViewImage hook fails closed, since a hook that raises denies.
  A view-only line also asks before every `Read`, so the permission gate runs whatever
  the hook does. A `Read` that completes without a hook call ends the turn and holds
  the line. A view-only line is refused where the line's settings or a managed policy
  set `disableAllHooks` or `allowManagedHooksOnly`. `report_directory` accepted
  `tmp/Lines`, which overwrote another line's scratch on a volume that ignores case.
  `dromon role` no longer fails when an older daemon sends a seat's budget as `null`.
- Round 15. A supervised run stopped with a `setsid` child, or a relay command that
  timed out with one, left that child running. Both now end it while its parent lives.
- Round 15. A view-only line whose `Read` completed without the ViewImage hook is now
  held whether or not it mailed an operator during the turn, because the mail exemption
  does not fit a call that escaped its confinement. The hook policy check no longer
  passes a 65th policy file unread, and refuses a directory with more files than it
  reads. It also refuses any value of `disableAllHooks` or `allowManagedHooksOnly`
  that is not the boolean `false`, where it refused only `true`.
- Round 15, re-review. A relay task cancelled while its command's group was being ended
  no longer leaves the stderr pipe's read descriptor and loop reader open. A descendant
  found by the walk is now signalled only if its start time and parent are still those
  the walk read, so a process that took the identifier of one that ended is not
  signalled, by a relay stop or a supervised run's stop.
  Descendants are signalled deepest first, so a parent that ends does not orphan a
  child before the child's own check.
