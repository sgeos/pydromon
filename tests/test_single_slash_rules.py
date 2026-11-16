"""Claude Code permission rules that name an absolute path with one leading slash.
**Round 16, at the operator's request, from another project's report.** One slash
makes the path relative to the project and matches nothing, silently, while two,
``Read(//abs/path)``, is absolute."""

from pathlib import Path

import pytest

from dromon import cli
from dromon.settings import parse_settings, single_slash_rules


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A stand-in for the filesystem root with ``srv``, ``etc`` and ``tmp``."""
    for name in ("srv", "etc", "tmp"):
        (tmp_path / name).mkdir()
    return tmp_path


def rules(root: Path, **lists: list[str]) -> list[tuple[str, str, str]]:
    return single_slash_rules({"permissions": lists}, str(root))


@pytest.mark.parametrize(
    ("rule", "corrected"),
    [
        ("Read(/srv/me/.ssh/**)", "Read(//srv/me/.ssh/**)"),
        ("Edit(/etc/hosts)", "Edit(//etc/hosts)"),
        ("Write(/tmp/x/**)", "Write(//tmp/x/**)"),
        ("Glob(/srv/**)", "Glob(//srv/**)"),
        ("Grep(/etc/*)", "Grep(//etc/*)"),
        ("NotebookEdit(/srv/me/n.ipynb)", "NotebookEdit(//srv/me/n.ipynb)"),
    ],
)
def test_a_one_slash_absolute_path_is_reported_with_its_correction(
    root: Path, rule: str, corrected: str
) -> None:
    assert rules(root, deny=[rule]) == [("deny", rule, corrected)]


@pytest.mark.parametrize(
    "rule",
    [
        "Read(//srv/me/.ssh/**)",  # absolute, written correctly
        "Read(~/.ssh/**)",  # home
        "Read(./notes/**)",  # relative to the working directory
        "Read(notes/**)",
        "Read(/src/**)",  # project-relative on purpose, no such directory at the root
        "Read(/)",
        "Read(/*.md)",
        "Bash(/etc/init stop)",  # not a tool that names files
        "WebFetch(domain:/etc)",
        "Read",  # no specifier
        "mcp__game__move",
    ],
)
def test_a_rule_that_is_not_a_one_slash_absolute_path_is_left_alone(
    root: Path, rule: str
) -> None:
    assert rules(root, deny=[rule], allow=[rule], ask=[rule]) == []


def test_each_list_is_reported_in_the_order_of_the_document(root: Path) -> None:
    found = rules(
        root,
        allow=["Read(/etc/a)"],
        ask=["Edit(/etc/b)"],
        deny=["Read(/etc/c)", "Read(//etc/d)", "Write(/srv/e)"],
    )
    assert [(kind, rule) for kind, rule, _ in found] == [
        ("allow", "Read(/etc/a)"),
        ("ask", "Edit(/etc/b)"),
        ("deny", "Read(/etc/c)"),
        ("deny", "Write(/srv/e)"),
    ]


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"permissions": "x"},
        {"permissions": {"deny": "Read(/etc/a)"}},
        {"permissions": {"deny": [1, None, {"a": 1}]}},
        {"permissions": {"other": ["Read(/etc/a)"]}},
    ],
)
def test_a_document_of_another_shape_gives_nothing_and_never_raises(
    root: Path, document: dict[str, object]
) -> None:
    assert single_slash_rules(document, str(root)) == []


def test_the_real_root_is_the_default_and_a_missing_directory_is_not_one() -> None:
    assert (
        single_slash_rules(
            {"permissions": {"deny": ["Read(/definitely-not-a-root-directory/x)"]}}
        )
        == []
    )


def test_a_validated_document_is_read_the_same_way(root: Path) -> None:
    settings = parse_settings({"permissions": {"deny": ["Read(/etc/passwd)"]}})
    assert settings is not None
    found = single_slash_rules(settings.document(), str(root))
    assert [rule for _, rule, _ in found] == ["Read(/etc/passwd)"]


def config_output(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> str:
    state = tmp_path / ".dromon"
    state.mkdir(exist_ok=True)
    (state / "dromon.toml").write_text("version = 1\n" + text)
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    return capsys.readouterr().out


def test_dromon_config_warns_of_a_project_rule_and_a_line_rule(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "[settings.permissions]\ndeny = ['Read(/etc/passwd)']\n"
        "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
        "[lines.a.settings.permissions]\nallow = ['Edit(/etc/hosts)']\n"
        "[lines.b]\nmodel = 'claude-haiku-5-5'\n"
        "[lines.b.settings.permissions]\ndeny = ['Read(//etc/hosts)']\n"
    )
    out = config_output(tmp_path, short_runtime, capsys, text)
    warnings = [line for line in out.splitlines() if "one leading slash" in line]
    assert len(warnings) == 2
    assert warnings[0].startswith("warning: the project's settings deny rule")
    assert (
        "Read(/etc/passwd)" in warnings[0]
        and "write Read(//etc/passwd)" in (warnings[0])
    )
    assert "A deny rule that matches nothing protects nothing" in warnings[0]
    assert warnings[1].startswith("warning: line a's settings allow rule")
    assert "protects nothing" not in warnings[1]


def test_dromon_config_says_nothing_of_correct_rules(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "[settings.permissions]\ndeny = ['Read(//etc/passwd)', 'Read(/src/**)']\n"
        "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
    )
    out = config_output(tmp_path, short_runtime, capsys, text)
    assert "one leading slash" not in out
