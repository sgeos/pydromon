# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-01, rounds 12 and 13

**Done, merged in pull requests 111 to 120.**

- **Federation.** Local and remote federation can be open or limited to an allowlist,
  with TLS through a pinned `pyopenssl`.
- **Operator commands.** `dromon create` takes limits, kept session and compaction,
  `dromon configure` takes `workdir_access`, and `dromon config` shows every table it
  validates. The gate prints each skipped test's reason.
- **Disclosure.** A refusal names at most one field and never a term or a line. The
  operator can accept an occurrence by file and line hash.
- **Fleet.** A per-user index of daemons and `dromon fleet` commands.
- **Refusals.** An ACP call that is rejected and reports that it did not run continues
  the turn, and stop notes use fixed words.
- **Restart and landing.** `dromon restart` runs the installed code's start checks
  against a snapshot of the live log before the old daemon stops, and `dromon land`
  takes a line's tree to a pull request.
- **Peer state.** A line sees each other line's state, hold cause, unread age and live
  leases, bounded, with nothing from notes or questions crossing.
- **Changelog fragments.** One file per branch under `changes/`, assembled at release,
  with a gate rule that continuous integration now enforces.
- **Team.** Three new roles, `ops` on Opus 5.5, `audit` on Sonnet 5.5 and `grok-review`
  on Grok 4.7. `engines` moved to GPT-6.1-Sol.

**Verified.** Full gates outside the sandbox and continuous integration on every pull
request, with reviews and re-reviews by the audit and review lines. The restart onto
`65d4ffa` passed its preflight, and a Grok turn ran after the fleet index was denied in
its profile. The changelog rule ran with a base in continuous integration.

**Not verified.** The items under Unverified live in `HANDOFF.md`.

**Also this session.** Every line and the operator wrote a round 13 report. Five of
seven lines are satisfied, up from two of five in round 11. The operator is not, and
names a declared full-gate run as the change that would most move it.
