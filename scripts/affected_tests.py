#!/usr/bin/env python3
"""The test files that name what a change touched, so a line can run them first.

**Why. Round 19, item 2.** A line inside a sandbox runs the test files it edited, and
the operator's full gate then finds the failures in the files it did not know to run.
In round 18 a change of the launch sweep's rule broke six tests in a file that built
launch records, which no run of the touched files could reach. This command lists the
test files that mention a function, class or module-level name the change touched, so
the line runs those too.

**Usage.** From the repository, with the system interpreter and no dependencies, since
it uses only the standard library and ``git``.

    python3 -I scripts/affected_tests.py BASE          # the test file paths, sorted
    python3 -I scripts/affected_tests.py BASE --why    # each with the names it holds
    python3 -I scripts/affected_tests.py BASE --names  # the names found, no search
    uv run pytest $(python3 -I scripts/affected_tests.py BASE)

``BASE`` is a commit, or anything ``git`` resolves to one. The comparison is the
working tree, tracked changes and new files that are not ignored, against ``BASE``.
Exit codes are 0 when it ran, whether or not it found a file, 1 when ``git`` or the
tree could not be read, and 2 for a usage error. An empty list is printed as nothing,
and a line says on standard error how many names it searched for, so a clean answer is
not taken for a search that found no names.

**What it does.** It reads the diff of every ``.py`` file under ``src/dromon`` against
``BASE``. A changed line is mapped to the definitions that contain it, in the version
that holds it, which is the working tree for an added or changed line and ``BASE`` for
a removed one, so a deleted function is named. A definition is a function, a method, a
class or a name assigned at module level, and the names of the definition and of every
definition around it are taken, so a changed method names its class. A test file is
listed when it contains one of the names as a whole word. A test file the change itself
touched is listed too. Names that are too common to say anything, the dunder methods
and a short list of builtin-like names, are not searched.

**What it cannot find. Residuals, all of which a line must still judge.**

- **Indirect callers.** A test that exercises a function only through another module
  names the other module's functions and not this one's. The search is of text, one
  level, and does not follow calls.
- **Names that are not in the text.** A name built at run time, a ``getattr`` of a
  composed string, or a behaviour with no name, such as a changed default or a changed
  constant that is read through a mapping, is not found.
- **Too many files.** A common method name, such as ``start`` or ``run``, names a test
  that has nothing to do with it. The list is a superset where it finds a name and
  never a proof that the rest is unaffected.
- **Module-level code that is not an assignment**, such as a loop at import, a changed
  decorator or an ``if`` that chooses a definition, names nothing. A changed import
  names nothing either.
- **Renames.** A renamed function is found under both names, by the removed line and the
  added one, and a file that used the old name and was not updated is the catch.
- **Non-Python inputs**, such as a JSON file the code reads, a protocol document or the
  configuration, which no name leads from.
- **Conftest.** A change to ``tests/conftest.py`` reaches every test, and is not
  expanded here. The file is listed when it changed, and a line must run the suite.

**Bounds.** At most ``MAX_FILES`` changed source files, ``MAX_NAMES`` names,
``MAX_TEST_FILES`` test files and ``MAX_BYTES`` read from each, and a ``git`` call is
given ``GIT_SECONDS``. Past a bound the command says so on standard error and exits 1,
never silently answering from part of the change.
"""

import argparse
import ast
import contextlib
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

SOURCE = "src/dromon"
TESTS = "tests"
MAX_FILES = 200
MAX_NAMES = 2000
MAX_TEST_FILES = 2000
MAX_BYTES = 4 * 1024 * 1024
GIT_SECONDS = 60

IGNORED = frozenset(
    {
        "main",
        "run",
        "start",
        "stop",
        "get",
        "set",
        "read",
        "write",
        "name",
        "path",
        "file",
        "data",
        "text",
        "value",
        "items",
        "keys",
        "close",
        "open",
        "send",
        "test",
        "self",
        "cls",
    }
)
"""Names so common that every test holds one, so searching for them would list the whole
suite. They are the names of builtin-like methods and variables. **Residual.** A change
that touches only a function called ``main`` finds nothing here, and a line reads the
list above and runs the file of the module by hand."""

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Failure(Exception):
    """The tree or ``git`` could not be read, or a bound was passed."""


def git(root: Path, *arguments: str) -> str:
    """The standard output of a ``git`` command in ``root``, or ``Failure``."""
    try:
        done = subprocess.run(  # noqa: S603
            ["git", *arguments],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failure(f"git {' '.join(arguments)} did not run: {error!r}") from error
    if done.returncode != 0:
        raise Failure(f"git {' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout


def ranges(diff: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The old-side and new-side line ranges the hunks of one file's diff name, each
    as ``(first, last)`` and only when the hunk touches at least one line on that
    side. A hunk that only adds has no old range, and one that only removes has no new
    range, which is how ``-U0`` marks them with a count of 0."""
    old: list[tuple[int, int]] = []
    new: list[tuple[int, int]] = []
    for line in diff.splitlines():
        found = HUNK.match(line)
        if found is None:
            continue
        old_start, old_count = int(found[1]), int(found[2]) if found[2] else 1
        new_start, new_count = int(found[3]), int(found[4]) if found[4] else 1
        if old_count > 0:
            old.append((old_start, old_start + old_count - 1))
        if new_count > 0:
            new.append((new_start, new_start + new_count - 1))
    return old, new


def definitions(source: str) -> list[tuple[str, int, int, int]]:
    """The functions, classes and module-level assigned names of ``source``, as
    ``(name, first line, last line, depth)``, or none when it does not parse. A
    method has the depth of its class plus one, and a name assigned inside a function
    is not a definition, so a local variable never names a test. A source that does
    not parse is no definition at all, and the caller says so."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[str, int, int, int]] = []
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    steps = 0
    while stack and steps < 200_000:
        steps += 1
        node, depth = stack.pop()
        for child in ast.iter_child_nodes(node):
            end = getattr(child, "end_lineno", None)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.append((child.name, child.lineno, end or child.lineno, depth))
                stack.append((child, depth + 1))
            elif isinstance(node, (ast.Module, ast.ClassDef)):
                found.extend(assigned(child, depth))
    return found


def assigned(node: ast.AST, depth: int) -> list[tuple[str, int, int, int]]:
    """The names a module-level or class-level assignment binds."""
    names: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        names = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        names = [node.target]
    found: list[tuple[str, int, int, int]] = []
    for target in names:
        if isinstance(target, ast.Name):
            found.append(
                (
                    target.id,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                    depth,
                )
            )
    return found


def names_touched(source: str, spans: Iterable[tuple[int, int]]) -> set[str]:
    """The name of every definition that holds a line of ``spans``, and of every one
    around it. A definition holds a line when the line lies from its first line to its
    last."""
    parts = definitions(source)
    touched: set[str] = set()
    for first, last in spans:
        for name, begin, end, _depth in parts:
            if begin <= last and first <= end:
                touched.add(name)
    return touched


def searchable(names: Iterable[str]) -> list[str]:
    """The names worth searching, which are not dunder, not private to a single
    underscore alone, and not on ``IGNORED``, sorted."""
    return sorted(
        name
        for name in set(names)
        if name not in IGNORED
        and not (name.startswith("__") and name.endswith("__"))
        and name.strip("_")
    )


def changed_files(root: Path, base: str) -> list[str]:
    """The ``.py`` files under ``SOURCE`` that differ from ``base``, including files
    that are new and not ignored."""
    listed = git(root, "diff", "--name-only", "--no-renames", base, "--", SOURCE)
    new = git(root, "ls-files", "--others", "--exclude-standard", "--", SOURCE)
    paths = sorted(
        {line for line in (listed + new).splitlines() if line.endswith(".py")}
    )
    if len(paths) > MAX_FILES:
        raise Failure(f"{len(paths)} source files changed, past the bound {MAX_FILES}")
    return paths


def source_names(root: Path, base: str) -> set[str]:
    """Every name the change from ``base`` touched in ``SOURCE``."""
    found: set[str] = set()
    for path in changed_files(root, base):
        current = root / path
        old_text = ""
        with contextlib.suppress(Failure):  # a file that is new has no old side
            old_text = git(root, "show", f"{base}:{path}")
        new_text = current.read_text(encoding="utf-8") if current.is_file() else ""
        if not old_text:
            found |= {name for name, *_ in definitions(new_text)}
            continue
        diff = git(root, "diff", "-U0", "--no-renames", base, "--", path)
        old, new = ranges(diff)
        found |= names_touched(old_text, old)
        found |= names_touched(new_text, new)
    return found


def touched_tests(root: Path, base: str) -> set[str]:
    """The test files the change itself edited or added."""
    listed = git(root, "diff", "--name-only", "--no-renames", base, "--", TESTS)
    new = git(root, "ls-files", "--others", "--exclude-standard", "--", TESTS)
    return {line for line in (listed + new).splitlines() if line.endswith(".py")}


def holds(text: str, pattern: re.Pattern[str]) -> list[str]:
    """The distinct names of ``pattern`` that ``text`` holds as whole words."""
    return sorted({found[0] for found in pattern.finditer(text)})


def affected(root: Path, base: str) -> tuple[dict[str, list[str]], list[str]]:
    """The test files to run, each with the names it holds, and the names searched.
    A test file the change edited is there with no names when it holds none."""
    names = searchable(source_names(root, base))
    if len(names) > MAX_NAMES:
        raise Failure(f"{len(names)} names changed, past the bound {MAX_NAMES}")
    result: dict[str, list[str]] = {path: [] for path in touched_tests(root, base)}
    if names:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")
        tests = sorted((root / TESTS).rglob("*.py"))
        if len(tests) > MAX_TEST_FILES:
            raise Failure(f"{len(tests)} test files, past the bound {MAX_TEST_FILES}")
        for test in tests:
            if "__pycache__" in test.parts:
                continue
            with test.open("rb") as handle:
                text = handle.read(MAX_BYTES).decode("utf-8", errors="replace")
            held = holds(text, pattern)
            if held:
                result[test.relative_to(root).as_posix()] = held
    return dict(sorted(result.items())), names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the test files that name what a change from BASE touched."
    )
    parser.add_argument("base", help="a commit to compare the working tree with")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--why", action="store_true", help="show the names each holds")
    mode.add_argument("--names", action="store_true", help="show the names, no search")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    try:
        if arguments.names:
            names = searchable(source_names(root, arguments.base))
            print("\n".join(names))
            print(f"affected_tests: {len(names)} names", file=sys.stderr)
            return 0
        result, names = affected(root, arguments.base)
    except Failure as error:
        print(f"affected_tests: {error}", file=sys.stderr)
        return 1
    for path, held in result.items():
        print(f"{path}: {', '.join(held)}" if arguments.why and held else path)
    print(
        f"affected_tests: {len(names)} names searched, {len(result)} test files",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
