# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, pydromon dogfoods itself

**Done.** On the operator's goal that the project be in order to dogfood its own
development.

- **Local setup.** `.dromon/dromon.toml` declares `worker` on Opus, `reviewer` on Haiku
  and `luna` on GPT-6-Luna, and `.dromon/LINES.md` gives their rules. Lines never
  commit, push or use `gh`, write only when given the task, run the sandbox form of the
  gate, and never spend a subscription or start a daemon. `docs/process/DOGFOOD.md`
  records both files with placeholders for the absolute paths.
- **A defect found and fixed.** pytest's collection stats every entry of the root
  directory, and a Codex line's profile denies even the metadata of `.dromon`, so a
  Codex line could not run the gate here. pytest now ignores `.dromon`, and a test ties
  the option to Dromon's state directory name. A Codex line also needs uv's cache under
  `$TMPDIR`, which the rules give it.

**Verification.** The daemon runs for this checkout. Through it, a Haiku line ran the
hygiene gate, was refused a `.git` write and a commit, and opened Appendix B without
reading it, and then passed the sandbox gate with 1153 tests. A GPT-6-Luna line passed
the same gate with 1154 tests after the fix. The failure was reproduced and the fix
checked with macOS's sandbox denying `.dromon`. The full gate passed locally with 1170
tests. No Opus turn was run.

**For the operator.** The daemon is left running, so lines can be attached with
`uv run dromon attach worker`. It runs this working tree and picks up merged changes
when restarted with `dromon stop` and `dromon start`.
