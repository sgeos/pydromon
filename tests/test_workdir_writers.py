"""Which lines can write their working directory, for the warning of ``dromon config``
about writers that share one. Round 9, item 2, and its review, F16 and F17, by which
the answer follows each engine's own confinement."""

from pathlib import Path

import pytest

from dromon import antigravity, cli, codex, cursor, grok, pi
from dromon.driver import ENGINE_OF
from dromon.engine import ENGINE_NAME as CLAUDE_CODE
from dromon.lineconfig import (
    CLAUDE_CODE_ENGINE,
    SANDBOXED_WRITERS,
    UNCONFINED_ENGINES,
    can_write_workdir,
    config_from_document,
)


@pytest.mark.parametrize(
    ("document", "writes"),
    [
        ({"builtin_tools": ["Read", "Write"]}, True),
        ({"builtin_tools": ["Bash"]}, True),
        ({"tool_policy": "allow_all"}, True),
        ({"tool_policy": "allowlist", "allowed_tools": ["Edit"]}, True),
        ({"builtin_tools": ["Read", "Grep"]}, False),
        ({"builtin_tools": ["Read", "Bash"], "workdir_access": "read"}, False),
        ({"tool_policy": "allow_all", "workdir_access": "read"}, False),
        (
            {
                "tool_policy": "allowlist",
                "allowed_tools": ["mcp__game__*"],
                "mcp_servers": {"game": {"type": "stdio", "command": "/bin/true"}},
            },
            False,
        ),
    ],
)
def test_a_claude_code_line_writes_only_with_write_access_and_a_file_tool(
    document: dict[str, object], writes: bool
) -> None:
    assert can_write_workdir(config_from_document(document), CLAUDE_CODE) is writes


@pytest.mark.parametrize("engine", [codex.ENGINE_NAME, grok.ENGINE_NAME])
@pytest.mark.parametrize(
    ("document", "writes"),
    [
        ({"builtin_tools": ["Bash"]}, False),
        ({"builtin_tools": ["Bash"], "tool_policy": "allow_all"}, False),
        ({"tool_policy": "allow_all"}, False),
        ({"builtin_tools": ["Read", "Grep"]}, False),
        ({"builtin_tools": ["Edit"]}, True),
        ({"builtin_tools": ["Bash", "Write"]}, True),
        ({"builtin_tools": ["Write"], "workdir_access": "read"}, False),
    ],
)
def test_a_codex_or_grok_line_writes_only_with_edit_or_write(
    engine: str, document: dict[str, object], writes: bool
) -> None:
    """Round 9 review, F16. Their sandbox keeps the workspace read-only without
    ``Edit`` or ``Write``, so a shell alone cannot write it, under any policy."""
    assert can_write_workdir(config_from_document(document), engine) is writes


@pytest.mark.parametrize(
    "engine", [pi.ENGINE_NAME, cursor.ENGINE_NAME, antigravity.ENGINE_NAME]
)
def test_a_line_on_an_engine_that_cannot_confine_reads_is_a_writer(
    engine: str,
) -> None:
    """Round 9 review, F17. These engines take no built-in tools and refuse a
    read-only working directory, so a line of theirs with write access writes."""
    config = config_from_document({})
    assert can_write_workdir(config, engine) is True
    read_only = config_from_document({"workdir_access": "read"})
    assert can_write_workdir(read_only, engine) is False


def test_an_engine_dromon_does_not_know_is_counted_as_a_writer() -> None:
    config = config_from_document({"builtin_tools": ["Read"]})
    assert can_write_workdir(config, None) is True
    assert can_write_workdir(config, "some-engine") is True


def test_the_engine_tables_match_the_engines_themselves() -> None:
    """The tables are repeated in ``dromon.lineconfig`` because each engine module
    imports it, so this holds them equal to the engines' own, and holds that every
    engine with a packaged driver is in exactly one of them."""
    assert CLAUDE_CODE_ENGINE == CLAUDE_CODE
    assert SANDBOXED_WRITERS == {
        codex.ENGINE_NAME: codex.WRITE_TOOLS,
        grok.ENGINE_NAME: grok.WRITE_TOOLS,
    }
    assert {
        pi.ENGINE_NAME,
        cursor.ENGINE_NAME,
        antigravity.ENGINE_NAME,
    } == UNCONFINED_ENGINES
    known = {CLAUDE_CODE_ENGINE, *SANDBOXED_WRITERS, *UNCONFINED_ENGINES}
    assert set(ENGINE_OF.values()) == known
    assert not set(SANDBOXED_WRITERS) & UNCONFINED_ENGINES
    assert CLAUDE_CODE_ENGINE not in {*SANDBOXED_WRITERS, *UNCONFINED_ENGINES}


def test_dromon_config_warns_only_about_lines_that_can_write(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shape the defect was found in: a reader with a shell, a reviewer with a
    shell, a player with only an MCP server's tools, all at the project root, and
    one writer. None of the others can write, so nothing is warned of. A second
    writer is, and so is a Codex line with ``Write``, while a Codex line with a
    shell alone is not."""
    state = tmp_path / ".dromon"
    state.mkdir()
    lines = (
        "version = 1\n"
        "[lines.research]\nmodel = 'claude-opus-5-5'\n"
        "builtin_tools = ['Read', 'Bash']\nworkdir_access = 'read'\n"
        "[lines.review]\nmodel = 'claude-opus-5-5'\n"
        "builtin_tools = ['Read', 'Grep', 'Bash']\nworkdir_access = 'read'\n"
        "[lines.player]\nmodel = 'claude-haiku-5-5'\ntool_policy = 'allowlist'\n"
        "allowed_tools = ['mcp__game__*']\n"
        "[lines.player.mcp_servers.game]\ntype = 'stdio'\ncommand = '/bin/true'\n"
        "[lines.shell]\nmodel = 'gpt-6-luna'\nbuiltin_tools = ['Bash']\n"
        "[lines.builder]\nmodel = 'claude-opus-5-5'\nbuiltin_tools = ['Edit']\n"
    )
    (state / "dromon.toml").write_text(lines)
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert "warning:" not in capsys.readouterr().out
    (state / "dromon.toml").write_text(
        lines + "[lines.other]\nmodel = 'gpt-6-luna'\nbuiltin_tools = ['Write']\n"
    )
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert "warning: writing lines builder, other share workdir" in (
        capsys.readouterr().out
    )


# Round 16, at the operator's request. A line asking for read access that Claude Code
# cannot confine is refused at every turn, so ``dromon config`` says so at once.


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


MCP_LINE = (
    "[lines.player]\nmodel = 'claude-haiku-5-5'\nworkdir_access = 'read'\n"
    "builtin_tools = ['Read']\n"
    "[lines.player.mcp_servers.game]\ntype = 'stdio'\ncommand = '/bin/true'\n"
)


def test_dromon_config_warns_of_a_read_line_with_an_mcp_server(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = config_output(tmp_path, short_runtime, capsys, MCP_LINE)
    (warning,) = [line for line in out.splitlines() if "will be refused" in line]
    assert warning.startswith("warning: line player has workdir_access read")
    assert "cannot confine configured MCP servers" in warning


def test_the_warning_is_the_refusal_the_engine_builder_raises() -> None:
    """One function gives both, so the warning cannot promise less than a turn
    refuses. Each reason it can give is one the builder gives."""
    from dromon.scheduler import claude_read_only_refusal

    reads = {"workdir_access": "read", "builtin_tools": ["Read"]}
    cases: dict[str, dict[str, object]] = {
        "MCP servers": {
            "mcp_servers": {"g": {"type": "stdio", "command": "/bin/true"}}
        },
        "an allow-all policy": {"tool_policy": "allow_all"},
        "additional MCP tools": {
            "tool_policy": "allowlist",
            "allowed_tools": ["mcp__g__*"],
        },
        "project settings or plugins": {"project_settings": True},
    }
    for reason, extra in cases.items():
        refusal = claude_read_only_refusal(
            config_from_document({**reads, **extra}), None, None
        )
        assert refusal is not None and reason in refusal, reason
    clean = config_from_document(reads)
    assert claude_read_only_refusal(clean, None, None) is None


def test_a_project_hook_makes_every_read_line_refused(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "[settings.hooks]\nPreToolUse = []\n"
        "[lines.reader]\nmodel = 'claude-haiku-5-5'\nworkdir_access = 'read'\n"
        "builtin_tools = ['Read']\n"
    )
    out = config_output(tmp_path, short_runtime, capsys, text)
    assert "command hooks" in out and "line reader has workdir_access read" in out


@pytest.mark.parametrize(
    "text",
    [
        # Write access with an MCP server is not a read line, so nothing is refused.
        MCP_LINE.replace("workdir_access = 'read'\n", ""),
        # A read line with no server, allow-all policy or hook is allowed.
        "[lines.reader]\nmodel = 'claude-haiku-5-5'\nworkdir_access = 'read'\n"
        "builtin_tools = ['Read', 'Grep']\n",
        # Other engines refuse a read line for other reasons, or never.
        MCP_LINE.replace("claude-haiku-5-5", "gpt-6-luna"),
    ],
)
def test_no_warning_where_a_turn_would_not_be_refused(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    out = config_output(tmp_path, short_runtime, capsys, text)
    assert "will be refused" not in out
