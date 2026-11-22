Type: Fixed

- Codex disables listed servers with complete placeholder tables of their own
  transport kind. This avoids a transportless command-line layer that the operator
  reproduced as an account initialization failure for a project-defined server.
  Real endpoints and credentials never enter the generated disabling overrides.
- Post-start server checks require explicit null runtime status before prompting.
  The protocol records the operator evidence and remaining startup observation limits.
