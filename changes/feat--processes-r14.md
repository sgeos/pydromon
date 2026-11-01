Type: Added

- Round 14, processes and the sandbox. `wake_after` accepts `pid:NUMBER`, the `task_id`
  that `list_processes` now gives each process of the line's own, and the daemon
  watches for its end and wakes the line. `worktree_status` gives a line's commit,
  branch, distance from the last fetched `origin/main` and dirty paths, computed by
  the daemon outside the sandbox, with unread paths named as incomplete and `clean`
  null rather than true. A sandboxed Claude Code line is given a temporary directory
  and a package cache outside its working directory, which its sandbox may write,
  with `TMPDIR` and `UV_CACHE_DIR` set to them. A turn states the sandbox facts a
  line keeps rediscovering, `line_status` gives them as `sandbox_facts`, and the
  turn after a compaction names the background tasks Dromon holds as running.
  `dromon status` and the other lines' view show an idle line with background jobs.
  Residual, an attribution of a line's processes by ancestry where macOS 27 does not
  let the daemon read the mark is not built.
