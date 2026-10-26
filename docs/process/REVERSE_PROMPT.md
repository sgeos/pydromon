# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, the harness ready for Keleusma, and installed for kaizen

**Done.** On the operator's goal that pydromon be in order for its session to direct
further development through Dromon, and that the harness reach a standard where
Keleusma's four lines can run on it.

- **Directed work.** pydromon's Opus `worker` line inspected Keleusma and built four
  stages, which its Haiku `reviewer` line reviewed. The operator's session verified
  each live and merged pull requests 35 to 40. The record is
  `docs/architecture/KELEUSMA_DOGFOOD.md`.
- **Verified live.** Working directories, git guards, scheduled wakes, leases,
  `require_sandbox`, the disclosure guard, rebinding, project settings, plugins and a
  Fable 5.1 probe. A read-only trial ran Keleusma's four lines in their worktrees, and
  a writing trial ran kaizen in a private clone through lease, background build, wake,
  commit and push.
- **Installed, on the operator's decisions.** Keleusma's `.dromon/dromon.toml` for the
  kaizen line, excluded from git and accepted by `dromon config`, and an untracked
  `secret/disclosure-terms.txt`. The remote stays on SSH. No daemon was started.
- **Reports.** The lines' harness reports are in `tmp/harness-report-*.md`, and the
  trials' reports in `tmp/keleusma-trial-reports.txt`.

**Not done.** No real Keleusma line has written under Dromon yet, because kaizen's own
live session was still running, and the operator starts the daemon once it ends.

**For the operator.**
1. Start Keleusma's daemon for kaizen when its live session has ended, as
   `KELEUSMA_DOGFOOD.md` shows, and decide when the other three lines join.
2. Move the remote to HTTPS when the lines should push.
3. The lines' top suggestions: let a line list and stop its own processes, and show a
   line its own holds, limits and leases.
