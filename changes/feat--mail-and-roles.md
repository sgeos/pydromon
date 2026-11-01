Type: Added

- Unanswered mail that can be closed. A reply or a correction naming a message closes
  it for the unanswered count, a line may correct its own note by naming it with
  `reply_to`, and `send_message` takes `asks_reply`, so a note that asks nothing is
  not counted. A note that leaves it out asks a reply unless it is itself a reply.
  `list_mail` with `unanswered_only` lists what the count counts, each with how long
  it has waited, and `acknowledge_mail` takes a short `reason`, recorded and shown
  only to the recipient. `message_sent` gains `asks_reply` and `message_acknowledged`
  gains `reason`, both left out when absent.
- `@operator` names the project's default operator, unless an operator of that name
  is registered. An address naming no registered operator is refused with the
  operators that exist.
- Paging of the other lines in `line_status` with `other_lines_offset`, and
  `other_lines_next_offset` while lines remain past those shown.
- `list_leases`, a read-only inventory of every live lease with its holder, both times
  and the note `acquire_lease` now takes, and the declared leases no line holds. It
  and `release_lease` say that a released lease is no proof that processes ended.
- Every turn's prompt names the commit the standing brief names beside the worktree's
  `HEAD`, and the daemon's loaded commit beside its checkout's, with a warning when a
  pair differs.
- `report_role_ready`, by which a seat reports its role ready. It is refused until
  ownership has passed to the caller and no former writer is in flight, and it reports
  the worktree's `HEAD` and dirty paths together to an operator.
- `dromon send` and `dromon fleet send` take `--no-reply` and `--ask-reply`, and the
  daemon's `send` takes `asks_reply`. Given neither, a new note asks a reply and a
  `--reply-to` note asks none, so an operator's note can leave a line's unanswered
  count without being acknowledged.
