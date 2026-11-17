Type: Added

- Queued runs. A line's `start_run` that waits only for the machine status or the
  project's slot is kept in a queue of at most 8 and begins by itself, in order and one
  in each look. The answer and `run_status` give its place and what it waits for, the
  line is sent a note when the run starts or the entry is dropped, with the reason, and
  `stop_run` with the queue identifier withdraws it. An entry is dropped, with an event,
  when its line is stopped, hibernated or released, when it no longer owns what the run
  would write, or when its working directory or the commit checked out there changes.
  The project file may name its gate slot with `runs.slot_lease`, a declared lease that
  holds a run in the queue while another line has it. `run_queued` and `run_dequeued` are
  new events, a branch state change file invalidates older snapshots, and a daemon
  from before them refuses a log that
  holds one. **Review of 883c00f.** An entry is also dropped after waiting longer than
  `runs.queue_max_age_seconds`, by default 7,200, from 60 to 86,400. A start whose
  parameter value looks like a credential is not queued, since the log cannot mask what
  the run starts from. A daemon that stopped between a queued run's start and its
  dequeue closes the entry as started on restart and tells the line the run is not
  dropped. Recovery uses event log sequence so a backward clock step does not hide
  the start. The run documentation records that credential patterns can refuse an
  ordinary example value and explains how to start it without queuing.
- The daemon says what `dromon config` says. It prints each warning about a file that
  loads to its standard error at a start, and `reload_project` gives them as `warnings`,
  which `dromon reload` prints.
- `line_status` gives `sampled_at`, the time of the machine status's sample, and every
  statement of the status a line sees names it.
