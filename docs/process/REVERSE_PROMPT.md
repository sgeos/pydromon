# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-11-19, rounds 18 and 19, the other project's recovery, all known issues and drivers

**The other project.** It was blocked pending a supported recovery and an independent
review. Round 18 gave both. `docs/architecture/UPGRADE.md` and `dromon upgrade-check` are
the supported path, and audit reviewed the diagnosis of its failed Codex starts, which
names three candidates, all fixed on main, and proves none for that installation. Its
later Codex `account/read` failure did not reproduce with its exact arguments a day
later. Three notices went by the mailbox. It has not replied, and it still ran 6567167
when last observed.

**Done, merged in pull requests 164 to 178.**

- **Security.** Four client-created Claude Code lines could list the home ssh directory
  and read the Codex sign-in file, which the round 18 report brief's confinement check
  found. Every Claude Code line now denies the shared credential paths in its sandbox and
  file tools, a line with no sandbox setting gets an enabled one, escape settings are
  refused, and `daemon.default_profile` gives client-created lines the project profile's
  commit guards. A live check on 2026-11-18 from six lines found all denied. Audit
  reviewed the change four times.
- **Runs and answers.** `dromon run status` overflowed the line cap and is bounded, with
  other answers. A queued run follows a commit of the same content, hashed from the files
  with hooks and filters off.
- **Processes.** A launch record whose start time cannot be read is retained, and a
  cancelled match closes its pipes, which a Linux CI failure exposed.
- **Mail.** The evidence index screens only quoted text, and lines can acknowledge many
  notes at once.
- **Session names.** Claude Code sessions are named by the operator's pattern, such as
  `PDr00-pydromon-runtime-Sn55hi`, configurable as `daemon.session_name`.
- **Drivers.** Every model Anthropic, OpenAI, xAI and Google released publicly between
  2026-08-21 and 2026-11-19 has a driver. GPT-6.2 Astra and Sol were added. Mythos 5.2
  and GPT-6 Cyber are restricted, so they were not counted as public.

**Team.** Anthropic seats owned every role from 2026-11-18T07:17Z, renewed to 21:26Z.
At about 20:30Z Claude's weekly window fell below 10 percent, and the roles failed over
to their OpenAI seats as designed. The operator's session returned them after the reset.

**Decided by the operator in session.** Session name rules, Sonnet's code `Sn`, the line
identifier as the variant, and a version code from the package version.

**Declined by the operator's session.** Locations in disclosure refusals, by the
operator's decision of 2026-10-30. A brief that asked for them was withdrawn unbuilt.

**Experience reports.** Rounds 18 and 19 from every Anthropic line that worked and from
capabilities-sol, in each worktree's `tmp/reports`, and the operator's in the main
checkout's `tmp/reports`. Most were satisfied. Requests not yet built are in the
handoff's Open list.

**Not verified.** A live Claude Code turn after the session name change, Linux, and the
GPT-6.2 drivers live. Several merges relied on continuous integration while memory
pressure held local gates.
