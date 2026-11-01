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
- Round 15. The command relay's standard error is a pipe Dromon makes and closes with
  public interfaces only, so it no longer reaches asyncio's private subprocess
  transport, a porting hazard. What the command wrote before it exited is still read,
  in at most 16 reads, and a descendant holding the pipe is still not waited for. A
  timeout or a daemon stop now also ends the descendants of a relay command that a walk
  of the process table finds, including one in a session of its own, and a stop of a
  supervised run does the same for the run's command, with the process group signals
  as before. Both find a process by its ancestry alone and not by the line mark, which
  this platform cannot read, so another line's or the operator's processes are out of
  reach. A descendant whose parent has exited is not found, which `docs/architecture/RUNS.md`
  and `docs/protocol/RELAY.md` state with what an operator must avoid.
