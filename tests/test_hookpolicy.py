"""Whether a view-only line is refused where hooks are switched off. Round 14."""

import json
from pathlib import Path

import pytest

from dromon import hookpolicy
from dromon.hookpolicy import hook_policy_refusal


def managed(directory: Path, name: str, content: str | bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / name
    file.write_bytes(content if isinstance(content, bytes) else content.encode())
    return file


def test_nothing_to_see_is_no_refusal(tmp_path: Path) -> None:
    assert hook_policy_refusal(None, (tmp_path,)) is None
    assert hook_policy_refusal({"env": {}}, (tmp_path / "absent",)) is None
    assert hook_policy_refusal(None, ()) is None


@pytest.mark.parametrize("key", hookpolicy.HOOK_SWITCHES)
def test_the_main_file_and_each_fragment_are_read(key: str, tmp_path: Path) -> None:
    managed(tmp_path / "a", "managed-settings.json", json.dumps({key: True}))
    why = hook_policy_refusal(None, (tmp_path / "a",))
    assert why is not None and key in why and "managed-settings.json" in why
    managed(tmp_path / "b", "10-x.json", json.dumps({"model": "m"}))
    managed(tmp_path / "b", "20-y.json", json.dumps({key: True}))
    managed(tmp_path / "b" / "managed-settings.d", "10-x.json", "{}")
    managed(tmp_path / "b" / "managed-settings.d", "20-y.json", json.dumps({key: True}))
    why = hook_policy_refusal(None, (tmp_path / "b",))
    assert why is not None and "20-y.json" in why and key in why


@pytest.mark.parametrize("value", [False, None, "true", 1, []])
def test_a_switch_that_is_not_true_is_no_refusal(value: object, tmp_path: Path) -> None:
    managed(
        tmp_path,
        "managed-settings.json",
        json.dumps({"disableAllHooks": value, "allowManagedHooksOnly": value}),
    )
    assert hook_policy_refusal(None, (tmp_path,)) is None


@pytest.mark.parametrize(
    ("content", "phrase"),
    [
        (b"not json", "as JSON"),
        (b"\xff\xfe", "as JSON"),
        (b"[1]", "not a JSON object"),
        (b" " * (hookpolicy.MAX_POLICY_BYTES + 1), "too large"),
    ],
)
def test_a_policy_that_cannot_be_seen_refuses(
    content: bytes, phrase: str, tmp_path: Path
) -> None:
    managed(tmp_path, "managed-settings.json", content)
    why = hook_policy_refusal(None, (tmp_path,))
    assert why is not None and phrase in why


def test_an_unreadable_file_refuses(tmp_path: Path) -> None:
    file = managed(tmp_path, "managed-settings.json", "{}")
    file.chmod(0)
    try:
        if file.stat().st_mode & 0o777 == 0 and not _can_read(file):
            why = hook_policy_refusal(None, (tmp_path,))
            assert why is not None and "cannot read" in why
    finally:
        file.chmod(0o600)


def _can_read(file: Path) -> bool:
    try:
        file.read_bytes()
    except OSError:
        return False
    return True


def test_the_settings_document_is_read_first(tmp_path: Path) -> None:
    why = hook_policy_refusal({"allowManagedHooksOnly": True}, (tmp_path,))
    assert why is not None and "settings document" in why
    assert hook_policy_refusal({"allowManagedHooksOnly": False}, (tmp_path,)) is None


def test_fragments_are_bounded_by_the_file_limit(tmp_path: Path) -> None:
    for number in range(hookpolicy.MAX_POLICY_FILES + 5):
        managed(tmp_path / "managed-settings.d", f"{number:03}.json", json.dumps({}))
    # The one past the limit is not read, so it cannot refuse. This pins the bound.
    managed(
        tmp_path / "managed-settings.d",
        "999.json",
        json.dumps({"disableAllHooks": True}),
    )
    assert hook_policy_refusal(None, (tmp_path,)) is None
