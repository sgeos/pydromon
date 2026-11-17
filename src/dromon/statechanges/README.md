# State changes

A branch that changes what applying an event means, or that changes the codec's types or
the state the fixed history of `tests/conftest.py` replays to, adds one file here. The
file is `BRANCH.txt`, where BRANCH is the branch name with each `/` written `--`, as a
changelog fragment in `changes/` is named. It says in prose what changed. Its name is
what counts. `dromon.statesnapshot.STATE_VERSION` is the SHA-256 of the sorted names,
one per line, so a snapshot written by code with another set of changes is ignored.

Each file is the branch's own, so two branches never conflict here, and their merge
holds both. The state those two changes give together is new, so the merge needs its own
pin in `tests/state_pins/`, which `tests/test_state_version.py` asks for by name and
content when it fails. A pin is also one new file, so adding it cannot conflict either.

`base.txt` holds the history of the whole numbers 1 to 6 that this set replaced in round
17. A file is never renamed or removed, since that would change the version of code that
did not change.

Any other entry, such as a merge tool's `base.txt.orig`, is left out, and the gate names
it in a warning, so a leftover file never stops the package from importing. A set that
is empty, too large or unreadable makes the version `unusable`, and the daemon then
replays the whole log rather than trusting any snapshot.

Discovery reads at most 4097 directory entries and sorts at most 4096, including
hidden entries and package files. It retains at most 128 ignored names. Exceeding
either limit makes the set unusable, so an arbitrary prefix never becomes a trusted
version. Within these limits, warnings are sorted by name. The limits bound entry
counts rather than name bytes or filesystem latency. A directory changed during the
read may still give an inconsistent view. Deployment must keep package files stable.

State change discovery slices `os.scandir` directly and closes the scanner after
reading, including on overflow or a read error. It does not use `Path.iterdir`, which
can allocate the full directory before yielding on supported Python versions.
Non-filesystem package resources make snapshots unusable because their enumeration
bounds are unknown. The entry limits are unchanged. The event log remains usable
through full replay in that case.
