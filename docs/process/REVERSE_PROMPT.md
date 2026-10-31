# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-30, Grok lines in a team, and drivers for Sol and Terra

**Done, merged in pull requests 94 and 95.**

- **Grok in a team.** Another project running Grok lines in worktrees found three
  defects. Dromon refused the empty `hooks-paths` file and the empty lock in
  `installed-plugins` that Grok writes on every start, so every second turn failed.
  The writer profile named a worktree's `.git` file, which Grok refuses. The
  suggested profiles denied no credentials. Dromon now admits only those two empty
  records, holds a worktree's git directories read-only, and requires the operator's
  SSH, Codex, Cargo and Pi credentials, and AWS when present, to be denied by absolute
  path. A refusal of a profile shared by several projects names only the entries to
  add.
- **Sol and Terra.** Codex drivers for `gpt-6.1-sol` and `gpt-6.1-terra`, for every
  strategy, with the catalogue's context window of 500,000 tokens and Luna's other
  settings.

**Outside the repository.** Dromon's shared Grok profile gained denials for the
Cargo and Pi credentials, with a backup beside it. The checks of pull request 95,
run offline against that profile and the other project's Grok worktree, pass for the
reader and the writer, and the home check passes with Grok's empty records present.

**Verified.** Full gates outside the sandbox and continuous integration on both pull
requests. The daemon was restarted onto `b0cd881`.

**Not verified.** A Grok turn on the new code, and any turn on Sol or Terra. The
other project's daemon still runs code from before pull request 95 until it is
restarted.

**Also this session.** A stray attach client from 2026-10-25, whose project had been
deleted, used a full core until it was stopped. An attach client that loses its
daemon should exit, which is not yet fixed. Dependabot showed no alerts.
