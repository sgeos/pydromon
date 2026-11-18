Type: Fixed

- A line setting that leaves the engine's configuration and model as they were, such
  as a limit or the wake cadence, no longer discards the engine, so it no longer closes
  a kept session or stops its background tasks. Round 18, item 3.
- An operator's note to a seat that waits because another seat owns its role is still
  recorded, and the `send` result and `dromon send` now say why it waits and name the
  owner. Round 18, item 4.
- `list_sent` and `dromon sent` call a note unanswered exactly when the recipient's
  count does, and show a note left open that asks no reply as `asks no reply`, so the
  sender's and the recipient's views agree. Round 18, item 5.
