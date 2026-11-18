Type: Fixed

- `dromon run status` no longer fails with "the line exceeds 65536". The `runs` answer
  was bounded by scope and not across scopes, and each run carried its note and output
  tail, so a project with enough lines passed the protocol's line limit and the operator
  could not read it. It now gives at most `--limit` runs, 20 by default and 100 at most,
  within a 48 KiB budget derived from the line limit, the runs that block a scope first
  and then the newest, and says how many it left out and that `dromon run status RUN_ID`
  shows one run whole. A listed run carries no note and no tail, and `corrects` names the
  newest eight failed runs with a count of the rest.
- `dromon run status` shows the queued runs, with their place and what holds each back.
- Other answers that grew with the log are bounded. `questions` and the open questions of
  `line_view` keep within a byte budget and give a count of those left out, `role_entry`
  cuts a body past 48 KiB, and `status` and `roles` give the newest hundred retired lines
  and roles with a total. `docs/protocol/CLIENT.md` lists every request with its bound,
  and the four that stay unbounded with the reason.
