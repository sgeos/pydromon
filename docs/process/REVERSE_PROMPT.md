# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-27, the rest of the T3 Code list

**Done.** The Opus `worker` line wrote it, the Haiku `reviewer` line reviewed it, and
two fix rounds followed.

- **Failures.** A failed turn holds its line and asks an operator. Instruction paths
  resolve against the project root and are checked at load.
- **Evidence.** `dromon probe --record` and `--scrub` give replayable fixtures. The
  first is a scrubbed live Claude Haiku 5.5 probe.
- **Data.** Engine capabilities are tables of yes, no or unknown, with a minimum
  version. Cost keeps unknown apart from zero. Each turn records its isolation.
- **Mail.** Request keys stop a retried call from acting twice. A rebind or a cleared
  session carries a bounded handoff of recent mail.
- **Status.** Event-log size and growth, and `dromon probe --no-turn`.
- **Declined.** Workspace capture, with reasons in `DESIGN.md`.

**Not verified.** No live turn ran except the recorded probe. Request keys are held only
in memory. The Grok, Codex and Antigravity capability rows are unknown.

**Harness finding.** Mail reaches a line only between turns, so a line on a long
multi-item turn cannot be redirected until it stops. Two operator messages waited
through six items.

**Earlier today.** The bounded engine waits and the stop policies are merged, and the
stop policies are verified live on macOS, as `LINE_CONFIG.md` records.

**Next.** Fill the Grok, Codex and Antigravity rows and their recording, then the
lines' experience reports.
