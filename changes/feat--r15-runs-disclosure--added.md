Type: Added

- Task of 2026-11-05, run receipts. The daemon waits for each run's supervisor on a
  thread and records the run's end as soon as it exits, and `run_status` reconciles an
  open run before it answers, so a finished output is never shown as running.
  `wake_after` takes an open run of the line's scope as `task_id`, and the run's end
  withdraws the wake and wakes the line with its `Run ended` note and exit. A failed
  run keeps the last 20 lines and at most 2,048 bytes of its output, printable
  characters only, in the new `run_ended` field `output_tail`, shown labelled untrusted
  and quoted in the `Run ended` note, in `run_status` for the latest run, in `dromon
  run status RUN` and in the `runs` request. **Rollback hazard.** A daemon from before
  this change refuses the log once a failed run's tail is recorded.
- Task of 2026-11-05, run parameters. A declared run command may hold whole-argument
  placeholders `{NAME}`, each declared with a pattern under
  `[runs.parameters.COMMAND.NAME]`, and `start_run` takes `arguments` with a value for
  each. A value must match its pattern in full, be 1 to 256 printable bytes and never
  begin with `-`, and replaces exactly its one argument without a shell, so a line can
  rerun one suspect test outside its sandbox. `run_status` lists each parameter with
  its pattern, and `RUNS.md` gives the trust argument.
- Task of 2026-11-05, CI logs. `dromon ci-log LINE RUN [--job JOB] [--yes]` runs `gh`
  as the operator, shows the GitHub Actions run and asks before copying, then copies
  the last 200 KiB of its failed steps' log, printable text only and headed as
  untrusted data, into the line's scratch under `ci/`. The new operator-only request
  `scratch_place` writes it as `write_scratch` writes, following no link, and tells
  the line.
- Task of 2026-11-05, the review line's flow. A test follows a review line through
  `review-gate` end to end in a real repository and worktree, from `run_status` and a
  refused start without a commit to a failed run on a branch tip, its wake, note,
  tail, output and removed checkout, and a corrected run linked to it. `RUNS.md`
  records the flow.
