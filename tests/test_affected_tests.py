"""``scripts/affected_tests.py``, which lists the tests that name what a change touched.
**Round 19, item 2.**

Each test builds a small repository of its own, commits a base, changes the working
tree and asks the script what the change reaches. The script is also run as the
documentation says, with ``python3 -I`` and no dependencies, once.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

import affected_tests
from affected_tests import Failure
from dromon import spawn

SCRIPT = Path(affected_tests.__file__).resolve()

LIBRARY = '''"""A library."""

LIMIT = 5


def alpha() -> int:
    return 1


def beta() -> int:
    return 2


class Gamma:
    def delta(self) -> int:
        return 3

    def epsilon(self) -> int:
        return 4


def main() -> None:
    return None
'''


def git(root: Path, *arguments: str) -> str:
    return spawn.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository whose base commit holds a library and three tests, one naming each
    of ``alpha``, ``Gamma`` and ``launch``."""
    root = tmp_path / "repo"
    (root / "src" / "dromon").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "dromon" / "library.py").write_text(LIBRARY)
    (root / "tests" / "test_alpha.py").write_text(
        "from dromon import library\n\n\n"
        "def test_a():\n    assert library.alpha() == 1\n"
    )
    (root / "tests" / "test_gamma.py").write_text(
        "from dromon.library import Gamma\n\n\ndef test_g():\n    assert Gamma()\n"
    )
    (root / "tests" / "test_launch.py").write_text(
        "def test_l():\n    launcher = 'a launcher'\n    assert launcher\n"
    )
    git(root, "init", "--quiet")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "base")
    return root


def change(root: Path, old: str, new: str, name: str = "library.py") -> None:
    path = root / "src" / "dromon" / name
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def found(root: Path, *, base: str = "HEAD") -> dict[str, list[str]]:
    result, _ = affected_tests.affected(root, base)
    return result


def test_a_changed_function_names_the_tests_that_mention_it_and_no_others(
    repository: Path,
) -> None:
    change(repository, "return 1", "return 10")
    assert found(repository) == {"tests/test_alpha.py": ["alpha"]}


def test_a_changed_method_names_its_class_and_every_test_naming_either(
    repository: Path,
) -> None:
    change(repository, "return 3", "return 30")
    assert found(repository) == {"tests/test_gamma.py": ["Gamma"]}
    names = affected_tests.source_names(repository, "HEAD")
    assert names == {"delta", "Gamma"}


def test_a_deleted_function_is_named_from_the_old_side(repository: Path) -> None:
    change(repository, "def alpha() -> int:\n    return 1\n\n\n", "")
    assert "tests/test_alpha.py" in found(repository)
    assert "alpha" in affected_tests.source_names(repository, "HEAD")


def test_a_renamed_function_is_found_under_both_names(repository: Path) -> None:
    change(repository, "def beta()", "def bravo()")
    assert affected_tests.source_names(repository, "HEAD") == {"beta", "bravo"}


def test_a_new_file_names_everything_it_defines(repository: Path) -> None:
    (repository / "src" / "dromon" / "fresh.py").write_text(
        "LIMIT_FRESH = 1\n\n\ndef brand_new() -> int:\n    return 0\n"
    )
    (repository / "tests" / "test_fresh.py").write_text(
        "def test_f():\n    assert brand_new\n"
    )
    assert affected_tests.source_names(repository, "HEAD") == {
        "LIMIT_FRESH",
        "brand_new",
    }
    assert found(repository)["tests/test_fresh.py"] == ["brand_new"]


def test_a_changed_module_level_assignment_names_the_constant(
    repository: Path,
) -> None:
    (repository / "tests" / "test_limit.py").write_text("LIMIT = 'seen'\n")
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "more")
    change(repository, "LIMIT = 5", "LIMIT = 6")
    assert found(repository) == {"tests/test_limit.py": ["LIMIT"]}


def test_a_name_inside_a_function_is_not_a_definition(repository: Path) -> None:
    change(repository, "return 2", "local = 2\n    return local")
    assert affected_tests.source_names(repository, "HEAD") == {"beta"}


def test_only_whole_words_match(repository: Path) -> None:
    """``launch`` is in ``launcher`` and is not the word."""
    (repository / "src" / "dromon" / "extra.py").write_text(
        "def launch() -> None:\n    return None\n"
    )
    assert "tests/test_launch.py" not in found(repository)


def test_names_too_common_to_say_anything_are_not_searched(repository: Path) -> None:
    change(
        repository,
        "def main() -> None:\n    return None",
        "def main() -> None:\n    return 1",
    )
    result, names = affected_tests.affected(repository, "HEAD")
    assert result == {} and names == []
    change(repository, "return 1\n\n\ndef beta", "return 1\n\n\ndef __beta_x__")
    assert "__beta_x__" not in affected_tests.searchable(["__beta_x__", "_", "__"])


def test_a_test_the_change_edited_is_listed_even_when_it_names_nothing(
    repository: Path,
) -> None:
    (repository / "tests" / "test_launch.py").write_text("def test_l():\n    pass\n")
    (repository / "tests" / "test_new.py").write_text("def test_n():\n    pass\n")
    assert found(repository) == {"tests/test_launch.py": [], "tests/test_new.py": []}


def test_an_unchanged_tree_finds_nothing(repository: Path) -> None:
    assert found(repository) == {}


def test_changes_outside_the_source_are_not_read(repository: Path) -> None:
    (repository / "docs").mkdir()
    (repository / "docs" / "alpha.md").write_text("alpha\n")
    (repository / "src" / "dromon" / "data.json").write_text('{"alpha": 1}')
    assert found(repository) == {}


def test_a_source_that_does_not_parse_names_only_what_it_held_before(
    repository: Path,
) -> None:
    """The new side gives no definition, so the old side's names are all that is
    found, which is the superset a line would want. A new file that does not parse
    gives none, and the count on standard error says so."""
    (repository / "src" / "dromon" / "library.py").write_text("def broken(:\n")
    assert "broken" not in affected_tests.source_names(repository, "HEAD")
    assert "alpha" in affected_tests.source_names(repository, "HEAD")
    git(repository, "checkout", "--quiet", "--", ".")
    (repository / "src" / "dromon" / "fresh.py").write_text("def broken(:\n")
    result, names = affected_tests.affected(repository, "HEAD")
    assert names == [] and result == {}


def test_a_change_after_a_commit_is_read_against_the_given_base(
    repository: Path,
) -> None:
    change(repository, "return 1", "return 10")
    git(repository, "commit", "--quiet", "-am", "alpha")
    assert found(repository) == {}
    assert found(repository, base="HEAD~1") == {"tests/test_alpha.py": ["alpha"]}


def test_the_hunk_ranges_of_a_diff() -> None:
    diff = "\n".join(
        [
            "diff --git a/x b/x",
            "@@ -3,2 +3,4 @@ context",
            "@@ -10 +12 @@",
            "@@ -20,0 +25,3 @@",
            "@@ -30,2 +36,0 @@",
        ]
    )
    old, new = affected_tests.ranges(diff)
    assert old == [(3, 4), (10, 10), (30, 31)]
    assert new == [(3, 6), (12, 12), (25, 27)]


def test_the_definitions_of_a_source_with_their_lines_and_depths() -> None:
    parts = affected_tests.definitions(LIBRARY)
    names = {name: (first, last, depth) for name, first, last, depth in parts}
    assert names["LIMIT"][2] == 0 and names["alpha"][2] == 0
    assert names["delta"][2] == 1 and names["Gamma"][2] == 0
    assert names["delta"][0] > names["Gamma"][0]
    assert names["delta"][1] <= names["Gamma"][1]


def test_an_unknown_base_is_a_failure_and_the_command_exits_one(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(Failure):
        found(repository, base="no-such-commit")
    status = affected_tests.main(["no-such-commit", "--root", str(repository)])
    assert status == 1
    assert "affected_tests: git" in capsys.readouterr().err


def test_a_missing_base_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        affected_tests.main([])
    assert raised.value.code == 2


@pytest.mark.parametrize("bound", ["MAX_NAMES", "MAX_FILES", "MAX_TEST_FILES"])
def test_a_bound_that_is_passed_fails_and_never_answers_from_part_of_the_change(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bound: str,
) -> None:
    change(repository, "return 1", "return 10")
    monkeypatch.setattr(affected_tests, bound, 0)
    assert affected_tests.main(["HEAD", "--root", str(repository)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "past the bound" in captured.err


def test_the_output_forms(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    change(repository, "return 1", "return 10")
    root = str(repository)
    assert affected_tests.main(["HEAD", "--root", root]) == 0
    plain = capsys.readouterr()
    assert plain.out == "tests/test_alpha.py\n"
    assert "1 names searched, 1 test files" in plain.err
    assert affected_tests.main(["HEAD", "--why", "--root", root]) == 0
    assert capsys.readouterr().out == "tests/test_alpha.py: alpha\n"
    assert affected_tests.main(["HEAD", "--names", "--root", root]) == 0
    names = capsys.readouterr()
    assert names.out == "alpha\n" and "1 names" in names.err


def test_the_script_runs_as_documented_with_the_system_interpreter(
    repository: Path,
) -> None:
    """``python3 -I`` and no dependencies, the way a line in a sandbox runs it."""
    change(repository, "return 1", "return 10")
    done = spawn.run(
        [sys.executable, "-I", str(SCRIPT), "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "tests/test_alpha.py\n"


def test_the_script_imports_only_the_standard_library() -> None:
    import ast

    tree = ast.parse(SCRIPT.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= set(sys.stdlib_module_names), imported


NESTED = """\
import sys

if sys.platform == "linux":

    class Watch:
        def close(self) -> None:
            return None

    LIMIT_IF = 1
else:

    class Watch:
        def close(self) -> None:
            return None

try:
    import missing
except ImportError:
    def fallback() -> None:
        return None

with open(__file__) as handle:
    HELD = handle

for index in range(2):
    LOOPED = index

while False:
    def never() -> None:
        return None

match sys.platform:
    case "linux":
        MATCHED = 1


def outer() -> None:
    if True:
        def inner() -> None:
            return None
        inside = 1
"""


def test_definitions_inside_module_level_compound_statements_are_found() -> None:
    """Round 19, audit. A class chosen by an ``if``, as ``ExitWatch`` is in the
    launcher, or a function defined in a ``try``, ``with``, loop or ``match``, is a
    definition at the depth of the statement that holds it. A name bound inside a
    function is still not one."""
    found = {name: depth for name, _, _, depth in affected_tests.definitions(NESTED)}
    for name in ("Watch", "LIMIT_IF", "fallback", "HELD", "LOOPED", "never", "MATCHED"):
        assert found[name] == 0, name
    assert found["close"] == 1 and found["outer"] == 0 and found["inner"] == 1
    assert "inside" not in found
    watches = [d for d in affected_tests.definitions(NESTED) if d[0] == "Watch"]
    assert len(watches) == 2, "both branches define it"


def test_exitwatch_in_the_launcher_is_a_definition() -> None:
    launcher = Path(affected_tests.__file__).resolve().parent.parent
    source = (launcher / "src" / "dromon" / "launcher.py").read_text()
    watches = [d for d in affected_tests.definitions(source) if d[0] == "ExitWatch"]
    assert len(watches) == 2 and {d[3] for d in watches} == {0}


def test_a_change_inside_a_conditional_class_names_it_and_the_tests_naming_it(
    repository: Path,
) -> None:
    library = repository / "src" / "dromon" / "library.py"
    library.write_text(
        library.read_text()
        + "\nif True:\n\n    class Watch:\n        def close(self) -> int:\n"
        "            return 1\n"
    )
    (repository / "tests" / "test_watch.py").write_text("def test_w():\n    Watch\n")
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "watch")
    change(
        repository,
        "def close(self) -> int:\n            return 1",
        "def close(self) -> int:\n            return 2",
    )
    assert found(repository) == {"tests/test_watch.py": ["Watch"]}


def test_a_base_that_begins_with_a_dash_is_a_revision_and_never_an_option(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--output`` would make ``git diff`` write a file. After ``--end-of-options``
    it is a name to look up, which is not found."""
    change(repository, "return 1", "return 10")
    target = repository / "planted.txt"
    with pytest.raises(SystemExit) as refused:
        affected_tests.main([f"--output={target}", "--root", str(repository)])
    assert refused.value.code == 2, "argparse takes it for an option and refuses"
    status = affected_tests.main(
        ["--root", str(repository), "--", f"--output={target}"]
    )
    assert status == 1
    assert not target.exists()
    assert "affected_tests: git" in capsys.readouterr().err
    with pytest.raises(Failure):
        found(repository, base=f"--output={target}")
    assert not target.exists()


def test_no_external_diff_or_text_conversion_program_runs(repository: Path) -> None:
    """A repository's configuration can name a program for ``git diff``, and a listing
    of changed names has no use for one."""
    marker = repository.parent / "ran.txt"
    program = repository.parent / "program.sh"
    program.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    program.chmod(0o700)
    git(repository, "config", "diff.external", str(program))
    git(repository, "config", "diff.conv.textconv", str(program))
    (repository / ".gitattributes").write_text("*.py diff=conv\n")
    change(repository, "return 1", "return 10")
    assert found(repository) == {"tests/test_alpha.py": ["alpha"]}
    assert not marker.exists()


def test_a_test_file_that_is_not_a_regular_file_is_skipped_without_following(
    repository: Path,
) -> None:
    """A link to a file that names the function, a link to a directory of such files,
    and a pipe that would block a reader, none of which a checkout holds."""
    change(repository, "return 1", "return 10")
    outside = repository.parent / "outside"
    outside.mkdir()
    (outside / "test_outside.py").write_text("alpha\n")
    (repository / "tests" / "test_link.py").symlink_to(outside / "test_outside.py")
    (repository / "tests" / "linked").symlink_to(outside, target_is_directory=True)
    pipe = repository / "tests" / "test_pipe.py"
    os.mkfifo(pipe)
    assert stat.S_ISFIFO(pipe.lstat().st_mode)
    assert found(repository) == {"tests/test_alpha.py": ["alpha"]}


def test_a_source_that_is_not_utf8_is_a_clear_error_and_not_a_traceback(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = repository / "src" / "dromon" / "library.py"
    library.write_bytes(b"def alpha():\n    return '\xff'\n")
    assert affected_tests.main(["HEAD", "--root", str(repository)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "library.py is not valid UTF-8 source" in captured.err
    assert "Traceback" not in captured.err


def test_an_old_side_that_is_not_utf8_is_a_clear_error_too(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = repository / "src" / "dromon" / "library.py"
    library.write_bytes(b"def alpha():\n    return '\xff'\n")
    git(repository, "commit", "--quiet", "-am", "latin")
    library.write_bytes(b"def alpha():\n    return 'a'\n")
    assert affected_tests.main(["HEAD", "--root", str(repository)]) == 1
    assert "at HEAD is not valid UTF-8 source" in capsys.readouterr().err


def test_a_test_file_that_is_not_utf8_is_still_searched(repository: Path) -> None:
    """Only names matter in a test file, so a bad byte is replaced and not fatal."""
    change(repository, "return 1", "return 10")
    (repository / "tests" / "test_bytes.py").write_bytes(b"# \xff\nalpha\n")
    assert found(repository)["tests/test_bytes.py"] == ["alpha"]
