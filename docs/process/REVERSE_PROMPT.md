# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-29, continuity across resets and handoffs

**Done, merged in pull requests 70 to 75.** The round 3 reports most often asked that a
line's task survive an engine reset or a change of a role's owner.

- **Standing brief.** `set_brief` records a line's task, decisions, artifacts and next
  action, stamped and bounded. It leads the first turn of a new engine session.
- **Handoff package.** Each item is marked as observed, reported, unknown or
  unreadable. Where the former owner left no brief or journal entry, the gate comes
  from its latest task record and the task from its latest local operator mail.
- **Smaller requests.** Task-bound wakes, the turn-end commit kept across a restart,
  `read_mail` on a line's own sent mail, spent compaction wakes withdrawn, a refusal
  naming the field of a one-field call, and the turn count's scope stated.
- **Operator fixes.** A hung test names itself after 300 seconds. `dromon config`
  names the disclosure list. `line_status` gives the daemon's load time. A timed-out
  client request says it may have taken effect. `DOGFOOD.md` matches the roles.

**Found by review.** Naming every matching field of a call would have let one counted
refusal answer several bits, so only a one-field call names its field.

**Round 4 reports.** core was satisfied. engines was not, because its package showed
every field unknown, which observed state now fills. review was not, asking for a key
on `send_message`, which exists, so the open part is keys across a restart.

**Not verified.** The inferred cause of the Pi test failure is not reproduced. The
filled package has not been seen live, since no handoff has happened since it merged.
Grok has not run a turn.

**For the operator.**
1. Sign in to Dromon's Grok home with the command in `GROK.md`.
2. Decide whether a review line may cite live probes the operator runs.
3. The Keleusma decisions listed in `KELEUSMA_DOGFOOD.md`.
4. State provider budgets with `dromon budget set`, since no engine reports one.
