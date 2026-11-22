# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-22, round 20, the other project first, all known issues and drivers

**The other project.** Every message it sent after 2026-11-06 had gone unread, because
the interline tool's `inbox` shows only the oldest twenty unacknowledged messages and
pydromon acknowledged none. The operator's session read all 213 through the store,
triaged them with six agents, acknowledged them, and answered requests 536, 539, 548 and
570. Its Codex lines were blocked by three pydromon defects, now fixed on main.

- **An oversized resume frame**, "Separator is not found, and chunk exceed the limit",
  held its Lead, Core and Review lines. Resumes now request `excludeTurns`, an oversized
  frame ends the turn recoverably, and unsubmitted mail is requeued. Pull request 185.
- **The listing context.** Servers were listed in the daemon's directory and overridden
  in the worktree's. They are now listed in each line's context, and Codex is resolved
  only outside line-writable roots. Pull request 183.
- **The account failure.** The other project bisected it to one bare
  `mcp_servers.S.enabled=false` for a server its project configuration defines. Disable
  overrides are now complete placeholder tables, which the operator confirmed live from
  its directory. Pull requests 188 and 189. The operator's session had first called this a
  transient provider failure, which was wrong.
- **Also for it.** An upgrade check that warns for dormant Grok profiles, 182. A no-model
  account check, 184. Counter epochs, 186.

**Drivers.** Gemini 4 Neon, released 2026-11-18, has a Pi driver. Cursor Agent and
Antigravity list no identifier for it locally. No other pertinent release since
2026-11-19.

**Team.** From 2026-11-22T07:14Z OpenAI seats own every role, grok-review is
hibernated, and Anthropic seats are fallback only through round 21.

**Experience reports.** All seven OpenAI seats reported SATISFIED, in each worktree's
`tmp/reports`. Every confinement check was denied. The operator's report is in the main
checkout's `tmp/reports`. Their top requests were immutable gate snapshots and visible
queued-run lifecycles.

**Found broken and fixed.** After pull request 188 every Codex line failed its start check, because the
isolation check required a null runtime status and an `enabled` field, and inside a thread
Codex 0.171.0 reports a disabled server as "disabled" with no such field. The operator's
brief had specified the null status from a probe made without a thread. `dromon restart`
refused the new code, so the running daemon was untouched. The fix was verified live on
all seven Codex lines before merge.

**Not verified.** The other project has not upgraded, so no fix has run there. GitHub's
macOS runners delayed every merge by hours, and local full gates were often held by
memory pressure, so several merges rested on continuous integration and sandbox gates.
