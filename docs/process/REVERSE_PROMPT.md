# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-30, own notes retired, and several files in one disclosure check

**Done, merged in pull requests 91 and 92.**

- **Own notes.** A line's own notes leave its count once a turn of the line ends
  after their delivery. A worker line in another project counted 62, and core 87,
  nearly all fired wakes and task notes already dealt with. The rule is read from
  the event log, so every view and a restarted daemon agree. A turn whose engine
  returned no report retires nothing, and its end is recorded with `unreported`.
- **Disclosure checks.** `check_disclosure` takes up to eight files as one check.
  Each file is counted on its own and the counts summed, and no answer says which
  file matched. Its description says it covers only the project's configured list.
- **The operator in the prompt.** The wake prompt names the operator that
  `ask_operator` reaches when `to` is omitted, as at the turn's start.

**Found by review.** A note delivered into a turn whose prompt was never composed
was retired unseen. A term could span two files. The prompt's operator could change
during the turn. A daemon before this round cannot read a log holding `unreported`,
which the protocol now warns of. All were fixed or documented before merge.

**Verified.** Full gates outside the sandbox and continuous integration passed on
both pull requests. The daemon was restarted onto `2caeb4f`. Replaying this
project's log and the other project's log read-only under the new rule gives every
line 0 own notes. No line has yet run a turn on the new code.

**Round 8 reports.** core was satisfied, and asks first again for a read-only tool
for its own event log entries. engines was not satisfied, since a stale brief led
its resumed context after a new task arrived. Two generic compaction notices it
cited were scheduled at 18:08Z, before the fix of round 7 was loaded at 18:36Z.
review was not satisfied, since its daemon was stale against the checkout while it
reviewed. Both dissatisfied lines point at stale context, which is the best next
iteration with the event log tool.

**Not verified.** Any of this round's changes in a live turn. The other project's
daemon runs the code it loaded before this round.
