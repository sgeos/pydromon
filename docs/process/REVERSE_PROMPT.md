# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-25, housekeeping and the Appendix B check

**Done.**

- **Appendix B.** Read in full. Every tracked file, every diff in history and every
  commit message was searched for its subject matter and read by hand. Nothing conflicts.
  Every keyword hit was an ordinary use, such as an adapter that probes an engine. The
  Keleusma properties the documents rely on are already in Keleusma's public README.
- **Dates.** The seed's dates are correct in Coordinated Universal Time. Its commits were
  made after 00:00 UTC on 2026-10-25, while their local Pacific timestamps read
  2026-10-24. `CLAUDE.md` now states the convention. No date was changed.
- **Integration document.** Updated from the `pykeleusma` repository, read without
  changes, at its commit `03d2309`. The four questions now carry that line's answers,
  the mapping shows its tasks 2 and 3 done and the new task 5b, and the examples no
  longer use one-element tuples, which Keleusma rejects.
- **Repository.** Local `main` matched `origin/main`. A stale tracking reference to the
  merged branch `docs/keleusma-integration` was pruned, since the remote branch was
  already deleted. The lock file is current.

**Not verified.** The `pykeleusma` line's answers were taken from its report and were
not reproduced here. The disagreement on the worst-case time unit is recorded, not
resolved.

**Verification.** `uv sync`, 18 tests passing, ruff check and format clean, and pyright
strict with 0 errors and 0 warnings.

**Operator decisions recorded.** The protocol specification is drafted here and ported
later. The first adapter targets Claude Opus 5.5, and the loop that drives it stays open
until task 4. The mailbox is chosen when needed.

**Next.** Task 2, the data model and event log.
