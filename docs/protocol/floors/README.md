# Rollback floor fragments

A branch that adds a one-way change to the event log, a kind, a field or a value that
an older daemon refuses, records it here and does not edit the rollback floor table in
[`EVENT_LOG.md`](../EVENT_LOG.md). Every such branch used to append a pending row at the
end of that table, so two of them conflicted at every merge, and rows stayed pending
long after they merged. One file per branch cannot conflict with another branch's.

The file is `BRANCH.md`, where BRANCH is the branch name with each `/` written `--`. It
holds one or more entries, separated by a blank line.

```
Change: `run_started.tree`, written whenever a run in a working directory starts with its tree readable
Search: _optional_str(data, "tree")
Path: src/dromon/codec.py
```

`Change` is the table's first column, on one line. `Search` is text that the change's
first commit added to `Path`, so that `git log -S` finds the commit. `Path` defaults to
`src/dromon/codec.py`, and a value added to an enumeration names `src/dromon/model.py`.
`Search` should name the event as well as the field, since a field name that two events
share finds the older event's commit.

**Review of `513928b`, finding 1.** The same holds for a value. The entry for the
expiry causes of `lease_released` searched for `EXPIRED = "expired"`, which another
enumeration of `model.py` held long before, so `dromon upgrade-check` placed the change
as older than revisions that cannot read it. A value's `Search` names a member that no
other enumeration holds, such as the entry's `LIMIT_LOWERED = "limit_lowered"`.
`tests/test_upgrade.py` compares the codec's kinds, fields and enumeration values with
those of an older revision, and fails when a kind that gained any of them is not named by
a change the floor check lists as newer than that revision.

**Review of `0db077c`, findings 3 and 4.** `Path` is relative to the repository, with no
`..`, and a path whose real form lies outside the repository, as through a link, is
refused, so a fragment cannot make the gate answer whether a file elsewhere on the
machine holds a string. A file here that is neither this README nor named `BRANCH.md` in
lowercase, such as `.DS_Store` or an editor's backup, is left out with a warning naming
it, rather than failing the gate. A fragment misnamed in another case is left out too,
and the warning is what shows it.

`scripts/check.sh` runs `python3 -I scripts/assemble_floors.py check`, which validates
the fragments and refuses a row of the table that is still pending or uncommitted. At a
release the operator's session runs `preview`, reads the commits it found, and runs
`release`, which writes each row with its first commit, the merge that brought it into
`main`, the branch and the pull request, and deletes the fragments, all in the release
commit. `scripts/assemble_floors.py` documents how each commit is found and where that
can go wrong.
