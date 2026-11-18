Type: Fixed

- A launch record whose harness's start time cannot be read, while its process is there,
  is no longer removed at once, so a live harness whose launcher has gone is looked at
  again. The record is kept for up to `MAX_START_RETRIES` sweeps, the count is written
  into it as `start_retries`, and it is removed when the time reads and differs, when no
  process holds the pid, when the count is spent or cannot be written. Nothing is
  signalled on a time that does not read.
- The temporary file that carries a launch record's retry count is created with
  exclusive creation and no following of links, at mode 0600 under a unique name, and is
  flushed before the rename, so a planted link is never followed.
- `scripts/affected_tests.py` finds definitions inside module-level `if`, `try`, `with`,
  loops and `match`, such as `ExitWatch`, runs `git` with `--end-of-options`,
  `--no-ext-diff` and `--no-textconv`, skips test files that are not regular files,
  and reports a source that is not UTF-8 as an error and not a traceback.
- Tests now run `processes.process_gone` itself, against a live, an exited and a reaped
  child.
