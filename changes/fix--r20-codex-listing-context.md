Type: Fixed

- Codex server listings use each line's working directory and filtered launch
  environment, with executable lookup from the line's search path. Root-only servers
  no longer become disabling overrides in a worktree that does not define them.
  Missing or malformed listed transports refuse startup before connection rather
  than receiving an invented transport. The Codex protocol records the configuration
  race that remains and the inspected behavior of the other engines.
- Codex executable lookup ignores relative and empty search entries and refuses
  executables in line-writable roots, including symbolic-link targets. Both launch
  commands filter unsafe interpreter search directories. Invalid server names are
  identified by their listing position without copying their text into refusals.
- Engine startup checks print screened named failure reasons without raw exceptions
  or captured standard error. The operator client withholds unrecognized child
  diagnostics. Codex validates and bounds every page of the started process's
  server status before a prompt. The protocol records the startup side-effect
  window that this observation cannot prevent.
- The Codex launch smoke check supplies its generated stand-in explicitly to the
  shared process wrapper. Normal lines retain executable path validation.
