Type: Fixed

- The post-start isolation check refused every Codex line, because Codex 0.171.0 sends no `enabled` field in an MCP status entry and reports a disabled server inside a thread with `runtimeStatus` "disabled". A missing `enabled` and a status of null or "disabled" are now accepted. A present `enabled` must still be exactly false, and any other status still refuses.
