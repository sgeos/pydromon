# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, dogfooding in epagoge with its local teacher

**Done.** On the operator's direction to review the epagoge project and bring Dromon to
the point where it can nominally be dogfooded there, with the ability to spin up the
local teacher model.

- **Review, facts.** Epagoge's generators all call a local teacher,
  `qwen3:30b-a3b-instruct-2507-q4_K_M`, served by Ollama on `127.0.0.1:11434`, at a
  declared context of 4096. Its `CLAUDE.md` carries obligations every agent must
  follow, including a disclosure discipline whose scan reads untracked files. Ollama
  0.40.10 was already serving through the desktop application, with the model pulled.
- **Gap, fact.** A Dromon Claude Code line ran with an empty system prompt and read no
  `CLAUDE.md`, so it could not follow a project's rules. Lines now take
  `claude_code_prompt` and `instructions`.
- `dromon teacher up`, `status` and `down` bring the teacher up and release it, as
  `docs/protocol/TEACHER.md` specifies. Dromon never pulls a model and stops only a
  server it started.
- Epagoge has a local `.dromon/dromon.toml` with a `worker` on Opus 5.5, a `reviewer`
  on Haiku 5.5 and a `luna` line on GPT-6-Luna, and `.dromon/` was added to its
  `.git/info/exclude`. No tracked file of epagoge changed, and its tree stayed clean.
- **Verified live in epagoge.** With the teacher loaded and `dromond` running, the
  `reviewer` line queried the teacher from its sandboxed shell and answered from
  `CLAUDE.md`, the `luna` line answered from its instructions, and both replied to the
  operator by mail. The daemon was then stopped and the teacher released.

**Decisions made by this line, open to the operator's review.**

- The teacher is not autostarted in epagoge's file, because loading it takes about
  19 GB on a 32 GB host. Free memory fell to 14 percent while it was loaded.
- Epagoge lines may not commit or push, as an `ask` rule that denies, since that
  project leaves both to the operator.
- The Claude lines read the operator's global `~/.claude/CLAUDE.md` as well as the
  project's, as Claude Code itself would.

**Not done.** No generator was run, and the epagoge gate was not run by a line. Whether
loopback needs `allowLocalBinding` is untested. A Codex line cannot reach the teacher.

**Verification.** 819 tests passing outside the sandbox with warnings treated as errors,
and all 8 live tests passing. Ruff and pyright strict clean.
