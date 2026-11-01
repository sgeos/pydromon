Type: Added

- Changelog fragments. A branch adds one file, `changes/BRANCH.md`, with a type of
  Added, Changed or Fixed and its entries, and does not edit `CHANGELOG.md`, which
  conflicted on almost every merge because every branch added under the same headings.
  `scripts/assemble_changelog.py` assembles the fragments at a release, previews them
  and collapses the Unreleased section's repeated headings, which it did once for the
  existing entries without losing any. `scripts/check.sh` validates every fragment and
  refuses a branch that edits the Unreleased section and adds or deletes no fragment.
  `changes/README.md` and the Git Workflow section of `CLAUDE.md` state the convention.
