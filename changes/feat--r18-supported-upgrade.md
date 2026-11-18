Type: Added

- Round 18, item 1. `dromon upgrade-check --from-log FILE` opens a copy of a
  project's event log and project file under the installed code, with no daemon and
  no engine, and reports each check apart, whether the log replays, whether its
  snapshot is used, whether `dromond --check` passes on the copies, which lines come
  back held, and, with `--from-commit`, the one-way changes whose rollback floor the
  old revision lacks. `docs/architecture/UPGRADE.md` is the supported procedure for
  moving a project from an old pinned revision, and back. It is tested against a log
  written by the code of 6567167.
- Round 18, item 3. `scripts/state_pin.py` writes the pin a merge of two state
  changing branches needs, from the computation the state version test imports, and
  never replaces a pin.
