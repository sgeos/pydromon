"""Changelog fragments, their assembly at a release, and the branch rule.

The assembler is a script of ``scripts/`` that the gate runs. Its functions are tested
directly, and its commands and the rule against a real git repository by running a copy
of it, since it finds its repository from its own place.
"""

import os
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
    perform_release,
    read_fragments,
    release_text,
    split_changelog,
    unreleased_text,
)
from dromon import spawn

SKIPPED_STATUS = 3
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


def test_a_release_refuses_a_version_already_released() -> None:
    # Review of 4da95bf, item 4.
    with pytest.raises(ChangelogError, match="already in the changelog"):
        release_text(REPEATED, [], "0.0.1", "2026-11-15")
    with pytest.raises(ChangelogError, match="already in the changelog"):
        release_text(
            RELEASED, [Fragment("a.md", "Fixed", "- x")], "0.1.0", "2026-11-16"
        )
    assert "## [0.1.1]" in release_text(
        RELEASED, [Fragment("a.md", "Fixed", "- x")], "0.1.1", "2026-11-16"
    )


@pytest.mark.parametrize(
    "date", ["2026-13-45", "2026-02-30", "2026-00-10", "2025-02-29", "0000-01-01"]
)
def test_a_release_refuses_a_date_that_is_not_on_the_calendar(date: str) -> None:
    with pytest.raises(ChangelogError, match="not a calendar date"):
        release_text(REPEATED, [], "0.1.0", date)


def test_a_leap_day_is_a_date() -> None:
    assert "## [0.1.0] - 2028-02-29" in release_text(
        REPEATED, [], "0.1.0", "2028-02-29"
    )


def test_fragments_join_their_type_in_name_order() -> None:
    merged = join_entries(
        {"Added": "- Old."},
        [Fragment("z.md", "Added", "- Z."), Fragment("a.md", "Added", "- A.")],
    )
    assert merged == {"Added": "- Old.\n- A.\n- Z."}


# The branch rule.


RELEASED = release_text(REPEATED, [], "0.1.0", "2026-11-15")


def test_the_rule_allows_an_untouched_changelog() -> None:
    assert branch_violation(REPEATED, REPEATED, []) is None


@pytest.mark.parametrize("removed", [[], [Fragment("feat--x.md", "Added", "- X.")]])
def test_the_rule_refuses_an_edit_to_unreleased_whatever_the_fragments(
    removed: list[Fragment],
) -> None:
    # Review of 4da95bf, item 6. Adding a fragment no longer excuses an edit.
    changed = REPEATED.replace("- First added.", "- Edited.")
    why = branch_violation(REPEATED, changed, removed)
    assert why is not None
    assert "changes/BRANCH.md" in why


def test_the_rule_ignores_an_edit_below_unreleased() -> None:
    changed = REPEATED.replace("- Old entry.", "- Old entry, reworded.")
    assert unreleased_text(REPEATED) == unreleased_text(changed)
    assert branch_violation(REPEATED, changed, []) is None


def test_the_rule_allows_a_lossless_regrouping() -> None:
    assert branch_violation(REPEATED, collapse_text(REPEATED), []) is None


def test_a_regrouping_that_changes_or_adds_an_entry_is_refused() -> None:
    tidy = collapse_text(REPEATED)
    reworded = tidy.replace("- First fixed.", "- First fixed, reworded.")
    added = tidy.replace("- First fixed.", "- First fixed.\n- Sneaked in.")
    dropped = tidy.replace("- First fixed.\n", "")
    for head in (reworded, added, dropped):
        assert branch_violation(REPEATED, head, []) is not None


def test_a_tidy_section_has_nothing_left_to_regroup() -> None:
    tidy = collapse_text(REPEATED)
    edited = tidy.replace("- First changed.", "- Edited.")
    assert branch_violation(tidy, edited, []) is not None
    assert branch_violation(tidy, tidy, []) is None


def test_the_rule_allows_a_release_with_or_without_fragments() -> None:
    fragment = Fragment("a.md", "Fixed", "- From a.")
    with_fragment = release_text(REPEATED, [fragment], "0.1.0", "2026-11-15")
    assert branch_violation(REPEATED, with_fragment, [fragment]) is None
    # Review of 4da95bf, F3c. Entries in Unreleased and no fragment still release.
    assert branch_violation(REPEATED, RELEASED, []) is None


def test_a_release_may_add_only_lines_its_fragments_and_unreleased_held() -> None:
    """Round 14, item 10. A release once kept every entry but could add one that no
    deleted fragment held."""
    fragment = Fragment("a.md", "Fixed", "- From a.\n  Continued.")
    honest = release_text(REPEATED, [fragment], "0.1.0", "2026-11-15")
    assert branch_violation(REPEATED, honest, [fragment]) is None
    sneaked = honest.replace("- From a.\n", "- From a.\n- Sneaked in.\n")
    assert branch_violation(REPEATED, sneaked, [fragment]) is not None
    # A fragment's line claimed by a release that did not delete that fragment.
    assert branch_violation(REPEATED, honest, []) is not None
    # Two copies of a line that one fragment held once.
    doubled = honest.replace("- From a.\n", "- From a.\n- From a.\n")
    assert branch_violation(REPEATED, doubled, [fragment]) is not None


def test_a_release_may_not_drop_a_fragments_entry_or_move_one_to_another_type() -> None:
    """Audit of round 14, T1 and T2."""
    fragment = Fragment("a.md", "Fixed", "- From a.\n  Continued.")
    other = Fragment("b.md", "Added", "- From b.")
    honest = release_text(REPEATED, [fragment, other], "0.1.0", "2026-11-15")
    assert branch_violation(REPEATED, honest, [fragment, other]) is None
    dropped = honest.replace("- From b.\n", "")
    assert "- From b." not in dropped
    assert branch_violation(REPEATED, dropped, [fragment, other]) is not None
    continuation_dropped = honest.replace("  Continued.\n", "", 1)
    assert (
        branch_violation(REPEATED, continuation_dropped, [fragment, other]) is not None
    )
    # The Fixed entry released under Added instead.
    moved = honest.replace("- From a.\n  Continued.\n", "").replace(
        "- From b.\n", "- From b.\n- From a.\n  Continued.\n"
    )
    assert moved.count("- From a.") == 1
    assert branch_violation(REPEATED, moved, [fragment, other]) is not None
    # An Unreleased entry moved to another type is refused as well.
    swapped = (
        RELEASED.replace("### Fixed", "### Spare")
        .replace("### Changed", "### Fixed")
        .replace("### Spare", "### Changed")
    )
    assert swapped != RELEASED
    assert branch_violation(REPEATED, swapped, []) is not None


def test_a_release_section_with_text_above_its_first_type_is_refused() -> None:
    head = RELEASED.replace(
        "## [0.1.0] - 2026-11-15\n", "## [0.1.0] - 2026-11-15\n\n- Loose.\n"
    )
    assert head != RELEASED
    assert branch_violation(REPEATED, head, []) is not None


def test_a_release_must_keep_every_entry_and_empty_unreleased() -> None:
    lost = RELEASED.replace("- First added.\n  Continued.\n", "")
    assert branch_violation(REPEATED, lost, []) is not None
    old_lost = RELEASED.replace("- Old entry.\n", "")
    assert branch_violation(REPEATED, old_lost, []) is not None
    refilled = RELEASED.replace(
        "## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n- Stays.\n"
    )
    assert branch_violation(REPEATED, refilled, []) is not None


def test_an_edit_that_adds_no_version_heading_is_not_a_release() -> None:
    emptied = REPEATED.replace(unreleased_text(REPEATED), "## [Unreleased]\n")
    assert (
        branch_violation(REPEATED, emptied, [Fragment("a.md", "Added", "- A.")])
        is not None
    )


def test_a_release_with_nothing_released_is_not_one() -> None:
    nothing = PREAMBLE + "\n## [Unreleased]\n\n## [0.0.1] - 2026-01-01\n"
    head = nothing.replace("## [0.0.1]", "## [0.0.2] - 2026-02-02\n\n## [0.0.1]")
    assert branch_violation(nothing, head, []) is None  # no change to Unreleased
    assert (
        branch_violation(
            nothing,
            head.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n- x\n"),
            [],
        )
        is not None
    )


# The commands and the rule against a real repository.


def git(root: Path, *arguments: str) -> str:
    done = spawn.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
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


def make_repository(root: Path, changelog: str) -> Path:
    """A repository on main holding ``changelog`` and the script, left on a branch."""
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "assemble_changelog.py")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "checkout", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feat/thing")
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository on main holding the collapsed changelog and the script."""
    return make_repository(tmp_path, collapse_text(REPEATED))


@pytest.fixture
def untidy_repository(tmp_path: Path) -> Path:
    """A repository whose changelog still has repeated headings."""
    return make_repository(tmp_path, REPEATED)


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
    assert "only a release or a lossless regrouping" in done.stderr


def test_the_same_edit_with_a_fragment_also_fails(repository: Path) -> None:
    # Review of 4da95bf, item 6. A fragment next to an edit is still an edit.
    edit_unreleased(repository)
    fragment = repository / "changes" / "feat--thing.md"
    fragment.parent.mkdir()
    fragment.write_text("Type: Added\n\n- Thing.\n", encoding="utf-8")
    assert script(repository, "check").returncode == 1
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "work")
    assert script(repository, "check").returncode == 1


def test_a_branch_that_only_regroups_unreleased_passes(
    untidy_repository: Path,
) -> None:
    done = script(untidy_repository, "collapse")
    assert done.returncode == 0
    assert script(untidy_repository, "check").returncode == 0
    git(untidy_repository, "commit", "-qam", "tidy")
    assert script(untidy_repository, "check").returncode == 0


def test_a_regrouping_that_also_changes_an_entry_fails(
    untidy_repository: Path,
) -> None:
    script(untidy_repository, "collapse")
    edit_unreleased(untidy_repository)
    assert script(untidy_repository, "check").returncode == 1


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


def test_a_release_with_only_unreleased_entries_passes_the_rule(
    repository: Path,
) -> None:
    done = script(repository, "release", "0.1.0", "2026-11-15")
    assert done.returncode == 0
    assert script(repository, "check").returncode == 0


def test_an_invalid_fragment_fails_the_check_naming_it(repository: Path) -> None:
    broken = repository / "changes" / "feat--thing.md"
    broken.parent.mkdir()
    broken.write_text("no type here\n", encoding="utf-8")
    done = script(repository, "check")
    assert done.returncode == 1
    assert "feat--thing.md" in done.stderr


def test_without_a_base_branch_the_rule_reports_a_skip_not_a_pass(
    tmp_path: Path,
) -> None:
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
    # Review of 4da95bf, item 2. Status 3 is a skip, which the gate shows as one.
    assert done.returncode == SKIPPED_STATUS
    assert "SKIPPED" in done.stdout
    assert "not checked" in done.stdout


def test_a_base_without_a_changelog_is_also_a_skip(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / "assemble_changelog.py")
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "checkout", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "CHANGELOG.md").write_text(collapse_text(REPEATED), encoding="utf-8")
    assert script(tmp_path, "check").returncode == SKIPPED_STATUS


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


# An atomic release. Review of 4da95bf, item 5.


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    """A directory with a changelog and two fragments, with no git."""
    (tmp_path / "CHANGELOG.md").write_text(collapse_text(REPEATED), encoding="utf-8")
    directory = tmp_path / "changes"
    directory.mkdir()
    (directory / "README.md").write_text("# Fragments\n", encoding="utf-8")
    (directory / "a.md").write_text("Type: Added\n\n- From a.\n", encoding="utf-8")
    (directory / "b.md").write_text("Type: Fixed\n\n- From b.\n", encoding="utf-8")
    return tmp_path


def snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root`` and its text, and every directory as empty text."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        found[name] = path.read_text(encoding="utf-8") if path.is_file() else "<dir>"
    return found


def failing_replace(
    monkeypatch: pytest.MonkeyPatch, fails: set[str], times: int = 1
) -> None:
    """Make ``os.replace`` raise for a source named in ``fails``, the first ``times``
    times each, and work otherwise."""
    real = os.replace
    counts: dict[str, int] = {}

    def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        name = Path(source).name
        if name in fails and counts.get(name, 0) < times:
            counts[name] = counts.get(name, 0) + 1
            raise OSError(f"cannot move {name}")
        real(source, target)

    monkeypatch.setattr(os, "replace", replace)


def test_a_release_moves_the_fragments_and_leaves_nothing_behind(
    release_root: Path,
) -> None:
    assert perform_release(release_root, "0.1.0", "2026-11-15", False) == 0
    text = (release_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- From a." in text
    assert "- From b." in text
    assert sorted(path.name for path in (release_root / "changes").iterdir()) == [
        "README.md"
    ]
    assert not (release_root / "CHANGELOG.md.release").exists()


def test_a_failure_moving_a_fragment_undoes_the_release(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(release_root)
    failing_replace(monkeypatch, {"b.md"})
    with pytest.raises(ChangelogError, match="undone, nothing changed"):
        perform_release(release_root, "0.1.0", "2026-11-15", False)
    monkeypatch.undo()
    assert snapshot(release_root) == before


def test_a_failure_replacing_the_changelog_undoes_the_moves(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(release_root)
    failing_replace(monkeypatch, {"CHANGELOG.md.release"})
    with pytest.raises(ChangelogError, match="undone, nothing changed"):
        perform_release(release_root, "0.1.0", "2026-11-15", False)
    monkeypatch.undo()
    assert snapshot(release_root) == before
    assert [fragment.name for fragment in read_fragments(release_root)] == [
        "a.md",
        "b.md",
    ]


def test_a_failure_writing_the_temporary_file_changes_nothing(
    release_root: Path,
) -> None:
    (release_root / "CHANGELOG.md.release").mkdir()
    before = snapshot(release_root)
    with pytest.raises(ChangelogError, match="nothing changed"):
        perform_release(release_root, "0.1.0", "2026-11-15", False)
    assert snapshot(release_root) == before


def test_a_leftover_staging_directory_refuses_the_release_and_changes_nothing(
    release_root: Path,
) -> None:
    (release_root / "changes" / ".released-0.1.0").mkdir()
    before = snapshot(release_root)
    with pytest.raises(ChangelogError, match="nothing changed"):
        perform_release(release_root, "0.1.0", "2026-11-15", False)
    assert snapshot(release_root) == before


def test_a_failed_undo_names_where_the_fragments_are(
    release_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The changelog cannot be replaced, and then a fragment cannot be moved back.
    real = os.replace

    def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(source).name == "CHANGELOG.md.release":
            raise OSError("cannot replace the changelog")
        if Path(source).name == "a.md" and Path(source).parent.name != "changes":
            raise OSError("cannot move a.md back")
        real(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(ChangelogError, match=r"a\.md could not be moved back"):
        perform_release(release_root, "0.1.0", "2026-11-15", False)
    monkeypatch.undo()
    assert (release_root / "changes" / ".released-0.1.0" / "a.md").exists()
    assert (release_root / "changes" / "b.md").exists()
    assert not (release_root / "CHANGELOG.md.release").exists()


def test_a_failure_removing_the_staging_directory_leaves_a_finished_release(
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    assert perform_release(release_root, "0.1.0", "2026-11-15", False) == 0
    monkeypatch.undo()
    assert "could not be removed" in capsys.readouterr().err
    text = (release_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0] - 2026-11-15" in text
    assert (release_root / "changes" / ".released-0.1.0" / "a.md").exists()
    # The leftovers are not fragments, so a rerun cannot release them twice. The same
    # version is refused, and the next one has nothing from them.
    assert read_fragments(release_root) == []
    with pytest.raises(ChangelogError, match="already in the changelog"):
        perform_release(release_root, "0.1.0", "2026-11-16", False)
    (release_root / "changes" / "c.md").write_text(
        "Type: Fixed\n\n- From c.\n", encoding="utf-8"
    )
    assert perform_release(release_root, "0.1.1", "2026-11-16", False) == 0
    after = (release_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert after.count("- From a.") == 1
    assert after.count("- From c.") == 1


def test_a_dry_run_changes_nothing(release_root: Path) -> None:
    before = snapshot(release_root)
    assert perform_release(release_root, "0.1.0", "2026-11-15", True) == 0
    assert snapshot(release_root) == before


# The gate and the documents.


UV_STAND_IN = """#!/bin/sh
case "$*" in *pyright*) echo "$*" >> "$UV_LOG" ;; esac
if [ -n "$UV_FAIL" ]; then
    case "$*" in *"$UV_FAIL"*) exit 3 ;; esac
fi
exit 0
"""
"""A `uv` that records each pyright call in `$UV_LOG`, fails a call containing
`$UV_FAIL`, and succeeds for everything else, so check.sh runs end to end."""


def gate_repository(tmp_path: Path, workflow: str | None = None) -> tuple[Path, Path]:
    """A repository holding the real check.sh and a stand-in `uv`, and the directory
    of the stand-in. `workflow` replaces the continuous integration workflow."""
    root = tmp_path / "repository"
    root.mkdir()
    (root / "scripts").mkdir()
    shutil.copy2(REPOSITORY / "scripts" / "check.sh", root / "scripts" / "check.sh")
    shutil.copy2(SCRIPT, root / "scripts" / "assemble_changelog.py")
    shutil.copy2(
        REPOSITORY / "scripts" / "ci_pythons.py", root / "scripts" / "ci_pythons.py"
    )
    (root / ".github" / "workflows").mkdir(parents=True)
    target = root / ".github" / "workflows" / "ci.yml"
    if workflow is None:
        shutil.copy2(REPOSITORY / ".github" / "workflows" / "ci.yml", target)
    else:
        target.write_text(workflow, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(collapse_text(REPEATED), encoding="utf-8")
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    ignore = "/tm" + "p/*\n!/tm" + "p/.gitkeep\n"
    (root / ".gitignore").write_text(ignore, encoding="utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / ".gitkeep").write_text("", encoding="utf-8")
    stand_in = tmp_path / "bin"
    stand_in.mkdir()
    (stand_in / "uv").write_text(UV_STAND_IN, encoding="utf-8")
    (stand_in / "uv").chmod(0o755)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "checkout", "-q", "-b", "side")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    return root, stand_in


def run_gate(
    root: Path, stand_in: Path, **extra: str
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PATH": f"{stand_in}:{os.environ['PATH']}", **extra}
    return subprocess.run(  # noqa: S603
        ["bash", "scripts/check.sh"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def test_the_gate_shows_a_skip_as_a_skip(tmp_path: Path) -> None:
    """Run the real check.sh with a stand-in `uv`, so the gate's own lines are read."""
    root, stand_in = gate_repository(tmp_path)
    environment = {**os.environ, "PATH": f"{stand_in}:{os.environ['PATH']}"}
    skipped = subprocess.run(  # noqa: S603
        ["bash", "scripts/check.sh"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert skipped.returncode == 0, skipped.stderr
    assert "SKIPPED: the changelog rule had no base branch" in skipped.stdout
    assert "ok: changelog fragments" not in skipped.stdout
    git(root, "branch", "main")
    based = subprocess.run(  # noqa: S603
        ["bash", "scripts/check.sh"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert based.returncode == 0, based.stderr
    assert "ok: changelog fragments are valid" in based.stdout
    assert "SKIPPED" not in based.stdout
    (root / "CHANGELOG.md").write_text(
        (root / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .replace("- First added.", "- Edited."),
        encoding="utf-8",
    )
    refused = subprocess.run(  # noqa: S603
        ["bash", "scripts/check.sh"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert refused.returncode == 1
    assert "only a release or a lossless regrouping" in refused.stderr


def test_continuous_integration_fetches_the_history_the_rule_needs() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    test_job = workflow.split("\n  workflows:")[0]
    assert "fetch-depth: 0" in test_job
    assert "origin/main" in test_job.split("fetch-depth: 0")[0]


@pytest.mark.parametrize(
    "path", ["CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md", "CLAUDE.md"]
)
def test_the_contributor_documents_describe_fragments(path: str) -> None:
    text = (REPOSITORY / path).read_text(encoding="utf-8")
    assert "changes/" in text
    assert "entry under `[Unreleased]`" not in text


# Round 15 review, G4. The gate's pyright loop, run end to end with a stand-in `uv`.

CALLS = [
    "run --no-sync pyright --pythonplatform Linux --pythonversion 3.12",
    "run --no-sync pyright --pythonplatform Darwin --pythonversion 3.12",
    "run --no-sync pyright --pythonplatform Linux --pythonversion 3.15",
    "run --no-sync pyright --pythonplatform Darwin --pythonversion 3.15",
]


def test_the_gate_runs_pyright_for_every_version_and_platform(tmp_path: Path) -> None:
    root, stand_in = gate_repository(tmp_path)
    log = tmp_path / "uv.log"
    done = run_gate(root, stand_in, UV_LOG=str(log))
    assert done.returncode == 0, done.stderr
    assert log.read_text(encoding="utf-8").splitlines() == CALLS
    assert "ok: pyright strict, for Linux and macOS, on Python 3.12 3.15" in done.stdout


@pytest.mark.parametrize("failing", CALLS)
def test_the_gate_fails_when_pyright_fails_for_any_one_version_or_platform(
    tmp_path: Path, failing: str
) -> None:
    root, stand_in = gate_repository(tmp_path)
    log = tmp_path / "uv.log"
    needle = failing.removeprefix("run --no-sync pyright ")
    done = run_gate(root, stand_in, UV_LOG=str(log), UV_FAIL=needle)
    assert done.returncode != 0
    assert "ok: pyright" not in done.stdout and "ok: pytest" not in done.stdout


@pytest.mark.parametrize(
    "workflow",
    [
        "name: CI\n",
        "python:\n  - '3.12'\n",
        'python: ["3.12"]\ninclude:\n  - python: "3.13"\n',
    ],
)
def test_the_gate_fails_without_running_pyright_when_the_versions_cannot_be_read(
    tmp_path: Path, workflow: str
) -> None:
    root, stand_in = gate_repository(tmp_path, workflow)
    log = tmp_path / "uv.log"
    done = run_gate(root, stand_in, UV_LOG=str(log))
    assert done.returncode != 0
    assert "could not be read" in done.stdout + done.stderr
    assert not log.exists(), "pyright ran although the versions could not be read"
    assert "ok: pyright" not in done.stdout


def test_the_gate_does_not_default_a_version_when_the_reader_fails(
    tmp_path: Path,
) -> None:
    """A reader that prints a version and then fails must not pass the gate, which
    pins the failed-read check against being turned into a default."""
    root, stand_in = gate_repository(tmp_path)
    (root / "scripts" / "ci_pythons.py").write_text(
        "import sys\nprint('3.12')\nsys.exit(1)\n", encoding="utf-8"
    )
    log = tmp_path / "uv.log"
    done = run_gate(root, stand_in, UV_LOG=str(log))
    assert done.returncode != 0
    assert not log.exists() and "ok: pyright" not in done.stdout


def test_the_gate_fails_when_the_reader_succeeds_with_no_version(
    tmp_path: Path,
) -> None:
    """Pins the check that at least one version was checked, which no workflow reaches
    through the real reader, since it already fails on none."""
    root, stand_in = gate_repository(tmp_path)
    (root / "scripts" / "ci_pythons.py").write_text("", encoding="utf-8")
    log = tmp_path / "uv.log"
    done = run_gate(root, stand_in, UV_LOG=str(log))
    assert done.returncode != 0
    assert "lists no Python version" in done.stdout + done.stderr
    assert not log.exists() and "ok: pyright" not in done.stdout
