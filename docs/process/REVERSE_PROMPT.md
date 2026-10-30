# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-30, one compaction wake, a wait for a task, and role notices once

**Done, merged in pull requests 87 to 89.**

- **Wait for a task.** A `wake_after` with `task_id` is documented as a wait. The
  named task's end always wakes the line, past the task-note limit, and its note names
  the wake it cancelled. A worker line in another project asked for this twice. A tool
  that blocks inside a turn was declined, since it would hold the turn with no mail.
- **Compaction wakes.** One context warning gives one wake, whose note names the
  estimate, the warning and its source. The engines line received two in round 6,
  because the next tick of the first wake's turn queued a second. After review, no
  context wake waits behind another wake or behind mail, a fired note whose compaction
  a turn already made starts no turn, and a pending context wake is adopted after a
  restart.
- **Role notices.** A seat is told of a role change once, across restarts. After this
  morning's restart the engines seat was given a handoff package of 2026-10-28 again,
  took the older task in it as its own, and acted on it. After review, only a turn
  that started after the change counts as told.

**Found by review.** A wait whose wake was cancelled or replaced left the line without
a context wake. A restart forgot which wake was the context wake. A turn running when
its role changed would have counted as told. All were fixed before merge.

**Verified live on 2026-10-30.** A push over SSH to a private test repository, with the
command Dromon builds and a deploy key in its own agent, and a refusal of an unpinned
host. One Grok probe turn, with the cached sign-in and no MCP servers loaded. Grok
sent `x.ai/mcp_initialized` rather than the `init_progress` that `GROK.md` expects.

**Round 7 reports.** core was satisfied, and asked first for a read-only tool for its
own event log entries, since both of its defects were found in the log. engines was
not satisfied, because a stale task resurfaced, which the role notice fix addresses.
review was not satisfied, because the daemon ran older code than every commit it
reviewed. The daemon was restarted onto the merged code after this round.

**Not verified.** A Grok line turn with Dromon's tools, a line pushing over SSH, and
any of this round's fixes in a live turn.

**For the operator.** Delete the test repository `sgeos/dromon-ssh-probe`, whose
deletion the session's token cannot do.
