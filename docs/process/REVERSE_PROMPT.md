# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, the harness brought to Keleusma, through the dogfooded harness

**Done.** On the operator's goal that pydromon be in order for its session to direct
further development through Dromon, and that the harness be brought to a standard
where Keleusma's four lines can run on it.

- **Directed work.** The Opus line `worker` inspected Keleusma read only and ranked the
  gaps, then built three stages. The Haiku line `reviewer` reviewed each, and its
  findings were fixed. The operator's session verified each stage live and merged pull
  requests 35 to 38. The record is `docs/architecture/KELEUSMA_DOGFOOD.md`.
- **Built.** Development-scale turn budgets, per-line working directories with session
  adoption and a git hooks and config guard, `wake_after`, `require_sandbox`, a
  disclosure guard, operator-only rebinding, allowlisted project settings, leases, and
  Fable 5.1 drivers.
- **Tried.** A scratch project ran Keleusma's four lines on Haiku in their real
  worktrees, read only. Each identified its line and task, the lease was contended as
  designed, and nothing in Keleusma was written.
- **Validated.** A Keleusma project file passes `dromon config` against the real
  worktrees, and is recorded with placeholder paths.
- **Reports.** The three lines' harness reports are in `tmp/harness-report-*.md`, and
  the trial's reports in `tmp/keleusma-trial-reports.txt`.

**Corrections to the lines' reports.** The worker's holds were cost holds, not
autonomous-turn holds. The reviewer's duplicate request was a deliberate resend after
its first turn failed to start. Luna's intermittent collection failure was the
`.dromon` defect, fixed by pytest's `--ignore`.

**Verified live afterwards, 2026-10-26.** In a scratch project, `require_sandbox`
refused an unsandboxed line, the disclosure guard refused a line's message without
naming the term, a project skill loaded and a project hook refused its turn, a rebind
from Haiku to Opus answered as Opus, and the `plugins` key loaded Keleusma's required
skill. In a private clone of Keleusma, a writing kaizen line took the lease, built a
crate in the background with two scheduled wakes, committed in its worktree, pushed to
a local remote, and released the lease. Stage 4, the plugin opt-in, merged with its
review findings fixed.

**Not verified.** No live turn ran on Fable 5.1, which the project file does not use.
The real Keleusma lines have not run as writing lines, because their live sessions work
in the same worktrees, and installing the file is the operator's decision.

**For the operator.**
1. Whether to install the Keleusma project file, which stops the live sessions of
   those lines, and whether those lines keep their authority to commit, push and merge.
2. Keleusma's remote over HTTPS, and `secret/disclosure-terms.txt` there.
3. The lines' top suggestions: let a line list and stop its own processes, and show a
   line its own holds, limits and leases.
