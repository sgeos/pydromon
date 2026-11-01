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
  prints their names. It refuses a version already in the changelog and a date that is
  not a real calendar date. ``--dry-run`` prints the new CHANGELOG.md and changes
  nothing. The release commit then holds the changelog edit and the fragment deletions.
* ``collapse`` rewrites the Unreleased section with one heading of each type, keeping
  every entry in order. It is how the repeated headings were removed.
* ``check`` is the gate's rule. It validates every fragment, and refuses a branch that
  changes CHANGELOG.md's Unreleased section unless the change is a release or a
  lossless regrouping. Its base is the merge base of HEAD and ``--base``, which defaults
  to ``origin/main`` and then ``main``. With no base available, as in a clone that
  fetched one branch, it prints that it skipped and exits 3, which the gate shows as a
  skip and not as a pass. Any other failure exits 1.

**The branch rule.** A branch does not edit CHANGELOG.md's Unreleased section. Its
entry goes in a fragment. Two edits are allowed, because neither adds, removes or
changes an entry. A *release* adds a version heading, leaves Unreleased empty, and
keeps every entry that was in Unreleased and every released line, and either deletes a
fragment or had entries in Unreleased. A *lossless regrouping* leaves Unreleased exactly
as ``collapse`` would write the base's, so a section with repeated headings can be
tidied once, and a section already tidy has nothing left to regroup. The regrouping is
the cleanest exception for the branch that introduced this rule, since it needs no
mark in the history and cannot be used to slip an entry in.

**Atomic release.** The new changelog is written to a temporary file first. The
fragments are then moved into ``changes/.released-VERSION``, and the temporary file
replaces CHANGELOG.md, which is one atomic step. Only then is the staging directory
removed. A failure before the replacement moves the fragments back and removes the
temporary file, so nothing has changed. A failure removing the staging directory leaves
a finished release and a directory to remove by hand, which no command reads as
fragments, so nothing is released twice. If moving back also fails, the message names
the staging directory that holds the fragments.

**Failure modes.** A fragment that does not parse stops every command, naming the file.
Unreleased entries are kept as written, apart from the order of the headings. The
assembler does not judge an entry's wording. A fragment edited after its branch merged
is a new change to a released-to-be entry and is not refused.

**Porting note.** Plain text in, plain text out, with no clock and no randomness. The
date is an argument.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
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
SKIPPED: Final = 3
"""The exit status of ``check`` when it had no base branch to compare with."""


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
    try:
        datetime.date.fromisoformat(date)
    except ValueError as error:
        raise ChangelogError(f"{date!r} is not a calendar date") from error
    if any(heading.startswith(f"## [{version}]") for heading in version_headings(text)):
        raise ChangelogError(f"version {version} is already in the changelog")
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


def version_headings(text: str) -> set[str]:
    """The headings of the released sections, such as ``## [0.1.0] - 2026-11-15``."""
    return {
        line
        for line in text.split("\n")
        if line.startswith("## [") and line != UNRELEASED
    }


def entry_lines(text: str) -> Counter[str]:
    """Every line that is neither blank nor a type heading."""
    return Counter(
        line
        for line in text.split("\n")
        if line.strip() and not line.startswith("### ")
    )


def outside_unreleased(text: str) -> str:
    """``text`` without the Unreleased section."""
    section = unreleased_text(text)
    return text.replace(section, "", 1) if section else text


def is_regrouping(base: str, head: str) -> bool:
    """Whether ``head``'s Unreleased is exactly what ``collapse`` makes of ``base``'s,
    which moves entries under fewer headings and changes none."""
    try:
        return unreleased_text(collapse_text(base)) == unreleased_text(head)
    except ChangelogError:
        return False


def is_release(base: str, head: str, removed: list[str]) -> bool:
    """Whether ``head`` is ``base`` released. A version heading is new, Unreleased is
    empty, every released line of ``base`` and every entry of its Unreleased is still
    there, and something was released, a deleted fragment or Unreleased entries."""
    try:
        before = split_changelog(base)
        after = split_changelog(head)
    except ChangelogError:
        return False
    if not version_headings(head) - version_headings(base) or after.unreleased:
        return False
    kept = entry_lines(unreleased_text(base))
    kept.pop(UNRELEASED, None)
    wanted = entry_lines(outside_unreleased(base)) + kept
    if wanted - entry_lines(outside_unreleased(head)):
        return False
    return bool(removed) or bool(before.unreleased)


def branch_violation(base: str, head: str, removed: list[str]) -> str | None:
    """Why a branch breaks the rule, or ``None``. ``base`` and ``head`` are the
    changelog before and after the branch, ``removed`` the fragment files it deletes."""
    if unreleased_text(base) == unreleased_text(head):
        return None
    if is_regrouping(base, head) or is_release(base, head, removed):
        return None
    return (
        "this branch edits CHANGELOG.md's Unreleased section, which only a release or"
        " a lossless regrouping may do. Put the entry in changes/BRANCH.md instead,"
        " as changes/README.md says, and leave the changelog to the release"
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


def removed_fragments(root: Path, base: str) -> list[str]:
    """The fragment files a working tree deletes against ``base``."""
    status = run_git(root, "diff", "--name-status", base, "--", FRAGMENTS_DIRECTORY)
    removed: list[str] = []
    for line in (status or "").splitlines():
        kind, _, name = line.partition("\t")
        if kind.startswith("D") and Path(name).name not in NOT_FRAGMENTS:
            removed.append(Path(name).name)
    return removed


def check(root: Path, requested: str | None) -> int:
    read_fragments(root)
    base = find_base(root, requested)
    old = None if base is None else run_git(root, "show", f"{base}:CHANGELOG.md")
    if base is None or old is None:
        print(
            "assemble_changelog: SKIPPED: no base branch with a changelog is"
            " available, so the rule on editing Unreleased was not checked"
        )
        return SKIPPED
    new = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    why = branch_violation(old, new, removed_fragments(root, base))
    if why is not None:
        print(f"assemble_changelog: FAIL: {why}", file=sys.stderr)
        return 1
    return 0


def apply_release(
    root: Path, new_text: str, fragments: list[Fragment], version: str
) -> list[str]:
    """Write a release so that a failure leaves nothing changed, or a finished release.
    Returns warnings. Raises ``ChangelogError`` when it undid the release."""
    directory = root / FRAGMENTS_DIRECTORY
    changelog = root / "CHANGELOG.md"
    temporary = root / "CHANGELOG.md.release"
    staging = directory / f".released-{version}"
    for leftover in (temporary, staging):
        # Only what this call made is ever removed, so a path already there is refused
        # before anything is touched, and is never taken for this call's own.
        if os.path.lexists(leftover):
            raise ChangelogError(
                f"{leftover} is left over from an earlier release, so nothing changed."
                " Remove it by hand and run the release again"
            )
    moved: list[str] = []
    made_staging = False
    try:
        temporary.write_text(new_text, encoding="utf-8")
        if fragments:
            staging.mkdir()
            made_staging = True
        for fragment in fragments:
            os.replace(directory / fragment.name, staging / fragment.name)
            moved.append(fragment.name)
        os.replace(temporary, changelog)
    except OSError as error:
        stuck: list[str] = []
        for name in reversed(moved):
            try:
                os.replace(staging / name, directory / name)
            except OSError:
                stuck.append(name)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        if not stuck:
            if made_staging:
                staging.rmdir()
            raise ChangelogError(
                f"the release failed and was undone, nothing changed: {error}"
            ) from error
        raise ChangelogError(
            f"the release failed: {error}. Fragments {', '.join(stuck)} could not be"
            f" moved back and are in {staging}"
        ) from error
    try:
        if staging.exists():
            shutil.rmtree(staging)
    except OSError as error:
        return [
            f"the release is complete, but {staging} could not be removed: {error}."
            " Remove it by hand. No command reads it as fragments"
        ]
    return []


def perform_release(root: Path, version: str, date: str, dry_run: bool) -> int:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    fragments = read_fragments(root)
    new = release_text(text, fragments, version, date)
    if dry_run:
        print(new, end="")
        return 0
    warnings = apply_release(root, new, fragments, version)
    for fragment in fragments:
        print(f"removed {FRAGMENTS_DIRECTORY}/{fragment.name}")
    for warning in warnings:
        print(f"assemble_changelog: warning: {warning}", file=sys.stderr)
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
        return perform_release(
            root, arguments.version, arguments.date, arguments.dry_run
        )
    except ChangelogError as error:
        print(f"assemble_changelog: FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
