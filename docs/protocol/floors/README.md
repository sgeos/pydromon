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

`scripts/check.sh` runs `python3 -I scripts/assemble_floors.py check`, which validates
the fragments and refuses a row of the table that is still pending or uncommitted. At a
release the operator's session runs `preview`, reads the commits it found, and runs
`release`, which writes each row with its first commit, the merge that brought it into
`main`, the branch and the pull request, and deletes the fragments, all in the release
commit. `scripts/assemble_floors.py` documents how each commit is found and where that
can go wrong.
