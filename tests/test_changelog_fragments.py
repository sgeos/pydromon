"""Changelog fragments, their assembly at a release, and the branch rule.

The assembler is a script of ``scripts/`` that the gate runs. Its functions are tested
directly, and its commands and the rule against a real git repository by running a copy
of it, since it finds its repository from its own place.
"""

import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from assemble_changelog import (
    ChangelogError,
    Fragment,
    branch_violation,
    collapse_text,
    join_entries,
    parse_fragment,
    read_fragments,
    release_text,
    split_changelog,
    unreleased_text,
)

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "assemble_changelog.py"

PREAMBLE = "# Changelog\n\nAll notable changes are recorded here.\n"
REPEATED = (
    PREAMBLE
    + """
## [Unreleased]

### Added

- First added.
  Continued.

### Changed

- First changed.

### Fixed

- First fixed.

### Added

- Second added.

### Fixed

- Second fixed.

## [0.0.1] - 2026-01-01

### Added

- Old entry.
"""
)


def entries(text: str) -> Counter[str]:
    """Every line that is neither blank nor a type heading."""
    return Counter(
        line
        for line in text.split("\n")
        if line.strip() and not line.startswith("### ")
    )


# Fragments.


def test_a_fragment_is_read_with_its_type_and_bullets() -> None:
    found = parse_fragment("feat--thing.md", "Type: Fixed\n\n- One.\n  More.\n- Two.\n")
    assert found == Fragment("feat--thing.md", "Fixed", "- One.\n  More.\n- Two.")


@pytest.mark.parametrize(
    ("name", "text", "why"),
    [
        ("Feat.md", "Type: Added\n\n- x\n", "named for its branch"),
        ("feat/x.md", "Type: Added\n\n- x\n", "named for its branch"),
        ("feat.txt", "Type: Added\n\n- x\n", "named for its branch"),
        ("a.md", "", "first line"),
        ("a.md", "- x\n", "first line"),
        ("a.md", "Type: Removed\n\n- x\n", "not one of"),
        ("a.md", "Type: added\n\n- x\n", "not one of"),
        ("a.md", "Type: Added\n- x\n", "blank line"),
        ("a.md", "Type: Added\n", "blank line"),
        ("a.md", "Type: Added\n\n\n", "begin with a bullet"),
        ("a.md", "Type: Added\n\nText without a bullet\n", "begin with a bullet"),
        ("a.md", "Type: Added\n\n- x\n### Added\n", "heading"),
        ("a.md", "Type: Added\n\n- x\n<<<<<<< HEAD\n", "merge marker"),
    ],
)
def test_a_malformed_fragment_is_refused_naming_the_file(
    name: str, text: str, why: str
) -> None:
    with pytest.raises(ChangelogError, match=why) as error:
        parse_fragment(name, text)
    assert name in str(error.value)


def test_fragments_are_read_in_name_order_and_the_readme_is_not_one(
    tmp_path: Path,
) -> None:
    assert read_fragments(tmp_path) == []
    directory = tmp_path / "changes"
    directory.mkdir()
    (directory / "README.md").write_text("# Not a fragment\n", encoding="utf-8")
    (directory / "b.md").write_text("Type: Added\n\n- b\n", encoding="utf-8")
    (directory / "a.md").write_text("Type: Fixed\n\n- a\n", encoding="utf-8")
    assert [fragment.name for fragment in read_fragments(tmp_path)] == ["a.md", "b.md"]
    (directory / "c.md").write_text("no type\n", encoding="utf-8")
    with pytest.raises(ChangelogError, match="c.md"):
        read_fragments(tmp_path)


# The Unreleased section.


def test_repeated_headings_are_collapsed_without_losing_an_entry() -> None:
    collapsed = collapse_text(REPEATED)
    unreleased = unreleased_text(collapsed)
    assert [line for line in unreleased.split("\n") if line.startswith("### ")] == [
        "### Added",
        "### Changed",
        "### Fixed",
    ]
    assert entries(collapsed) == entries(REPEATED)
    assert split_changelog(collapsed).unreleased["Added"] == (
        "- First added.\n  Continued.\n- Second added."
    )
    assert collapsed.startswith(PREAMBLE)
    assert collapsed.endswith("## [0.0.1] - 2026-01-01\n\n### Added\n\n- Old entry.\n")


def test_collapsing_twice_changes_nothing() -> None:
    once = collapse_text(REPEATED)
    assert collapse_text(once) == once


def test_the_repositorys_changelog_has_one_heading_of_each_type() -> None:
    text = (REPOSITORY / "CHANGELOG.md").read_text(encoding="utf-8")
    section = unreleased_text(text)
    headings = [line for line in section.split("\n") if line.startswith("### ")]
    assert len(headings) == len(set(headings))
    assert collapse_text(text) == text


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("# Changelog\n\n## [0.0.1] - 2026-01-01\n", "no '## \\[Unreleased\\]'"),
        ("## [Unreleased]\n\nstray\n\n### Added\n\n- x\n", "above its first"),
        ("## [Unreleased]\n\n### Removed\n\n- x\n", "not a type"),
    ],
)
def test_a_changelog_the_assembler_cannot_read_is_refused(text: str, why: str) -> None:
    with pytest.raises(ChangelogError, match=why):
        split_changelog(text)


# A release.


def test_a_release_puts_unreleased_and_the_fragments_in_one_section() -> None:
    fragments = [
        Fragment("b.md", "Added", "- From b."),
        Fragment("a.md", "Added", "- From a."),
        Fragment("c.md", "Changed", "- From c."),
    ]
    text = release_text(REPEATED, fragments, "0.1.0", "2026-11-15")
    parts = split_changelog(text)
    assert parts.unreleased == {}
    assert text.index("## [Unreleased]") < text.index("## [0.1.0] - 2026-11-15")
    assert text.index("## [0.1.0] - 2026-11-15") < text.index("## [0.0.1]")
    released = text.split("## [0.1.0] - 2026-11-15\n")[1].split("## [0.0.1]")[0]
    assert released.count("### Added") == 1
    assert released.index("- Second added.") < released.index("- From a.")
    assert released.index("- From a.") < released.index("- From b.")
    assert "- From c." in released
    expected = entries(REPEATED) + Counter(
        {"- From a.": 1, "- From b.": 1, "- From c.": 1, "## [0.1.0] - 2026-11-15": 1}
    )
    assert entries(text) == expected
    assert text.endswith("## [0.0.1] - 2026-01-01\n\n### Added\n\n- Old entry.\n")


def test_a_release_after_a_release_starts_from_an_empty_unreleased() -> None:
    first = release_text(REPEATED, [], "0.1.0", "2026-11-15")
    second = release_text(
        first, [Fragment("a.md", "Fixed", "- Later.")], "0.1.1", "2026-11-20"
    )
    assert second.index("## [0.1.1]") < second.index("## [0.1.0]")
    assert "- Later." in second.split("## [0.1.0]")[0]
    assert "- Later." not in second.split("## [0.1.0]")[1]


@pytest.mark.parametrize(
    ("version", "date", "why"),
    [
        ("1.0", "2026-11-15", "not a version"),
        ("v1.0.0", "2026-11-15", "not a version"),
        ("1.0.0", "15 November", "not a date"),
    ],
)
def test_a_release_needs_a_version_and_a_date(
    version: str, date: str, why: str
) -> None:
    with pytest.raises(ChangelogError, match=why):
        release_text(REPEATED, [], version, date)


def test_a_release_with_nothing_to_release_is_refused() -> None:
    with pytest.raises(ChangelogError, match="no entries"):
        release_text(PREAMBLE + "\n## [Unreleased]\n", [], "0.1.0", "2026-11-15")


def test_fragments_join_their_type_in_name_order() -> None:
    merged = join_entries(
        {"Added": "- Old."},
        [Fragment("z.md", "Added", "- Z."), Fragment("a.md", "Added", "- A.")],
    )
    assert merged == {"Added": "- Old.\n- A.\n- Z."}


# The branch rule.


def test_the_rule_allows_an_untouched_changelog_and_a_fragment_or_a_release() -> None:
    changed = REPEATED.replace("- First added.", "- Edited.")
    assert branch_violation(REPEATED, REPEATED, [], []) is None
    assert branch_violation(REPEATED, changed, ["feat--x.md"], []) is None
    assert branch_violation(REPEATED, changed, [], ["feat--x.md"]) is None


def test_the_rule_refuses_an_edit_to_unreleased_with_no_fragment() -> None:
    changed = REPEATED.replace("- First added.", "- Edited.")
    why = branch_violation(REPEATED, changed, [], [])
    assert why is not None
    assert "changes/BRANCH.md" in why


def test_the_rule_ignores_an_edit_below_unreleased() -> None:
    changed = REPEATED.replace("- Old entry.", "- Old entry, reworded.")
    assert unreleased_text(REPEATED) == unreleased_text(changed)
    assert branch_violation(REPEATED, changed, [], []) is None


# The commands and the rule against a real repository.


def git(root: Path, *arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def script(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(root / "scripts" / "assemble_changelog.py"),
            *arguments,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository on main holding the collapsed changelog and the script."""
    root = tmp_path
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "assemble_changelog.py")
    (root / "CHANGELOG.md").write_text(collapse_text(REPEATED), encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "checkout", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feat/thing")
    return root


def edit_unreleased(root: Path) -> None:
    path = root / "CHANGELOG.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("- First added.", "- Edited."),
        encoding="utf-8",
    )


def test_a_branch_that_edits_unreleased_without_a_fragment_fails(
    repository: Path,
) -> None:
    edit_unreleased(repository)
    done = script(repository, "check")
    assert done.returncode == 1
    assert "adds no fragment" in done.stderr


def test_the_same_edit_with_an_untracked_or_committed_fragment_passes(
    repository: Path,
) -> None:
    edit_unreleased(repository)
    fragment = repository / "changes" / "feat--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Added\n\n- Thing.\n", encoding="utf-8")
    assert script(repository, "check").returncode == 0
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "work")
    assert script(repository, "check").returncode == 0


def test_a_branch_with_a_fragment_and_no_changelog_edit_passes(
    repository: Path,
) -> None:
    fragment = repository / "changes" / "feat--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Fixed\n\n- Thing.\n", encoding="utf-8")
    assert script(repository, "check").returncode == 0


def test_a_branch_with_no_change_passes(repository: Path) -> None:
    assert script(repository, "check").returncode == 0


def test_a_release_commit_that_deletes_fragments_passes(repository: Path) -> None:
    fragment = repository / "changes" / "old--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Fixed\n\n- Old thing.\n", encoding="utf-8")
    git(repository, "checkout", "-q", "main")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "a merged fragment")
    git(repository, "checkout", "-q", "-b", "release/0.1.0")
    done = script(repository, "release", "0.1.0", "2026-11-15")
    assert done.returncode == 0
    assert "removed changes/old--thing.md" in done.stdout
    assert not fragment.exists()
    text = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0] - 2026-11-15" in text
    assert "- Old thing." in text
    assert script(repository, "check").returncode == 0


def test_an_invalid_fragment_fails_the_check_naming_it(repository: Path) -> None:
    broken = repository / "changes" / "feat--thing.md"
    broken.parent.mkdir()
    broken.write_text("no type here\n", encoding="utf-8")
    done = script(repository, "check")
    assert done.returncode == 1
    assert "feat--thing.md" in done.stderr


def test_without_a_base_branch_the_rule_says_so_and_passes(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / "assemble_changelog.py")
    (tmp_path / "CHANGELOG.md").write_text(collapse_text(REPEATED), encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "checkout", "-q", "-b", "side")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    edit_unreleased(tmp_path)
    done = script(tmp_path, "check")
    assert done.returncode == 0
    assert "not checked" in done.stdout


def test_preview_and_a_dry_run_write_nothing(repository: Path) -> None:
    fragment = repository / "changes" / "feat--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Added\n\n- Thing.\n", encoding="utf-8")
    before = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
    preview = script(repository, "preview")
    assert preview.returncode == 0
    assert "- Thing." in preview.stdout
    assert "## [" not in preview.stdout
    dry = script(repository, "release", "0.1.0", "2026-11-15", "--dry-run")
    assert dry.returncode == 0
    assert "## [0.1.0] - 2026-11-15" in dry.stdout
    assert (repository / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert fragment.exists()


def test_a_bad_release_leaves_the_changelog_and_fragments_alone(
    repository: Path,
) -> None:
    fragment = repository / "changes" / "feat--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Added\n\n- Thing.\n", encoding="utf-8")
    before = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
    done = script(repository, "release", "next", "2026-11-15")
    assert done.returncode == 1
    assert "not a version" in done.stderr
    assert (repository / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert fragment.exists()


def test_the_gate_runs_the_rule() -> None:
    text = (REPOSITORY / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert "scripts/assemble_changelog.py check" in text
    assert text.index("assemble_changelog.py check") < text.index("uv sync --locked")
