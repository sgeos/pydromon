# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, sync with sgeos/project-template

**Done.** On the admin session's request, relayed and approved by the operator, and with
the operator's additions of README badges and a review of the Keleusma and angelssword
repositories.

- **Adopted as is.** `.gitattributes` and `.github/rulesets/main.json`. Every tracked
  file was already LF, so nothing was renormalised.
- **Adapted.** `SECURITY.md`, with pydromon's sockets, sandboxes, credential policy and
  isolation as its scope. `CONTRIBUTING.md`, with `uv`, the gate and the `socket` and
  `live` markers. The pull request template.
- **`scripts/check.sh`.** The template's hygiene checks, then `uv sync --locked`, Ruff,
  pyright, pytest with warnings as errors and its arguments, and shellcheck when
  installed. CI runs it with `UV_PYTHON` set, after a Linux shellcheck step that
  requires the tool.
- **CI.** The concurrency group is the ref for a pull request and the run identifier
  otherwise, and the matrix runs at most two jobs at once.
- **From Keleusma and angelssword.** Issue templates for bugs and features, `llms.txt`,
  README badges, and repository topics.
- **Settings, applied on the operator's approval and read back.** Merge commits only,
  branches deleted on merge, no wiki or projects, secret scanning and push protection,
  vulnerability alerts, the `main` ruleset, and topics.

**Declined, with reasons recorded in the proposal.** `paths-ignore`, because the merge
procedure waits on checks. `CITATION.cff`, for a prototype to be retired. A dependency
advisory job, pending a supply-chain decision on fetching a scanner at CI time.

**Reported for the template, not edited.** Opt-in sandbox and credential test markers,
issue templates and `llms.txt`, a shellcheck step that fails when the tool is absent, a
pinned language-setup example with `timeout-minutes`, topics in its settings step, and an
optional disclosure screen against a term list under `secret/`.

**Verification.** `./scripts/check.sh -m "not socket and not live"` passed inside the
sandbox, and the full gate outside it. CI on the pull request runs the new workflow for
the first time.
