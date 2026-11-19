Type: Fixed

- A queued run in a working directory is no longer dropped when `HEAD` moves to a
  commit of exactly the tree it was queued for. A queued start and a started run record
  `content`, a digest of the path, mode and bytes of every file the index tracks, as
  the files are on disk, and a move of `HEAD` that leaves it unchanged is recorded as
  the new event `run_retargeted`, with both commits, and keeps the start's place. A
  content that differs, by one file or more, or cannot be told, drops the start as
  before. The digest is read without clean filters, a monitor hook, the user's or the
  system's git configuration, or trust in the index's stamps, so none of these can
  hide a difference, and a repository holding a submodule leaves it unknown.
- `run_status` says that the latest run still covers the tree when `HEAD` moved and
  the content of every tracked file is as it ran, where it said before that the run
  no longer covered the tree.
