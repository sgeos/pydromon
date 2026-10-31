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

from imagesamples import gif, jpeg, png, png_carrying, png_with_raw_data, webp

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
PNG = png()


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_a_small_png_in_the_working_directory_is_accepted(tmp_path: Path) -> None:
    write(tmp_path / "shots" / "a.png", PNG)
    assert image_refusal("shots/a.png", tmp_path) is None
    write(tmp_path / "b.PNG", PNG)
    assert image_refusal("b.PNG", tmp_path) is None


SAMPLES = [
    ("a.png", png()),
    ("a.jpg", jpeg()),
    ("a.jpeg", jpeg()),
    ("a.gif", gif()),
    ("a.webp", webp()),
]


@pytest.mark.parametrize(("name", "content"), SAMPLES)
def test_each_image_type_is_accepted_as_a_complete_container(
    tmp_path: Path, name: str, content: bytes
) -> None:
    write(tmp_path / name, content)
    assert image_refusal(name, tmp_path) is None
    write(tmp_path / name, b"just some text, not an image at all")
    assert "does not begin" in (image_refusal(name, tmp_path) or "")


# Review of 1045994, I1. These fail on the commit that checked 16 bytes.


@pytest.mark.parametrize(("name", "content"), SAMPLES)
def test_content_after_the_container_is_refused(
    tmp_path: Path, name: str, content: bytes
) -> None:
    write(tmp_path / name, content + b"TOP SECRET TEXT")
    assert image_refusal(name, tmp_path) is not None


@pytest.mark.parametrize(("name", "content"), SAMPLES)
def test_a_truncated_container_is_refused(
    tmp_path: Path, name: str, content: bytes
) -> None:
    for cut in (len(content) - 1, len(content) // 2, 12):
        write(tmp_path / name, content[:cut])
        assert image_refusal(name, tmp_path) is not None, cut


@pytest.mark.parametrize(
    ("name", "opening"),
    [
        ("a.png", b"\x89PNG\r\n\x1a\n"),
        ("a.jpg", b"\xff\xd8\xff"),
        ("a.gif", b"GIF89a"),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBP"),
    ],
)
def test_the_opening_bytes_followed_by_text_are_refused(
    tmp_path: Path, name: str, opening: bytes
) -> None:
    write(tmp_path / name, opening + b"TOP SECRET TEXT, any file's content")
    assert image_refusal(name, tmp_path) is not None


def test_a_complete_container_may_still_carry_arbitrary_bytes(tmp_path: Path) -> None:
    # Review of d00da44, T7 and I1b. This pins a limit and does not endorse it. The
    # check proves a container is complete, not that its data is a picture, so a text
    # chunk and compressed data can hold up to the size limit of chosen bytes. A later
    # change that refuses these is deliberate and edits this test.
    secret = b"TOP SECRET TEXT"
    write(tmp_path / "text.png", png_carrying(secret))
    write(tmp_path / "raw.png", png_with_raw_data(secret))
    assert image_refusal("text.png", tmp_path) is None
    assert image_refusal("raw.png", tmp_path) is None
    comment = bytearray(jpeg(100))
    comment[comment.index(b"\xff\xfe") + 4 :][:20] = secret.ljust(20, b" ")
    write(tmp_path / "comment.jpg", bytes(comment))
    assert image_refusal("comment.jpg", tmp_path) is None


def test_a_png_with_a_wrong_checksum_or_content_after_iend_is_refused(
    tmp_path: Path,
) -> None:
    good = png()
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    write(tmp_path / "crc.png", bytes(bad))
    assert "checksum" in (image_refusal("crc.png", tmp_path) or "")
    write(tmp_path / "tail.png", good + b"\x00")
    assert "after the png IEND" in (image_refusal("tail.png", tmp_path) or "")


def test_a_riff_file_that_is_not_webp_or_has_the_wrong_length_is_refused(
    tmp_path: Path,
) -> None:
    write(tmp_path / "a.webp", b"RIFF\x00\x00\x00\x00WAVEfmt ")
    assert "webp" in (image_refusal("a.webp", tmp_path) or "")
    good = bytearray(webp())
    good[4] += 1
    write(tmp_path / "b.webp", bytes(good))
    assert "riff length" in (image_refusal("b.webp", tmp_path) or "")


def test_a_file_named_for_one_type_must_be_that_type(tmp_path: Path) -> None:
    write(tmp_path / "jpeg-as.png", jpeg())
    write(tmp_path / "png-as.jpg", png())
    assert image_refusal("jpeg-as.png", tmp_path) is not None
    assert image_refusal("png-as.jpg", tmp_path) is not None


def test_a_jpeg_scan_may_hold_stuffed_and_restart_bytes(tmp_path: Path) -> None:
    data = bytearray(jpeg())
    scan_data = b"\x12\xff\x00\x34\xff\xd0\x56"
    data[-4:-2] = b""
    data[-2:] = b""
    write(tmp_path / "a.jpg", bytes(data) + scan_data + b"\xff\xd9")
    assert image_refusal("a.jpg", tmp_path) is None


@pytest.mark.parametrize("name", ["a.svg", "a.pdf", "a.txt", "a", "a.png.txt"])
def test_other_suffixes_are_refused(tmp_path: Path, name: str) -> None:
    write(tmp_path / name, PNG)
    assert "by its name" in (image_refusal(name, tmp_path) or "")


def test_the_size_limit_is_enforced_at_the_boundary(tmp_path: Path) -> None:
    exact = write(tmp_path / "exact.png", png(MAX_IMAGE_BYTES))
    assert exact.stat().st_size == MAX_IMAGE_BYTES
    assert image_refusal("exact.png", tmp_path) is None
    over = write(tmp_path / "over.png", png(MAX_IMAGE_BYTES + 1))
    reason = image_refusal("over.png", tmp_path) or ""
    assert "over the limit" in reason
    assert str(MAX_IMAGE_BYTES) in reason
    assert over.stat().st_size == MAX_IMAGE_BYTES + 1
    assert image_refusal("exact.png", tmp_path, limit=10) is not None


def test_each_type_is_accepted_at_a_size_and_refused_past_it(tmp_path: Path) -> None:
    for name, build in (("a.jpg", jpeg), ("a.gif", gif), ("a.webp", webp)):
        write(tmp_path / name, build(100))
        assert image_refusal(name, tmp_path) is None, name
        assert "over the limit" in (image_refusal(name, tmp_path, limit=99) or "")


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


# Review of 1045994, T2 and T4. The swaps the module can see are the ones between its
# own steps, so each is injected between them. A swap between the module's check and
# the engine's read cannot be tested here and is a stated limit.


def test_a_file_swapped_for_a_fifo_between_the_stat_and_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    regular = write(tmp_path / "regular.png", png())
    real_stat = os.stat

    def stale(path: object, *args: object, **kwargs: object) -> os.stat_result:
        del path, args, kwargs
        return real_stat(regular)

    monkeypatch.setattr("dromon.imageaccess.os.stat", stale)
    reason = image_refusal("fifo.png", tmp_path) or ""
    assert "not a regular file" in reason


def test_a_link_swapped_in_after_the_path_was_resolved_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    write(tmp_path / "outside.png", png())
    link = work / "link.png"
    link.symlink_to(tmp_path / "outside.png")
    real_realpath = os.path.realpath

    def unresolved(path: os.PathLike[str] | str, **kwargs: bool) -> str:
        # The link itself is left as it was before the swap resolved it, as a path
        # swapped after resolution is. The working directory still resolves.
        text = os.fspath(path)
        if text.endswith("link.png"):
            return text
        return real_realpath(text, strict=kwargs.get("strict", False))

    monkeypatch.setattr("dromon.imageaccess.os.path.realpath", unresolved)
    reason = image_refusal("link.png", work) or ""
    assert "cannot be opened" in reason


def test_a_file_that_grew_past_the_limit_after_its_size_was_taken_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "a.png", png(200))
    real_fstat = os.fstat

    def small(descriptor: int) -> os.stat_result:
        found = real_fstat(descriptor)
        fields = list(tuple(found)[:10])
        fields[6] = 10
        return os.stat_result(tuple(fields))

    monkeypatch.setattr("dromon.imageaccess.os.fstat", small)
    assert "grew past the limit" in (image_refusal("a.png", tmp_path, limit=100) or "")


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
