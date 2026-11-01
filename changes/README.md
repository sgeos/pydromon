# Changelog fragments

A branch records its changelog entry here and does not edit `CHANGELOG.md`. Every branch
used to add under the same headings of the Unreleased section, so almost every merge
conflicted there. One file per branch cannot conflict with another branch's.

## A fragment

The file is `changes/BRANCH.md`, where BRANCH is the branch name with each `/` written
`--`. The branch `feat/changelog-fragments` writes `changes/feat--changelog-fragments.md`.
Use lowercase letters, digits, dots, hyphens and underscores.

```
Type: Fixed

- What changed, in the changelog's own form. A bullet begins with a hyphen and a
  space, and its continuation lines are indented two spaces.
```

The first line is `Type: Added`, `Type: Changed` or `Type: Fixed`. A blank line follows
it, and then one or more bullets. A fragment holds no headings. A branch with entries of
two types writes two fragments, for example `feat--thing.md` and `feat--thing--fixed.md`.

## The branch rule

`scripts/check.sh` runs `scripts/assemble_changelog.py check`. It validates every
fragment, and it refuses a branch that edits the Unreleased section of `CHANGELOG.md`,
whether or not the branch also adds a fragment. Two edits are allowed, because neither
adds, removes or changes an entry.

- A release adds a version heading, leaves Unreleased empty and keeps every entry, as
  the next section describes.
- A lossless regrouping leaves Unreleased exactly as the `collapse` command would write
  it, which tidies repeated headings once. A section already tidy has nothing to regroup.

The rule compares with the merge base of `HEAD` and `origin/main`, then `main`. With
neither, as in a clone of depth one, the gate prints `SKIPPED` and the script exits 3. It
does not report a pass. Continuous integration fetches the full history so that
`origin/main` exists there.

## A release

The release is made by the operator's session, in one commit.

```sh
python3 -I scripts/assemble_changelog.py preview
python3 -I scripts/assemble_changelog.py release 0.1.0 2026-11-15 --dry-run
python3 -I scripts/assemble_changelog.py release 0.1.0 2026-11-15
```

`preview` prints the entries of the next release without writing. `release` writes the
section `## [0.1.0] - 2026-11-15` below an empty `## [Unreleased]`. It holds the entries
already in Unreleased, followed by every fragment in name order, grouped under one
`Added`, `Changed` and `Fixed` heading, and it deletes the fragments it used. It refuses
a version already in the changelog and a date that is not a calendar date. Commit the
changed `CHANGELOG.md` and the deleted fragments together.

The release is atomic in this sense. It writes the new changelog to a temporary file,
moves the fragments to `changes/.released-0.1.0`, replaces `CHANGELOG.md` in one step,
and then removes that directory. A failure before the replacement undoes the moves and
leaves nothing changed. A failure removing the directory leaves a finished release and a
warning to remove it by hand, and no command reads it as fragments.
