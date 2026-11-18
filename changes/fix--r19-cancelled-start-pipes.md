Type: Fixed

- `runparams.match` no longer returns, or lets a cancellation go on, while the worker
  thread that runs the matcher child's `communicate` is still using the child's pipes,
  or before they are closed. A worker that had not yet started was cancelled with the
  call and never closed them, a descriptor leak for each cancelled start on a loaded
  machine, and a late one failed a Linux run. The call now waits for the worker, closes
  both pipes itself, and raises a cancellation that arrived while it was ending the
  child.
