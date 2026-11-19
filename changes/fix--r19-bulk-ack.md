Type: Added

- `acknowledge_mail` takes a bulk form. `message_ids` closes up to 50 named notes, and
  `from_sender` with `up_to_message_id` closes the oldest 50 notes from one sender that
  still count as unanswered, up to a named message. Each message records its own
  acknowledgement, the whole of `message_ids` is checked before any is recorded, and a
  message outside the caller's mailbox, a question or an issue is refused or left
  counted. No event changes. A reply naming a line's note already closed it, and a test
  now pins that.
