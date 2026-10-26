# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, prospective engine adapters for Pi, Cursor Agent and Antigravity

**Done.** On the operator's goal that the dogfooded lines build drivers for Claude
Code, Codex, Cursor Agent, Antigravity and Pi, to the extent reasonable, so that each
can be tested when resources allow.

- **Work.** The Opus `worker` line wrote `pi.py`, `cursor.py`, `antigravity.py`, the
  shared `jsonlines.py`, nine Keleusma drivers and a protocol document for each engine.
  The Haiku `reviewer` line reviewed each adapter, then re-reviewed every fix, and the
  worker fixed what it found. Claude Code and Codex were already adapted and verified
  live, and no gap was found in them.
- **Basis.** Pi 1.4.0 and Cursor Agent 2026.10.01 are installed, and their help,
  documentation and installed code were read, but no turn was run. Antigravity is not
  installed, so its adapter rests on its published documentation alone, and its model
  name is a guess.
- **Operator decisions taken in session.** Cursor and Antigravity cannot be fully
  isolated, so daemon lines on them are refused. `dromon probe` runs one turn behind
  `--accept-partial-isolation`, and Antigravity also needs
  `--accept-unverified-credential`, since its stream names no credential. Pi's Claude
  plan sign-in is refused, because whether a plan may be drawn through a third-party
  harness is not established.

**Not verified.** No adapter has run against its real tool. Each of `PI.md`,
`CURSOR.md` and `ANTIGRAVITY.md` lists what to verify first.

**For the operator.**
1. Before a live Pi test, confirm that the ChatGPT plan's terms allow its use through
   Pi, then sign in once in Pi's dedicated agent directory, per `PI.md`.
2. Run `dromon probe` with the flags only after working through the verify-first list.
3. Start Keleusma's daemon for kaizen when its live session has ended.
