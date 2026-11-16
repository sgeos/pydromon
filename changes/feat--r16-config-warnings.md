Type: Added

- Round 16, at the operator's request. `dromon config` warns of a Claude Code line
  with `workdir_access = "read"` that every turn would refuse, because it has an MCP
  server, an allow-all policy or an allowed `mcp__` tool, project settings or plugins,
  or command hooks, which Claude Code cannot confine to a read-only directory. The
  reason is the one the engine builder gives, from one shared function.
- `dromon config` warns of a permission rule in the project's settings or a line's that
  names an absolute path for a file tool with one leading slash, as `Read(/etc/x)`,
  which Claude Code reads as relative to the project and so matches nothing. Two
  slashes, as in `Read(//etc/x)`, name an absolute path. The warning names the rule and
  the corrected one, and says that a deny rule that matches nothing protects nothing.
- `dromon mail` and `dromon fleet mail` say which matches a page holds, and how many
  unread messages come after the page and before it, with the `--offset` that shows
  them, from new `unread_after` and `unread_before` fields of the `mail` answer. An
  operator who read the tail of a page of the oldest messages no longer misses the
  newest ones without a sign of them. An older daemon that sends neither field still
  gets the line naming the matches shown.
