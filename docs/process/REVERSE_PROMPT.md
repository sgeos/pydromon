# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, task 6, operator questions

**Done.** Tasks 1 to 5 were already complete, and pull request #11, ticks and per-line
tools, was merged first. Task 6 is complete.

- Lines ask through an in-process `ask_operator` tool, with optional choices. A
  question goes to the named operator, or by default to the operator who last messaged
  the line.
- The daemon keeps the queue in the event log. `dromon questions` lists an operator's
  open questions, or all with `--all`, and marks them delivered. `dromon answer` replies,
  only the asked operator may answer, and only once.
- An answer is a reply message, so it wakes the line in a later tick and closes the
  question.
- **Found live and fixed.** After a restart, the first live run's line had forgotten
  its question, because engine sessions lived only in memory. A `line_session` event now
  records each line's session, and the rebuilt engine resumes it. In the second live
  run, the line answered with a codeword it was given before the restart.

**Decisions made by this line, open to the operator's review.**

- Listing questions marks them delivered, since the client has shown them.
- A question without `to` goes to the operator who last messaged the line, and fails
  with a clear message when none has.

**Verification.** `uv sync`, 455 tests passing with warnings treated as errors outside
the sandbox, and all three live tests passing on Haiku 5.5. Ruff and pyright strict
clean. The full flow, from asking through a daemon restart to the answer waking the line,
ran live through the command line.

**Not verified.** Driver state is not persisted, so a driver's counters restart with
the daemon. Notes a line sends to an operator are recorded but shown by no client yet.

**Game loop, a later prompt on the same day.** On the operator's feedback, the
scheduler became a game loop. Each tick updates every line once and skips a busy one,
which counts as its update, turns run across ticks without blocking the loop, and mail
moves from a sent queue to the post office at the end of each tick. The operator's
direction on large per-line and per-project configuration, and on a client split from
the daemon like a multiplayer game, is recorded in the design. 458 tests and all three
live tests pass.

**No blocking questions, a later prompt on the same day.** On the operator's
decision, the `blocking` field and every blocked-line rule are gone. A line asks with
`ask_operator`, then ends its turn or keeps working as the line-agent decides, and
receives the answer as mail in a later turn. An open question never stops its line from
being woken. Dromon's own tools now reject
arguments outside their schema.

**Mailboxes, a later prompt on the same day.** On the operator's direction that nobody
is compelled to answer and that a hundred messages need tools, every party now has a
mailbox with read state, recorded by `message_read`. A wake prompt opens with "X new
message(s). Y unread. Z unanswered." and lists headers without bodies. Lines use
`list_mail` and `read_mail`, operators `dromon mail` and `dromon read`. All three live
tests pass with real Haiku lines reading their instructions through `read_mail`. 484
tests pass.

**Next.** Task 7, conversation control, then task 8, the attachable client.
