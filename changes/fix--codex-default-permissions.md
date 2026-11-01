Type: Fixed

- Codex lines start again under Codex 0.166.0. A round 14 change overrode every MCP server
  with a whole table holding a command, which Codex refused for servers defined by `url` and
  then fell back to defaults that failed `account/read`. Dromon is back to the sparse
  `enabled=false` override, and also names its own permission profile as `default_permissions`.
