Type: Fixed

- The test suite no longer fails a gate at its end after a killed session. A test that
  makes a directory unreadable on purpose restores it in a `finally` clause, which a
  kill skips, and pytest could then not delete the old base directory, so a later gate
  exited 1 after every test had passed, as one did on 2026-11-11. The worktree test
  registers its restore before it closes the directory, and a session fixture gives the
  owner back access to any such directory left by an earlier session.
