Type: Fixed

- A commit run uses the interpreter the operator's gate uses. Its fresh home hid the
  interpreters uv manages, so the gate in the checkout ran under the first Python on
  the search path and failed. The command is now given `UV_PYTHON`, the path of the
  base interpreter of the environment the daemon runs from, and `UV_PYTHON_DOWNLOADS`
  set to `never`, and a start is refused before its checkout when that interpreter is
  missing. The cache stays private to the run.
- A start whose parameter match is cancelled kills and reaps the matching child and
  closes its pipes before the cancellation goes on. A cancellation after the child
  started and before the worker handed it back left the child running with both
  pipes open.
