#!/usr/bin/env python3
"""Changelog fragments, their assembly at a release, and the branch rule.

**Why.** Every branch added its entry under the same headings of CHANGELOG.md's
Unreleased section, so almost every merge conflicted there, and the section had grown
repeated headings. A branch now adds one small file under ``changes/``, named for the
branch, and nothing else touches the changelog. A release assembles the fragments.

**A fragment** is ``changes/BRANCH.md``, where BRANCH is the branch name with each
``/`` written ``--``, for example ``changes/feat--changelog-fragments.md``. Its first
line is ``Type: Added``, ``Type: Changed`` or ``Type: Fixed``, then a blank line, then
one or more bullets in the changelog's own form, each starting with ``- `` and wrapped
with a two space indent. ``changes/README.md`` is not a fragment.

**Commands.** All run from anywhere inside the repository, with only the standard
library.

* ``preview`` prints what the next release's section would be, the Unreleased entries
  and every fragment, and writes nothing.
* ``release VERSION DATE`` writes that section into CHANGELOG.md as ``## [VERSION] -
  DATE`` below an empty ``## [Unreleased]``, deletes the fragments it used, and
  prints their names. ``--dry-run`` prints the new CHANGELOG.md and changes nothing.
  The release commit then holds the changelog edit and the fragment deletions.
* ``collapse`` rewrites the Unreleased section with one heading of each type, keeping
  every entry in order. It is how the repeated headings were removed.
* ``check`` is the gate's rule. It validates every fragment, and refuses a branch that
  changes CHANGELOG.md's Unreleased section and neither adds nor deletes a fragment.
  Its base is the merge base of HEAD and ``--base``, which defaults to ``origin/main``
  and then ``main``. With no base available, as in a clone that fetched one branch, it
  says so and passes. A release deletes fragments, so it is allowed to edit Unreleased.

**Failure modes.** A fragment that does not parse stops every command, naming the file.
Unreleased entries are kept as written, apart from the order of the headings. The
assembler does not judge an entry's wording. A fragment edited after its branch merged
is a new change to a released-to-be entry and is not refused.

**Porting note.** Plain text in, plain text out, with no clock and no randomness. The
date is an argument.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Final, NamedTuple

TYPES: Final = ("Added", "Changed", "Fixed")
"""The entry types, in the order their sections are written."""

UNRELEASED: Final = "## [Unreleased]"
FRAGMENTS_DIRECTORY: Final = "changes"
NOT_FRAGMENTS: Final = frozenset({"README.md"})
FRAGMENT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]*\.md")
VERSION: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ChangelogError(Exception):
    """A fragment or the changelog is not in the form this script reads."""


class Fragment(NamedTuple):
    name: str
    type: str
    body: str


class Parts(NamedTuple):
    preamble: str
    unreleased: dict[str, str]
    rest: str


def parse_fragment(name: str, text: str) -> Fragment:
    """The fragment ``text`` named ``name``, or ``ChangelogError`` naming the file."""
    if FRAGMENT_NAME.fullmatch(name) is None:
        raise ChangelogError(
            f"{name}: a fragment is named for its branch, in lowercase letters,"
            " digits, dots, hyphens and underscores, ending in .md"
        )
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Type: "):
        raise ChangelogError(f"{name}: the first line must be 'Type: ' and a type")
    kind = lines[0][len("Type: ") :].strip()
    if kind not in TYPES:
        raise ChangelogError(f"{name}: the type is {kind!r}, not one of {TYPES}")
    if len(lines) < 3 or lines[1].strip():
        raise ChangelogError(f"{name}: a blank line must follow the type line")
    body = "\n".join(lines[2:]).strip("\n")
    if not body.startswith("- "):
        raise ChangelogError(f"{name}: the text must begin with a bullet, '- '")
    for number, line in enumerate(body.splitlines(), start=3):
        if line.startswith("#"):
            raise ChangelogError(f"{name}:{number}: a heading is not allowed")
        if line.startswith("<<<<<<<") or line.startswith(">>>>>>>"):
            raise ChangelogError(f"{name}:{number}: a merge marker is left in")
    return Fragment(name, kind, body)


def read_fragments(root: Path) -> list[Fragment]:
    """Every fragment under ``root``, in name order."""
    directory = root / FRAGMENTS_DIRECTORY
    if not directory.is_dir():
        return []
    found: list[Fragment] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in NOT_FRAGMENTS:
            continue
        found.append(parse_fragment(path.name, path.read_text(encoding="utf-8")))
    return found


def split_changelog(text: str) -> Parts:
    """The preamble above Unreleased, its entries by type with repeated headings
    joined in order, and everything below. ``ChangelogError`` if there is no
    Unreleased section, text sits above its first heading, or a heading is not a
    known type."""
    lines = text.split("\n")
    try:
        start = lines.index(UNRELEASED)
    except ValueError as error:
        raise ChangelogError(f"CHANGELOG.md has no '{UNRELEASED}' heading") from error
    end = len(lines)
    for number in range(start + 1, len(lines)):
        if lines[number].startswith("## "):
            end = number
            break
    segments: dict[str, list[list[str]]] = {kind: [] for kind in TYPES}
    current: list[str] | None = None
    for line in lines[start + 1 : end]:
        if line.startswith("### "):
            kind = line[4:].strip()
            if kind not in TYPES:
                raise ChangelogError(f"Unreleased has a heading {kind!r}, not a type")
            current = []
            segments[kind].append(current)
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise ChangelogError("Unreleased has text above its first type heading")
    unreleased: dict[str, str] = {}
    for kind, pieces in segments.items():
        body = "\n".join(
            part for part in ("\n".join(piece).strip("\n") for piece in pieces) if part
        )
        if body:
            unreleased[kind] = body
    preamble = "\n".join(lines[:start])
    rest = "\n".join(lines[end:])
    return Parts(preamble, unreleased, rest)


def render_sections(entries: dict[str, str]) -> str:
    """The type sections for ``entries``, one heading of each type present."""
    blocks = [f"### {kind}\n\n{entries[kind]}\n" for kind in TYPES if entries.get(kind)]
    return "\n".join(blocks)


def join_entries(parts: dict[str, str], fragments: list[Fragment]) -> dict[str, str]:
    """``parts`` with each fragment's bullets appended to its type, in name order."""
    merged = dict(parts)
    for fragment in sorted(fragments, key=lambda item: item.name):
        existing = merged.get(fragment.type)
        merged[fragment.type] = (
            fragment.body if not existing else existing + "\n" + fragment.body
        )
    return merged


def collapse_text(text: str) -> str:
    """CHANGELOG ``text`` with the Unreleased section rewritten to one heading of each
    type present, every entry kept in order."""
    parts = split_changelog(text)
    sections = render_sections(parts.unreleased).rstrip("\n")
    pieces = [parts.preamble.rstrip("\n"), f"{UNRELEASED}\n\n{sections}".rstrip("\n")]
    tail = parts.rest.strip("\n")
    if tail:
        pieces.append(tail)
    return "\n\n".join(pieces) + "\n"


def release_text(text: str, fragments: list[Fragment], version: str, date: str) -> str:
    """CHANGELOG ``text`` after a release: an empty Unreleased, then the section
    ``## [VERSION] - DATE`` holding the Unreleased entries and the fragments."""
    if VERSION.fullmatch(version) is None:
        raise ChangelogError(f"{version!r} is not a version such as 0.1.0")
    if DATE.fullmatch(date) is None:
        raise ChangelogError(f"{date!r} is not a date such as 2026-10-31")
    parts = split_changelog(text)
    merged = join_entries(parts.unreleased, fragments)
    if not merged:
        raise ChangelogError("there are no entries to release")
    section = f"## [{version}] - {date}\n\n{render_sections(merged)}"
    tail = parts.rest.strip("\n")
    pieces = [parts.preamble.rstrip("\n"), f"{UNRELEASED}", section.rstrip("\n")]
    if tail:
        pieces.append(tail)
    return "\n\n".join(pieces) + "\n"


def unreleased_text(text: str) -> str:
    """The Unreleased section's lines, from its heading to the next section."""
    lines = text.split("\n")
    if UNRELEASED not in lines:
        return ""
    start = lines.index(UNRELEASED)
    end = len(lines)
    for number in range(start + 1, len(lines)):
        if lines[number].startswith("## "):
            end = number
            break
    return "\n".join(lines[start:end])


def branch_violation(
    base: str, head: str, added: list[str], removed: list[str]
) -> str | None:
    """Why a branch breaks the rule, or ``None``. ``base`` and ``head`` are the
    changelog before and after the branch, ``added`` and ``removed`` the fragment
    files it adds and deletes."""
    if unreleased_text(base) == unreleased_text(head):
        return None
    if added or removed:
        return None
    return (
        "this branch edits CHANGELOG.md's Unreleased section and adds no fragment."
        " Put the entry in changes/BRANCH.md instead, as changes/README.md says, and"
        " leave the changelog to the release"
    )


def run_git(root: Path, *arguments: str) -> str | None:
    """Git's output in ``root``, or ``None`` if it fails."""
    done = subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def find_base(root: Path, requested: str | None) -> str | None:
    """The merge base of HEAD and the first reference that resolves, or ``None``."""
    candidates = [requested] if requested else ["origin/main", "main"]
    for reference in candidates:
        base = run_git(root, "merge-base", "HEAD", reference)
        if base:
            return base.strip()
    return None


def fragment_changes(root: Path, base: str) -> tuple[list[str], list[str]]:
    """The fragment files a working tree adds and deletes against ``base``,
    untracked files included."""
    status = run_git(root, "diff", "--name-status", base, "--", FRAGMENTS_DIRECTORY)
    added: list[str] = []
    removed: list[str] = []
    for line in (status or "").splitlines():
        kind, _, name = line.partition("\t")
        leaf = Path(name).name
        if leaf in NOT_FRAGMENTS:
            continue
        if kind.startswith("A"):
            added.append(leaf)
        elif kind.startswith("D"):
            removed.append(leaf)
    untracked = run_git(
        root, "ls-files", "--others", "--exclude-standard", "--", FRAGMENTS_DIRECTORY
    )
    for name in (untracked or "").splitlines():
        if Path(name).name not in NOT_FRAGMENTS:
            added.append(Path(name).name)
    return added, removed


def check(root: Path, requested: str | None) -> int:
    read_fragments(root)
    base = find_base(root, requested)
    if base is None:
        print(
            "assemble_changelog: note: no base branch is available, so the rule on"
            " editing Unreleased was not checked"
        )
        return 0
    old = run_git(root, "show", f"{base}:CHANGELOG.md")
    if old is None:
        return 0
    new = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    added, removed = fragment_changes(root, base)
    why = branch_violation(old, new, added, removed)
    if why is not None:
        print(f"assemble_changelog: FAIL: {why}", file=sys.stderr)
        return 1
    return 0


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preview")
    commands.add_parser("collapse")
    release = commands.add_parser("release")
    release.add_argument("version")
    release.add_argument("date")
    release.add_argument("--dry-run", action="store_true")
    checking = commands.add_parser("check")
    checking.add_argument("--base")
    arguments = parser.parse_args(argv)
    root = repository_root()
    changelog = root / "CHANGELOG.md"
    try:
        if arguments.command == "check":
            return check(root, arguments.base)
        text = changelog.read_text(encoding="utf-8")
        if arguments.command == "collapse":
            changelog.write_text(collapse_text(text), encoding="utf-8")
            return 0
        fragments = read_fragments(root)
        if arguments.command == "preview":
            merged = join_entries(split_changelog(text).unreleased, fragments)
            print(render_sections(merged), end="")
            return 0
        new = release_text(text, fragments, arguments.version, arguments.date)
        if arguments.dry_run:
            print(new, end="")
            return 0
        changelog.write_text(new, encoding="utf-8")
        for fragment in fragments:
            (root / FRAGMENTS_DIRECTORY / fragment.name).unlink()
            print(f"removed {FRAGMENTS_DIRECTORY}/{fragment.name}")
    except ChangelogError as error:
        print(f"assemble_changelog: FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
