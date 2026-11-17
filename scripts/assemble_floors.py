#!/usr/bin/env python3
"""Rollback floor fragments, and their assembly into the table at a release.

**Why. Round 17, item 3.** Every branch that added a one-way change to the event log
appended a row at the end of the rollback floor table in
``docs/protocol/EVENT_LOG.md``, marked pending, so two such branches conflicted there
at every merge, and rows stayed pending or uncommitted long after their merge, since
nobody returned to them. A branch now adds one file, and a release writes the rows
with the commits git names, so nothing is written by hand that git already knows.

**A fragment** is ``docs/protocol/floors/BRANCH.md``, BRANCH the branch name with each
``/`` written ``--``, as ``changes/`` names a changelog fragment. It holds one or more
entries separated by a blank line, each of two or three lines.

    Change: `run_started.tree`, written whenever a run in a working directory starts
    Search: _optional_str(data, "tree")
    Path: src/dromon/codec.py

``Change`` is the table's first column, one line. ``Search`` is text that the commit
first writing and reading the change added to ``Path``, which defaults to
``src/dromon/codec.py``, so ``git log -S`` finds that commit. ``README.md`` is not a
fragment.

**Commands**, run from anywhere in the repository with the standard library only.

* ``check`` is the gate's rule. It validates every fragment, requires each ``Search``
  to be present in its ``Path`` as the tree holds it, and refuses a table row whose
  commit or floor says pending or uncommitted, since such a row is a fragment's work.
  It reads no history, so it runs in a shallow clone.
* ``preview`` prints the rows a release would write, with their commits, and writes
  nothing. A change not yet committed shows ``Uncommitted``, and one not yet merged
  into the base shows ``Pending``.
* ``release`` appends the rows to the table in the order of their floors and deletes
  the fragments. It refuses while any row would be uncommitted or pending, so it runs
  on ``main`` after the branches merged.

**How a row is found.** The first commit is the oldest commit of ``HEAD`` whose change
to ``Path`` changes how often ``Search`` occurs, as ``git log -S`` reports. The floor
is the oldest commit on the first parent line of the base, ``--base``, by default
``origin/main`` and then ``main``, that contains the first commit, which is the merge
that brought it in. The branch and the pull request come from that merge's subject
where it is GitHub's or git's own, and otherwise from the fragment's name.

**Failure modes.** A ``Search`` that an earlier commit already added and removed finds
that earlier commit, so choose text unique to the change, and read ``preview``. A
rebased or squashed branch moves the first commit, and ``preview`` shows the one the
history holds. **Porting note.** Plain text and git's own output, with no clock.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple

DIRECTORY: Final = Path("docs/protocol/floors")
TABLE_FILE: Final = Path("docs/protocol/EVENT_LOG.md")
HEADER: Final = "| Change | First commit | Floor on `main` | Branch |"
DEFAULT_PATH: Final = "src/dromon/codec.py"
NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]*")
MAX_LINE: Final = 1024
UNSETTLED: Final = ("Pending", "Uncommitted")
GITHUB_MERGE: Final = re.compile(r"Merge pull request #(\d+) from [^/\s]+/(\S+)")
GIT_MERGE: Final = re.compile(r"Merge branch '([^']+)'")


class FloorError(Exception):
    """A fragment, the table or the history that cannot be used, with the reason."""


class Entry(NamedTuple):
    fragment: str
    change: str
    search: str
    path: str


class Row(NamedTuple):
    change: str
    first: str
    floor: str
    branch: str
    order: int


def run(base: Path | None, *arguments: str) -> subprocess.CompletedProcess[str]:
    """``git`` with ``arguments`` in ``base``, its output captured, never raising on
    its exit. The arguments are this script's own or text from the repository's own
    fragments, never a shell line."""
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )


def root() -> Path:
    found = run(None, "rev-parse", "--show-toplevel")
    if found.returncode != 0:
        raise FloorError("not inside a git repository")
    return Path(found.stdout.strip())


def parse(path: Path) -> list[Entry]:
    """The entries of one fragment, or ``FloorError`` naming the file."""
    stem = path.name.removesuffix(".md")
    if not path.name.endswith(".md") or NAME.fullmatch(stem) is None:
        raise FloorError(f"{path}: a fragment is BRANCH.md, in lowercase")
    entries: list[Entry] = []
    blocks = path.read_text(encoding="utf-8").strip("\n").split("\n\n")
    for block in blocks:
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            key, separator, value = line.partition(": ")
            if (
                not separator
                or key not in ("Change", "Search", "Path")
                or key in fields
            ):
                raise FloorError(f"{path}: unexpected line {line!r}")
            if not value.strip() or len(value) > MAX_LINE or "|" in value:
                raise FloorError(f"{path}: {key} must be one line without a pipe")
            fields[key] = value
        if "Change" not in fields or "Search" not in fields:
            raise FloorError(f"{path}: each entry needs a Change and a Search line")
        entries.append(
            Entry(
                stem,
                fields["Change"],
                fields["Search"],
                relative(path, fields.get("Path", DEFAULT_PATH)),
            )
        )
    return entries


def relative(fragment: Path, value: str) -> str:
    """A fragment's ``Path``, which must name a file of the repository by a relative
    path without ``..``. **Review of 0db077c, finding 3.** An absolute path or a
    ``..`` part let a fragment ask the gate whether any readable file on the machine
    holds a string, one bit for each run in continuous integration. ``check`` also
    refuses a path whose real form lies outside the repository, as through a link."""
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value.startswith("~")
        or "\\" in value
        or any(part == ".." for part in pure.parts)
    ):
        raise FloorError(
            f"{fragment}: Path must be relative to the repository, without .."
        )
    return value


def is_fragment(path: Path) -> bool:
    stem = path.name.removesuffix(".md")
    return (
        path.name.endswith(".md")
        and NAME.fullmatch(stem) is not None
        and path.is_file()
    )


def ignored(base: Path) -> list[str]:
    """The entries of the fragment directory that are neither its README nor named
    as a fragment, such as ``.DS_Store`` or an editor's backup. **Review of 0db077c,
    finding 4.** They are left out with a warning, rather than failing the gate,
    since git need not know of them. Residual. A fragment misnamed in another case,
    as ``Feat--b.md``, is also left out, and the warning is what shows it."""
    directory = base / DIRECTORY
    if not directory.is_dir():
        return []
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.name != "README.md" and not is_fragment(path)
    )


def fragments(base: Path) -> list[Entry]:
    directory = base / DIRECTORY
    if not directory.is_dir():
        return []
    entries: list[Entry] = []
    for path in sorted(directory.iterdir()):
        if path.name != "README.md" and is_fragment(path):
            entries.extend(parse(path))
    return entries


def table_bounds(lines: list[str]) -> tuple[int, int]:
    """The index of the table's header and of the line after its last row."""
    if lines.count(HEADER) != 1:
        raise FloorError(f"{TABLE_FILE} must hold the floor table's header once")
    start = lines.index(HEADER)
    end = start + 2
    while end < len(lines) and lines[end].startswith("| "):
        end += 1
    return start, end


def check(base: Path) -> None:
    for entry in fragments(base):
        target = base / entry.path
        if not target.resolve().is_relative_to(base.resolve()):
            raise FloorError(
                f"{entry.fragment}: {entry.path} lies outside the repository"
            )
        if not target.is_file():
            raise FloorError(f"{entry.fragment}: {entry.path} is not a file")
        if entry.search not in target.read_text(encoding="utf-8"):
            raise FloorError(f"{entry.fragment}: {entry.path} does not hold its Search")
    lines = (base / TABLE_FILE).read_text(encoding="utf-8").split("\n")
    start, end = table_bounds(lines)
    for number in range(start + 2, end):
        cells = [cell.strip() for cell in lines[number].strip("|").split(" | ")]
        if any(word in cells[1:3] for word in UNSETTLED):
            raise FloorError(
                f"{TABLE_FILE}:{number + 1}: a pending row belongs in a fragment"
                f" under {DIRECTORY}, which a release writes with its commits"
            )


def git(base: Path, *arguments: str) -> str:
    found = run(base, *arguments)
    if found.returncode != 0:
        raise FloorError(f"git {' '.join(arguments)} failed: {found.stderr.strip()}")
    return found.stdout.strip()


def base_ref(base: Path, given: str | None) -> str:
    for name in (given,) if given else ("origin/main", "main"):
        if (
            run(base, "rev-parse", "--verify", "-q", f"{name}^{{commit}}").returncode
            == 0
        ):
            return name
    raise FloorError("no base branch, so no floor can be found")


def row(base: Path, entry: Entry, line: list[str]) -> Row:
    found = git(
        base,
        "log",
        "--reverse",
        "--format=%H",
        "-S",
        entry.search,
        "HEAD",
        "--",
        entry.path,
    ).split()
    fallback = entry.fragment.replace("--", "/")
    if not found:
        return Row(entry.change, "Uncommitted", "Pending", f"`{fallback}`", len(line))
    first = git(base, "rev-parse", "--short=7", found[0])
    for order, commit in enumerate(line):
        if run(base, "merge-base", "--is-ancestor", found[0], commit).returncode == 0:
            subject = git(base, "log", "-1", "--format=%s", commit)
            github, plain = GITHUB_MERGE.match(subject), GIT_MERGE.match(subject)
            branch = (
                f"`{github.group(2)}`, #{github.group(1)}"
                if github
                else f"`{plain.group(1)}`"
                if plain
                else f"`{fallback}`"
            )
            short = git(base, "rev-parse", "--short=7", commit)
            return Row(entry.change, f"`{first}`", f"`{short}`", branch, order)
    return Row(entry.change, f"`{first}`", "Pending", f"`{fallback}`", len(line))


def rows(base: Path, given: str | None) -> list[Row]:
    entries = fragments(base)
    if not entries:
        return []
    reference = base_ref(base, given)
    line = git(base, "rev-list", "--first-parent", "--reverse", reference).split()
    return sorted((row(base, entry, line) for entry in entries), key=lambda r: r.order)


def written(found: Row) -> str:
    return f"| {found.change} | {found.first} | {found.floor} | {found.branch} |"


def release(base: Path, given: str | None) -> list[str]:
    found = rows(base, given)
    if not found:
        raise FloorError("no fragment to release")
    unsettled = [
        r.change for r in found if r.first in UNSETTLED or r.floor in UNSETTLED
    ]
    if unsettled:
        raise FloorError(f"not merged into the base yet: {'; '.join(unsettled)}")
    table = base / TABLE_FILE
    lines = table.read_text(encoding="utf-8").split("\n")
    _, end = table_bounds(lines)
    lines[end:end] = [written(r) for r in found]
    table.write_text("\n".join(lines), encoding="utf-8")
    names = sorted({entry.fragment for entry in fragments(base)})
    for name in names:
        (base / DIRECTORY / f"{name}.md").unlink()
    return names


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Rollback floor fragments.")
    parser.add_argument("command", choices=("check", "preview", "release"))
    parser.add_argument("--base", default=None)
    options = parser.parse_args(arguments)
    try:
        base = root()
        for name in ignored(base):
            print(
                f"assemble_floors: warning: ignored {DIRECTORY / name}, which is not"
                " named BRANCH.md in lowercase",
                file=sys.stderr,
            )
        if options.command == "check":
            check(base)
        elif options.command == "preview":
            for found in rows(base, options.base):
                print(written(found))
        else:
            for name in release(base, options.base):
                print(f"released {DIRECTORY / name}.md")
    except FloorError as error:
        print(f"assemble_floors: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
