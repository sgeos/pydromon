Type: Fixed

- Identify reset periods of limit and session counters in status, overview and the
  client with epochs, reset causes and times beside durable whole-line totals.
  Preserve unknown-cost totals across releases and daemon restarts.
- Include nonignored untracked files in queued-run content comparisons so committing
  a new file without changing its content keeps a queued run. Retain isolated reads,
  aggregate entry bounds, deadlines and unsafe-path refusals.
