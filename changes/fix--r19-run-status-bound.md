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
- A text the daemon cuts to fit one answer now says so in the text itself, since a
  client written before the cut reads only the text. A `questions` text and a
  `role_entry` body end with "[cut by the daemon, ...]", the last question ends with how
  many were left out, and a run in a list has the words "[not in a list, ...]" where its
  note and tail were `null`. The protocol version stays 1, and `docs/protocol/CLIENT.md`
  records the change as one-way for the port, what an older client still cannot see, and
  that a version bump is the operator's decision.
- A queued run's reason is cut by its written length and not its UTF-8 length, the proof
  of the bound now includes the `checkout` path of a commit run, a test pins that a
  blocking run is always the newest of its scope and so is never hidden by the window,
  and the line that says what a list left out names what it takes first.
