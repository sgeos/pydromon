Type: Added

- Round 14, operator tooling, in part. `dromon read` and the `read` request open a
  message the operator sent, such as one to a line, marking nothing, and `read`
  returns `recipient`. `dromon restart` also prints the commit the successor's
  checkout holds, and warns when it differs from the commit loaded. `dromon
  sync-worktrees` runs `uv sync --locked` in every role's worktree, reporting each,
  and `dromon land` warns when the branch changes `uv.lock`. `dromon renew LINE`
  starts a line's next turn in a fresh engine session, keeping its mail, brief and
  files. A start makes or checks every private directory Dromon owns with mode
  0700 before any turn, and refuses naming each wide one. A daemon given
  `DROMON_REGISTRY_DIR` registers in that fleet index only, as `FLEET.md` now
  decides. A release of changelog fragments may add only lines that Unreleased or
  the fragments it deletes held.
- Round 14, continued. `dromon retire LINE` and the `retire_line` request retire a
  line the project file does not declare, recording the new event `line_retired`.
  The line is hibernated, keeps its mail, takes no turn, mail or seat again, leaves
  the one-writer rule, and a project file that declares it is refused. `status`
  marks each line with `declared` and lists `retired_lines`, and `dromon status`
  prints `not in the project file` for a line the file dropped. **Rollback hazard.**
  A daemon from before round 14 cannot decode `line_retired` and refuses the log, so
  once one line is retired the daemon cannot go back below this release. A run's
  `run_ended` note ends with the test summary its output gives, with each skip
  reason, which `-rfEs` in `pyproject.toml` now makes pytest print, and `run_status`
  links each failed run to the run of its command that corrected it. The launcher's
  stop tests retry a run whose own bounds passed, and skip with the bounds named
  when no run is conclusive. `dromon run status` and the `runs` request list each
  scope's latest runs with command, line, start, end, exit code and output file, and
  `dromon run status RUN` shows one run's whole receipt.
- Round 14, commit runs. `[runs.commit_commands]` and `[runs.commit_refs] patterns`
  declare gates a line that only reads its working directory may start with
  `start_run` and a full `commit`, which the daemon checks is contained by an allowed
  ref, checks out detached in a private `commit-runs` directory in its state
  directory, records as `commit` and `checkout` on `run_started`, and removes once the
  run no longer blocks its scope, without following links.
- Round 14, audit fixes. `dromon sync-worktrees` compares each worktree's
  `pyproject.toml`, `uv.lock` and local build backend with `origin/main`, prints any
  difference, and syncs such a worktree only on the operator's yes or with
  `--accept-changes`, since uv builds and runs them as the operator. Retiring a line
  withdraws its open questions, its hold question included, and its pending wake in
  the same checked batch, and says so. An operator may read mail a retired line
  received, and the role text no longer names a retired line as waiting. A run's
  test summary is labelled unverified, is read only from the last short summary
  block with each skip reason cut to 80 characters, and is never recorded for a lost
  run. The launcher's stop tests excuse a passed bound only when a delay measured
  during that attempt explains it, rather than whenever the load average exceeds
  the processor count. A release of changelog fragments must hold every deleted
  fragment's entry, each under its own type.
- Round 14, commit run review. A commit run's commit must be the tip of a ref matching
  `[runs.commit_refs] patterns`, not any commit such a ref contains, and each pattern
  must lie under `refs/remotes/REMOTE/`, since a Claude Code line's enabled sandbox
  now denies writing `refs/remotes` and `packed-refs` and may write every other ref.
  A project file with a pattern under `refs/heads` or `refs/tags` is refused. A
  repository whose refs are not in git's files backend refuses commit runs. Every git
  call for commit runs reads neither the operator's global nor the system
  configuration, runs with the search path alone, turns off hooks, filters, the large
  file filter, credential helpers, submodules and replace refs, writes an
  `info/attributes` of `* -filter` before the checkout, and is killed with its whole
  process group at its bound. The checks, checkout and removal run on a worker of
  their own, a scope has one commit run starting at a time, and a start refused in
  the checks or the checkout records the new event `commit_run_refused` and a
  120-second cooldown for its scope. The command's `HOME` is a fresh private
  directory removed with its checkout, which is now `commit-runs/RUN/tree` beside
  `commit-runs/RUN/home`. `RUNS.md` says what a green commit run proves, who can
  move each allowed ref, and that a reading seat can read output a command wrote
  unsandboxed. **Rollback hazard.** A daemon from before this change cannot decode
  `commit_run_refused` and refuses the log once one is recorded.
- Round 14, commit run re-review. A commit run is refused unless the checkout's
  `.git`, the common git directory, its `refs`, `refs/remotes` and `packed-refs`, and
  each directory and loose file of the matched ref are real and the daemon user's, not
  links, read with `lstat` before and after the match, since a link at `refs` let a
  line's commit pass as `origin/main`'s tip. `SANDBOX.md` records that Claude Code
  already denies renaming a parent of a denied path, and that the `packed-refs` deny
  makes deleting or renaming a packed branch or tag, `git pack-refs` and `git gc`
  fail for a sandboxed line. `dromon sync-worktrees` also compares `uv.toml`,
  `.python-version`, `.python-versions` and every local source directory the project
  file or the lock names, and compares the whole worktree, saying why, where a backend
  path, a workspace, a backend other than `uv_build`, a local index, link, cache or
  mirror, or a local git source leaves the build's inputs unbounded.
- Round 14, final check of the commit runs and the sync. `dromon sync-worktrees` no
  longer lets uv read a worktree's `.venv`, whose interpreter a line could write and
  `uv sync` ran. It builds each environment with `uv venv --relocatable` and `uv sync
  --locked --link-mode copy` in a fresh private directory in the state directory's
  `sync-envs`, moves the old `.venv` there and the new environment into its place,
  and removes the old one unread. The daemon reads the device and inode of the
  project's `refs` and `refs/remotes` as it starts, and refuses every commit run
  while either differs, since a real directory a line moved into their place passed
  the `lstat` checks. The build-input tests pin each compared configuration file,
  `uv.toml` included.
- Round 14, restart preflight. The read-only `start_context` request, on the client
  socket only, gives the daemon's interpreter and its `DROMON_REGISTRY_DIR` and
  `SSH_AUTH_SOCK`, and `dromon restart` runs its preflight on that interpreter with
  those variables, as the successor starts, falling back to its own with a note for
  an older daemon.
