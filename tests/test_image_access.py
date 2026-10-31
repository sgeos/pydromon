"""The ``ViewImage`` capability, round 11: its bounds, and that nothing else grants it.

The per-engine behaviour is tested beside each adapter, in ``test_engine.py``,
``test_codex.py`` and ``test_grok.py``. This file holds the bounds themselves and the
property that spans engines, that a line without the capability cannot view an image.
"""

import asyncio
import os
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from dromon import antigravity, cursor, grok, pi
from dromon.codex import IMAGE_FEATURE, server_arguments
from dromon.codex import unsupported as codex_unsupported
from dromon.engine import permission_gate
from dromon.imageaccess import (
    MAX_IMAGE_BYTES,
    PROTECTED_NAMES,
    VIEW_IMAGE,
    claude_tools,
    image_refusal,
    view_only,
)
from dromon.lineconfig import LineConfig, ToolPolicy

CONTEXT = ToolPermissionContext(signal=None, suggestions=[])
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_a_small_png_in_the_working_directory_is_accepted(tmp_path: Path) -> None:
    write(tmp_path / "shots" / "a.png", PNG)
    assert image_refusal("shots/a.png", tmp_path) is None
    write(tmp_path / "b.PNG", PNG)
    assert image_refusal("b.PNG", tmp_path) is None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 8),
        ("a.jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 8),
        ("a.gif", b"GIF89a" + b"\x00" * 8),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
    ],
)
def test_each_image_type_needs_its_own_opening_bytes(
    tmp_path: Path, name: str, content: bytes
) -> None:
    write(tmp_path / name, content)
    assert image_refusal(name, tmp_path) is None
    write(tmp_path / name, b"just some text, not an image at all")
    assert "does not begin" in (image_refusal(name, tmp_path) or "")


def test_a_riff_file_that_is_not_webp_is_refused(tmp_path: Path) -> None:
    write(tmp_path / "a.webp", b"RIFF\x00\x00\x00\x00WAVEfmt ")
    assert "webp" in (image_refusal("a.webp", tmp_path) or "")


@pytest.mark.parametrize("name", ["a.svg", "a.pdf", "a.txt", "a", "a.png.txt"])
def test_other_suffixes_are_refused(tmp_path: Path, name: str) -> None:
    write(tmp_path / name, PNG)
    assert "by its name" in (image_refusal(name, tmp_path) or "")


def test_the_size_limit_is_enforced_at_the_boundary(tmp_path: Path) -> None:
    exact = write(tmp_path / "exact.png", PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG)))
    assert exact.stat().st_size == MAX_IMAGE_BYTES
    assert image_refusal("exact.png", tmp_path) is None
    over = write(
        tmp_path / "over.png", PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG) + 1)
    )
    reason = image_refusal("over.png", tmp_path) or ""
    assert "over the limit" in reason
    assert str(MAX_IMAGE_BYTES) in reason
    assert over.stat().st_size == MAX_IMAGE_BYTES + 1
    assert image_refusal("exact.png", tmp_path, limit=10) is not None


def test_a_path_must_stay_inside_the_working_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    write(tmp_path / "outside.png", PNG)
    write(work / "inside.png", PNG)
    assert "outside" in (image_refusal("../outside.png", work) or "")
    assert "outside" in (image_refusal(str(tmp_path / "outside.png"), work) or "")
    (work / "link.png").symlink_to(tmp_path / "outside.png")
    assert "outside" in (image_refusal("link.png", work) or "")
    (work / "dirlink").symlink_to(tmp_path)
    assert "outside" in (image_refusal("dirlink/outside.png", work) or "")
    assert image_refusal("inside.png", work) is None


@pytest.mark.parametrize("directory", sorted(PROTECTED_NAMES))
def test_state_history_and_secrets_are_never_read(
    tmp_path: Path, directory: str
) -> None:
    write(tmp_path / directory / "a.png", PNG)
    assert "never reads" in (image_refusal(f"{directory}/a.png", tmp_path) or "")


@pytest.mark.parametrize("raw", [None, 3, ["a.png"], "", "a\x00.png", "a" * 5000])
def test_a_malformed_path_is_refused(tmp_path: Path, raw: object) -> None:
    assert image_refusal(raw, tmp_path) is not None


def test_a_missing_file_a_directory_and_a_fifo_are_refused(tmp_path: Path) -> None:
    assert "does not exist" in (image_refusal("none.png", tmp_path) or "")
    (tmp_path / "dir.png").mkdir()
    assert image_refusal("dir.png", tmp_path) is not None
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    assert image_refusal("fifo.png", tmp_path) is not None


def test_view_image_maps_to_read_for_claude_code_and_is_view_only_without_read() -> (
    None
):
    assert claude_tools((VIEW_IMAGE,)) == ["Read"]
    assert claude_tools(("Bash", VIEW_IMAGE)) == ["Bash", "Read"]
    assert claude_tools(("Bash", "Read", VIEW_IMAGE)) == ["Bash", "Read"]
    assert claude_tools(()) == []
    assert view_only((VIEW_IMAGE,))
    assert not view_only(("Read", VIEW_IMAGE))
    assert not view_only(())


# No line without the capability can view an image, on any engine.

WITHOUT = (
    LineConfig(),
    LineConfig(builtin_tools=("Bash",)),
    LineConfig(builtin_tools=("Bash", "Edit", "Write")),
    LineConfig(tool_policy=ToolPolicy.ALLOW_ALL),
)


@pytest.mark.parametrize("config", WITHOUT)
def test_a_codex_line_without_it_has_the_image_feature_disabled(
    config: LineConfig,
) -> None:
    arguments = server_arguments([], config)
    assert arguments[arguments.index(IMAGE_FEATURE) - 1] == "--disable"
    assert "image_generation" in arguments


def test_a_codex_line_with_it_enables_view_image_and_never_generation() -> None:
    config = LineConfig(builtin_tools=(VIEW_IMAGE,))
    assert codex_unsupported(config) is None
    arguments = server_arguments([], config)
    assert arguments[arguments.index(IMAGE_FEATURE) - 1] == "--enable"
    assert arguments[arguments.index("image_generation") - 1] == "--disable"


@pytest.mark.parametrize("config", WITHOUT)
def test_a_claude_code_line_without_it_is_offered_no_read(config: LineConfig) -> None:
    assert "Read" not in claude_tools(config.builtin_tools)
    gate = permission_gate(config, frozenset())
    denied = asyncio.run(gate("Read", {"file_path": "/work/a.png"}, CONTEXT))
    if config.tool_policy is ToolPolicy.ALLOWLIST:
        assert isinstance(denied, PermissionResultDeny)


def test_pi_cursor_and_antigravity_refuse_the_capability() -> None:
    config = LineConfig(builtin_tools=(VIEW_IMAGE,))
    for refusal in (
        pi.unsupported(config),
        cursor.unsupported(config),
        antigravity.unsupported(config),
    ):
        assert refusal is not None


def test_a_claude_code_line_with_it_may_read_an_image_only(tmp_path: Path) -> None:
    write(tmp_path / "a.png", PNG)
    write(tmp_path / "notes.txt", b"text")
    config = LineConfig(builtin_tools=(VIEW_IMAGE,), tool_policy=ToolPolicy.ALLOW_ALL)
    gate = permission_gate(config, frozenset(), workdir=tmp_path)
    allowed = asyncio.run(gate("Read", {"file_path": "a.png"}, CONTEXT))
    refused = asyncio.run(gate("Read", {"file_path": "notes.txt"}, CONTEXT))
    missing = asyncio.run(gate("Read", {}, CONTEXT))
    assert isinstance(allowed, PermissionResultAllow)
    assert isinstance(refused, PermissionResultDeny)
    assert "ViewImage" in refused.message
    assert isinstance(missing, PermissionResultDeny)


def test_a_claude_code_line_with_read_is_not_narrowed_by_it(tmp_path: Path) -> None:
    write(tmp_path / "notes.txt", b"text")
    config = LineConfig(builtin_tools=("Read", VIEW_IMAGE))
    gate = permission_gate(config, frozenset(), workdir=tmp_path)
    allowed = asyncio.run(gate("Read", {"file_path": "notes.txt"}, CONTEXT))
    assert isinstance(allowed, PermissionResultAllow)


def test_grok_supports_the_capability_and_pairs_it_with_read_file_only() -> None:
    assert grok.unsupported(LineConfig(builtin_tools=(VIEW_IMAGE,))) is None
    granting = {name for name, tools in grok.GROK_TOOLS.items() if VIEW_IMAGE in tools}
    assert granting == {"read_file"}
    assert "read" in grok.GROK_TOOL_KINDS["read_file"]
    for generating in (
        "image_gen",
        "image_edit",
        "image_to_video",
        "reference_to_video",
    ):
        assert generating not in grok.GROK_TOOLS
