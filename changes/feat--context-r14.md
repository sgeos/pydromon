Type: Added

- Round 14. A line's configuration may declare `report_directory`, a directory under
  `tmp/` in its working directory such as `tmp/reports`, and `write_scratch` takes an
  `area` of `reports` that writes under it, so a review line writes its report without
  a shell. The area is refused when none is declared and while the line does not own
  its role's worktree, and every other path stays refused. `check_disclosure` checks a
  file in parts of 1 MiB cut after a newline and sums the counts, so a file of up to
  16 MiB costs one check, and it accepts the absolute path `write_scratch` reports
  when it lies under the line's working directory. A line longer than 1 MiB is
  refused. `line_status` gives `disclosure`, which says whether the project has a
  disclosure list, `provider_budget`, and in `cost_usd` the `unknown_turns`,
  `unknown_counts_as_usd` and `counting` that show how a turn of unknown cost counts
  against `max_cost_usd`, which is as zero. The engines that compact and what each
  loses are written in `LINE_CONFIG.md`.
  `compact_session` wakes the line as soon as the turn that asked ends, and not after
  a minute. `dromon settings` shows `allow_compact`, `context_warn_tokens` and
  `auto_compact_tokens` with their sources.
