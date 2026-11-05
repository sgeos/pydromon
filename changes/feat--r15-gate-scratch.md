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
- Round 15 review, V1. The workflow reader refuses a matrix that adds a version another
  way, with an `include` entry or any other `python` key, an `exclude` key or an
  unquoted version, instead of reading only the one line and checking fewer versions
  than CI tests.
- Round 15 review, G4. Tests run the gate's pyright loop with a stand-in `uv`, so that
  removing the no-version check or defaulting a failed read fails a test.
- Round 15 review, G1 and G3. The disclosure memory is emptied for a line whose
  configuration changes, such as its working directory, and a test pins that a restart
  note always starts its turn.
