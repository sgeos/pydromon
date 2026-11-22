Type: Fixed

- Codex server listings use each line's working directory and filtered launch
  environment, with executable lookup from the line's search path. Root-only servers
  no longer become disabling overrides in a worktree that does not define them.
  Missing or malformed listed transports refuse startup before connection rather
  than receiving an invented transport. The Codex protocol records the configuration
  race that remains and the inspected behavior of the other engines.
