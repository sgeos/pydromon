"""Rollback floor fragments, round 17, item 3.

The functions take the repository's root, so they are tested against a real git
history made here, with a merge whose subject is GitHub's, as ``main`` holds them.
"""

import shutil
from pathlib import Path

import pytest
from assemble_floors import (
    HEADER,
    FloorError,
    check,
    main,
    parse,
    release,
    rows,
    written,
)
from dromon import spawn

TABLE = f"""# Event log

{HEADER}
|---|---|---|---|
| `older` | `1111111` | `2222222` | `feat/older`, #1 |

After the table.
"""


def git(root: Path, *arguments: str) -> str:
    found = shutil.which("git")
    assert found is not None
    return spawn.run(
        [found, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    ).stdout.strip()


def fragment(root: Path, name: str, text: str) -> Path:
    path = root / "docs/protocol/floors" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src/dromon").mkdir(parents=True)
    (root / "docs/protocol").mkdir(parents=True)
    (root / "docs/protocol/EVENT_LOG.md").write_text(TABLE, encoding="utf-8")
    (root / "src/dromon/codec.py").write_text('FIELDS = ("seq",)\n', encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "first")
    return root


ENTRY = 'Change: `run_started.tree`, written with a tree\nSearch: "tree",\n'


def branch_with_change(root: Path) -> str:
    """A branch whose one commit adds a field and its fragment, and that commit."""
    git(root, "switch", "-q", "-c", "feat/tree")
    (root / "src/dromon/codec.py").write_text(
        'FIELDS = ("seq", "tree",)\n', encoding="utf-8"
    )
    fragment(root, "feat--tree", ENTRY)
    git(root, "add", ".")
    git(root, "commit", "-qm", "feat: tree")
    return git(root, "rev-parse", "--short=7", "HEAD")


def merge(root: Path) -> str:
    git(root, "switch", "-q", "main")
    git(
        root,
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "Merge pull request #7 from sgeos/feat/tree",
        "feat/tree",
    )
    return git(root, "rev-parse", "--short=7", "HEAD")


def test_a_fragment_s_entries_are_read(tmp_path: Path) -> None:
    path = fragment(
        tmp_path,
        "feat--two",
        ENTRY
        + '\nChange: a value\nSearch: VALUE = "value"\nPath: src/dromon/model.py\n',
    )
    first, second = parse(path)
    assert (first.change, first.search, first.path) == (
        "`run_started.tree`, written with a tree",
        '"tree",',
        "src/dromon/codec.py",
    )
    assert (second.fragment, second.path) == ("feat--two", "src/dromon/model.py")


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("Feat--x", ENTRY, "in lowercase"),
        ("feat--x", "Change: a\n", "a Change and a Search"),
        ("feat--x", "Search: a\n", "a Change and a Search"),
        ("feat--x", ENTRY + "Search: twice\n", "unexpected line"),
        ("feat--x", "Colour: blue\nSearch: a\n", "unexpected line"),
        ("feat--x", "Change: a | b\nSearch: a\n", "without a pipe"),
        ("feat--x", "Change:  \nSearch: a\n", "without a pipe"),
        ("feat--x", "Change a\nSearch: a\n", "unexpected line"),
    ],
)
def test_a_fragment_of_another_form_is_refused(
    tmp_path: Path, name: str, text: str, message: str
) -> None:
    with pytest.raises(FloorError, match=message):
        parse(fragment(tmp_path, name, text))


def test_check_requires_each_search_in_the_tree(repository: Path) -> None:
    check(repository)
    fragment(repository, "feat--tree", ENTRY)
    with pytest.raises(FloorError, match="does not hold its Search"):
        check(repository)
    fragment(repository, "feat--tree", ENTRY + "Path: src/dromon/absent.py\n")
    with pytest.raises(FloorError, match="is not a file"):
        check(repository)


@pytest.mark.parametrize(
    "row",
    [
        "| `x` | `abc1234` | Pending | `feat/x` |",
        "| `x` | Uncommitted | Pending | `feat/x` |",
    ],
)
def test_check_refuses_a_pending_row_in_the_table(repository: Path, row: str) -> None:
    table = repository / "docs/protocol/EVENT_LOG.md"
    table.write_text(TABLE.replace("#1 |\n", f"#1 |\n{row}\n"), encoding="utf-8")
    with pytest.raises(FloorError, match="belongs in a fragment"):
        check(repository)


def test_check_requires_the_table_header_once(repository: Path) -> None:
    table = repository / "docs/protocol/EVENT_LOG.md"
    table.write_text(TABLE + HEADER + "\n", encoding="utf-8")
    with pytest.raises(FloorError, match="header once"):
        check(repository)


def test_an_uncommitted_change_shows_so(repository: Path) -> None:
    (repository / "src/dromon/codec.py").write_text('FIELDS = ("tree",)\n', "utf-8")
    fragment(repository, "feat--tree", ENTRY)
    (found,) = rows(repository, "main")
    assert (found.first, found.floor, found.branch) == (
        "Uncommitted",
        "Pending",
        "`feat/tree`",
    )


def test_a_branch_not_merged_is_pending_with_its_commit(repository: Path) -> None:
    first = branch_with_change(repository)
    (found,) = rows(repository, "main")
    assert (found.first, found.floor) == (f"`{first}`", "Pending")
    with pytest.raises(FloorError, match="not merged into the base yet"):
        release(repository, "main")


def test_a_release_writes_the_row_from_the_history(repository: Path) -> None:
    first = branch_with_change(repository)
    floor = merge(repository)
    (found,) = rows(repository, "main")
    expected = f"| `run_started.tree`, written with a tree | `{first}` | `{floor}` |"
    assert written(found) == f"{expected} `feat/tree`, #7 |"
    assert release(repository, "main") == ["feat--tree"]
    text = (repository / "docs/protocol/EVENT_LOG.md").read_text(encoding="utf-8")
    assert f"#1 |\n{expected} `feat/tree`, #7 |\n\nAfter the table." in text
    assert not (repository / "docs/protocol/floors/feat--tree.md").exists()
    check(repository)
    with pytest.raises(FloorError, match="no fragment to release"):
        release(repository, "main")


def test_a_plain_git_merge_names_its_branch(repository: Path) -> None:
    branch_with_change(repository)
    git(repository, "switch", "-q", "main")
    git(repository, "merge", "--no-ff", "-q", "--no-edit", "feat/tree")
    (found,) = rows(repository, "main")
    assert found.branch == "`feat/tree`"


def test_no_base_is_said(repository: Path) -> None:
    branch_with_change(repository)
    with pytest.raises(FloorError, match="no base branch"):
        rows(repository, "absent")


@pytest.mark.parametrize(
    "value",
    [
        "/etc/hosts",
        "../../etc/hosts",
        "src/../../outside.py",
        "~/notes.txt",
        "src\\dromon\\codec.py",
    ],
)
def test_a_path_outside_the_repository_is_refused(tmp_path: Path, value: str) -> None:
    """Review of 0db077c, finding 3. The audit's two forms, and two more spellings."""
    path = fragment(
        tmp_path, "feat--x", f"Change: a\nSearch: localhost\nPath: {value}\n"
    )
    with pytest.raises(FloorError, match="relative to the repository, without"):
        parse(path)


def test_a_path_that_leads_out_through_a_link_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("localhost\n", encoding="utf-8")
    (repository / "src/linked.txt").symlink_to(outside)
    fragment(
        repository, "feat--x", "Change: a\nSearch: localhost\nPath: src/linked.txt\n"
    )
    with pytest.raises(FloorError, match="lies outside the repository"):
        check(repository)


@pytest.mark.parametrize(
    "name", [".DS_Store", "feat--b.md~", "feat--b.md.orig", "Feat--b.md", "notes.txt"]
)
def test_a_stray_file_is_ignored_with_a_warning(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """Review of 0db077c, finding 4. A file git need not know of, as Finder or an
    editor or a merge tool leaves, no longer fails the check, and is named."""
    directory = repository / "docs/protocol/floors"
    directory.mkdir(parents=True)
    (directory / name).write_text("not a fragment\n", encoding="utf-8")
    (directory / "README.md").write_text("# Floors\n", encoding="utf-8")
    monkeypatch.chdir(repository)
    assert main(["check"]) == 0
    error = capsys.readouterr().err
    assert f"warning: ignored docs/protocol/floors/{name}," in error
    assert "README.md" not in error


def test_a_directory_in_the_fragments_is_ignored(repository: Path) -> None:
    (repository / "docs/protocol/floors/feat--x.md").mkdir(parents=True)
    check(repository)


def test_the_command_reports_an_error_and_exits_1(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    assert main(["check"]) == 0
    fragment(repository, "feat--tree", ENTRY)
    assert main(["check"]) == 1
    assert "does not hold its Search" in capsys.readouterr().err
