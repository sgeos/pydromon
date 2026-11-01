Type: Fixed

- Grok processes disable subagents and disable web fetch unless the line has
  WebFetch. Sessions are replaced when their advertised tool switches change.
  Unsupported tool calls retain individual denials with continuation only when
  later updates explicitly confirm non-execution.

- Grok session digests cover all tool grants. A replacement identifier survives a
  refused turn in the adapter, and a replacement without a valid identifier has
  one attempt per digest on that adapter. Rejected calls without a confirming
  update hold the line at turn end and name the affected tool.

- Generated Grok configuration includes a fixed denial hook for todo_write and
  spawn_subagent. Processes without WebFetch receive its command-line deny rule.
  The hook program must be outside line write paths and protected by the sandbox.
  Grok documents hook failures as fail open. Live enforcement remains unverified.
