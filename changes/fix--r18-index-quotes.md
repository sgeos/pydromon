Type: Fixed

- The evidence index screens only the text each entry quotes, not the identifiers,
  addresses, commits and run commands Dromon writes, so a report to an operator whose
  address matches the disclosure list is no longer withheld. A withheld entry is now
  named by its identifier, when that identifier has the generated form and is itself
  clear of the list, so a renewed session can reopen it. Round 18 fix, items 1 and 2.
- A line's note to an operator that names no `reply_to`, while that operator's notes
  wait unanswered, is told how many wait and the newest identifier, so the line can
  acknowledge the one it answered. The note is not linked by itself. Round 18 fix,
  item 3.
