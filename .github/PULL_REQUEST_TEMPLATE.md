## Summary

<!-- One or two sentences on what the change does and why. Use `Fixes #N` to link an issue. -->

## Type of change

- [ ] Feature
- [ ] Fix
- [ ] Documentation
- [ ] Tests
- [ ] Maintenance, such as continuous integration, packaging or process

## Checklist

- [ ] `./scripts/check.sh` exits zero, or the sandbox form `-m "not socket and not live"` did and the note below says why
- [ ] Live tests were run for a change to an engine adapter, or the note below says why not
- [ ] The specification under `docs/protocol/` matches any behaviour this changes
- [ ] Any figure quoted in the change was measured on this tree, or the same sentence says it was not
- [ ] No check was weakened to make this pass. If a check is wrong, the change says so and fixes it deliberately
- [ ] Nothing from `secret/`, `tmp/` or `.dromon/` is included
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` if a user would notice the change

## Notes for the reviewer

<!-- Alternatives considered, places where a second opinion would help, or anything unverified. -->
