# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, second sync with sgeos/project-template

**Done.** On the operator's request, with the code of conduct as the priority, a sync
with the template at `f829df4`, a check of the GitHub repository, and an audit of the
template.

- **Adopted.** `CODE_OF_CONDUCT.md` verbatim, linked from the README, `CONTRIBUTING.md`
  and `llms.txt`. `.github/CODEOWNERS`, `.github/release.yml`, the issue template
  `config.yml`, `docs/decisions/TEMPLATE.md`, `scripts/worktree.sh` and the opt-in
  `scripts/hooks/pre-push`. The template's `.editorconfig`, `.gitattributes` and
  narrower `.claude` ignore rules.
- **Merged.** `scripts/check.sh` takes the template's hygiene section whole and keeps
  pydromon's project checks and its argument pass-through to pytest. CI grants no
  permissions by default, installs shellcheck on macOS where missing, and gains the
  template's actionlint and zizmor job. zizmor reported no findings locally.
- **Commit trailer.** Dropped on the operator's instruction in session, which confirmed
  what the template states. `CLAUDE.md` and `AGENTS.md` say so.
- **GitHub.** Settings read back as the template prescribes. The labels `breaking` and
  `ignore-for-release`, which `release.yml` uses, were missing and were created.
- **Local only.** `secret/disclosure-terms.txt` now holds the disclosure screen terms,
  so the gate scans every tracked file. One term of the usual screen is left out,
  because it matches a dependency name in `uv.lock`.

**Verification.** The full gate passed outside the sandbox with 1169 tests, and the
sandbox form inside it. `scripts/worktree.sh` was tried inside the sandbox. Its default
location fails, and a base under `tmp/` works, which `CLAUDE.md` now records.

**For the operator.** Dependabot pull request 20, a bump of `astral-sh/setup-uv` to
10.3.0 with green checks, is still open for review. The new grouping may supersede it.
A draft message to the admin session with the template findings is in
`tmp/admin-message.md`, which is not tracked.
