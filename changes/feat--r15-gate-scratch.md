Type: Added

- Round 15, item 1. The gate type-checks every Python version the workflow's matrix
  lists, on both platforms, reading the list with `scripts/ci_pythons.py`, which fails
  when it finds none. The earlier gate checked only the interpreter it ran on, and the
  workflow's other interpreter found Python 3.12 errors it had missed.
- Round 15, item 2. The pytest summary ends with one line, `not run here: ...`, that
  names the socket, live and process-table tests left out, with counts and the reason,
  or says they all ran.
- Round 15, item 4. A file whose exact bytes already passed the disclosure list, as a
  checked text or as written by `write_scratch`, is answered by `check_disclosure` as a
  pass without spending a check, so checking a draft and then its file costs one.
