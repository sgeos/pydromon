Type: Changed

- A line's configuration no longer stores literal credentials in the event log. Each
  literal value of an MCP server's `env` or `headers`, and of a settings document's
  `env`, is sealed into an owner-only store beside the log, `events.sqlite.sealed`.
  The event holds `<sealed>` in its place, and a new `line_configured.sealed` field
  names each location with an HMAC-SHA256 of the value under a key only the store
  holds. The engine is built with the values put back. A missing or altered store
  fails the line's turn, naming each location and never a value. A configuration that
  gives `<sealed>` as a value is refused. References such as `${NAME}` are kept as
  written.
- Nothing is migrated. A configuration stored before sealing keeps its literal values
  in the log. `dromond` names each such line at start, and the daemon status lists them
  as `event_log.unsealed_lines`. Configuring the line again seals it.
- Rollback floor. A daemon older than this branch refuses a log holding a
  `line_configured` with `sealed`. `EVENT_LOG.md` records it in the rollback table, and
  marks the round 14 mail and role changes as merged in #125.
- `EVENT_LOG.md` records start time and memory measured on synthetic logs of 3.3, 33
  and 330 MiB, which open in about 0.12, 1.1 and 12 seconds. It also gives a design for
  a bounded start from a verified snapshot, which is not built.
