Type: Fixed

- Expose the integration root and resolved working directory in compact line status.
- Index the newest role journal entry so overview avoids scanning journal history.
- Record lease expiry and cap existing durations when reload lowers the limit, with a holder notice.
- Preserve the restart reason when a later redirect overwrites it, avoiding a duplicate driver-stop note.
- Expose the oldest unanswered age and bounded recent thread context through list_mail.
- Resolve the integration root to the main Git checkout for a linked project and refuse inconsistent Git pointers.
- Shorten leases at reload with one durable event that preserves the holder across replay.
