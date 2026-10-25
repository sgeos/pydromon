"""Instructions: a line's system prompt and the files appended to it. The Codex side
is tested in ``test_codex.py``, beside its scripted app-server."""

import json
from pathlib import Path

import pytest

from dromon import cli
from dromon.driver import Driver, Strategy
from dromon.engine import (
    MAX_INSTRUCTION_BYTES,
    ClaudeCodeLine,
    EngineRefused,
    read_instructions,
)
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.projectconfig import parse


def haiku() -> Driver:
    return Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)


def project(tmp_path: Path) -> Path:
    (tmp_path / "CLAUDE.md").write_text("Follow the project rules.\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "STYLE.md").write_text("No contractions.\n")
    return tmp_path


def test_the_configuration_carries_its_prompt_and_files_through_the_log() -> None:
    config = config_from_document(
        {"claude_code_prompt": True, "instructions": ["CLAUDE.md", "~/notes.md"]}
    )
    assert config.claude_code_prompt
    assert config.instructions == ("CLAUDE.md", "~/notes.md")
    assert decode_config(encode_config(config)) == config
    stored = json.loads(encode_config(DEFAULT_CONFIG))
    assert "claude_code_prompt" not in stored and "instructions" not in stored


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"claude_code_prompt": "yes"}, "claude_code_prompt must be a boolean"),
        ({"instructions": "CLAUDE.md"}, "instructions is not an array"),
        ({"instructions": [""]}, "instructions must be text"),
        ({"instructions": ["a", "a"]}, "names a file twice"),
        ({"instructions": [f"f{i}" for i in range(17)]}, "more than 16 files"),
    ],
)
def test_a_bad_instruction_setting_is_refused(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_document(document)


def test_files_are_read_in_order_under_headings(tmp_path: Path) -> None:
    root = project(tmp_path)
    text = read_instructions(["CLAUDE.md", "docs/STYLE.md"], root)
    assert text == (
        "Contents of CLAUDE.md:\n\nFollow the project rules.\n\n"
        "Contents of docs/STYLE.md:\n\nNo contractions."
    )
    assert read_instructions([str(root / "CLAUDE.md")], Path("/elsewhere")).endswith(
        "Follow the project rules."
    )
    assert read_instructions([], root) == ""


def test_a_missing_oversized_or_binary_file_refuses_the_turn(tmp_path: Path) -> None:
    root = project(tmp_path)
    with pytest.raises(EngineRefused, match="cannot read the instructions MISSING.md"):
        read_instructions(["MISSING.md"], root)
    (root / "big.md").write_bytes(b"x" * (MAX_INSTRUCTION_BYTES + 1))
    with pytest.raises(EngineRefused, match="exceed"):
        read_instructions(["big.md"], root)
    (root / "bin.md").write_bytes(b"\xff\xfe")
    with pytest.raises(EngineRefused, match="not UTF-8"):
        read_instructions(["bin.md"], root)


def test_a_claude_line_gets_an_empty_prompt_unless_configured(tmp_path: Path) -> None:
    root = project(tmp_path)
    assert ClaudeCodeLine(haiku(), root).options().system_prompt is None
    custom = LineConfig(instructions=("CLAUDE.md",))
    assert ClaudeCodeLine(haiku(), root, config=custom).options().system_prompt == (
        "Contents of CLAUDE.md:\n\nFollow the project rules."
    )
    preset = LineConfig(claude_code_prompt=True)
    assert ClaudeCodeLine(haiku(), root, config=preset).options().system_prompt == {
        "type": "preset",
        "preset": "claude_code",
    }
    both = LineConfig(claude_code_prompt=True, instructions=("CLAUDE.md",))
    assert ClaudeCodeLine(haiku(), root, config=both).options().system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "Contents of CLAUDE.md:\n\nFollow the project rules.",
    }


def test_the_project_file_and_the_client_set_them() -> None:
    config = parse(
        "version = 1\n[profiles.p]\nmodel = 'claude-haiku-5-5'\n"
        "claude_code_prompt = true\ninstructions = ['CLAUDE.md']\n"
        "[lines.a]\nprofile = 'p'\n[lines.b]\nprofile = 'p'\ninstructions = []\n"
    )
    assert config.lines["a"].config.instructions == ("CLAUDE.md",)
    assert config.lines["a"].config.claude_code_prompt
    assert config.lines["b"].config.instructions == ()
    arguments = cli.build_parser().parse_args(
        [
            "configure",
            "a",
            "--instructions",
            "CLAUDE.md",
            "--instructions",
            "AGENTS.md",
            "--claude-code-prompt",
        ]
    )
    document = cli._config_document(arguments)  # pyright: ignore[reportPrivateUsage]
    assert document is not None
    assert document["instructions"] == ["CLAUDE.md", "AGENTS.md"]
    assert document["claude_code_prompt"] is True
