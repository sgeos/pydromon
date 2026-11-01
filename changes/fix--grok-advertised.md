Type: Fixed

- Grok processes disable subagents and disable web fetch unless the line has
  WebFetch. Sessions are replaced when their advertised tool switches change.
  Unsupported tool calls retain individual denials with continuation only when
  later updates explicitly confirm non-execution.
