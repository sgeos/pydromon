Type: Changed

- Round 14. The result of `write_scratch` gives the bytes written and the absolute path
  of the file, in place of the relative path. A budget that is unknown is a named entry
  with `status` `unknown` in `role_status` and `line_status`, never `null`, and a
  field of a stated budget that the statement left out says `unknown` too. The
  `estimate` of `cost_usd` says `unknown` in the status tools where it was `null`,
  while the client protocol's `cost_estimate_usd` stays `null`. The budget line at the
  head of each turn's prompt says that turns of unknown cost are counted as $0.00. A
  file for `check_disclosure` may now exceed 1 MiB, up to 16 MiB combined.
- Round 14 review. The `api` strategy is refused on the Codex, Grok and Cursor Agent
  engines, which report no cost, so `max_cost_usd` could not bound what such a line
  spends. `compact_session` is refused after a compaction that left the context at
  or past the warning, or of unknown size, until a context below the warning is
  reported. `report_directory` refuses `tmp/worktrees`, `tmp/runs` and `tmp/probes`
  as well as `tmp/lines`, in any letter case or Unicode form, and any component that
  starts with a dot. The write compares the directory with them by inode.
