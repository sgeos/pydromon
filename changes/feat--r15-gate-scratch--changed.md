Type: Changed

- Round 15, item 3. `write_scratch` accepts the reports area for a line that writes
  `tmp/` and declares no report directory, writing `tmp/reports`, and does not nest a
  path that already begins with the report directory. Every refusal reads `write_scratch
  refused by the RULE rule` and names the rule that applied.
- Round 15, item 5. A tick, and the release of a held line, start no turn when every
  waiting message has already been replied to or acknowledged. The mail is delivered
  without a turn, and a compaction wake and a restart note still start theirs.
- Round 15, item 6. Re-encoding or stripping viewed images was not done. The reason is
  recorded in `docs/protocol/LINE_CONFIG.md`, since Dromon never holds the bytes the
  engine reads.
