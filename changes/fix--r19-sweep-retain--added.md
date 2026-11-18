Type: Added

- `scripts/affected_tests.py`, run with `python3 -I` and no dependencies, takes a base
  commit and lists the test files that name the functions, classes and module-level
  names that the working tree changed in `src/dromon`, with what it cannot find, so a
  line can run them before the full gate.
