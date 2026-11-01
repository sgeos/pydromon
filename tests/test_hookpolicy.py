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


def test_a_switch_that_is_false_or_absent_is_no_refusal(tmp_path: Path) -> None:
    managed(
        tmp_path,
        "managed-settings.json",
        json.dumps({"disableAllHooks": False, "allowManagedHooksOnly": False}),
    )
    assert hook_policy_refusal(None, (tmp_path,)) is None
    assert hook_policy_refusal({"disableAllHooks": False}, (tmp_path,)) is None


@pytest.mark.parametrize("key", hookpolicy.HOOK_SWITCHES)
@pytest.mark.parametrize("value", [True, None, "true", "false", 0, 1, [], {}])
def test_a_switch_that_is_not_the_boolean_false_refuses(
    key: str, value: object, tmp_path: Path
) -> None:
    """Round 15, review of e9adf91. Only ``false`` is shown to leave hooks on, so a
    string, a number, ``null`` and a container refuse as ``true`` does, in a managed
    file, in a fragment and in the operator's settings document."""
    managed(tmp_path / "a", "managed-settings.json", json.dumps({key: value}))
    why = hook_policy_refusal(None, (tmp_path / "a",))
    assert why is not None and f"sets {key}" in why
    managed(tmp_path / "b" / "managed-settings.d", "10.json", json.dumps({key: value}))
    why = hook_policy_refusal(None, (tmp_path / "b",))
    assert why is not None and f"sets {key}" in why and "10.json" in why
    why = hook_policy_refusal({key: value}, (tmp_path / "absent",))
    assert why is not None and "settings document" in why


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


def test_more_files_than_the_limit_refuses_even_when_the_extra_is_harmless(
    tmp_path: Path,
) -> None:
    """Round 15, review of e9adf91. A 65th fragment that disables hooks passed,
    because only the first 64 were read. The directory now refuses when it holds more
    than it reads, whatever the file past the bound holds, and the main file counts."""
    fragments = tmp_path / "managed-settings.d"
    for number in range(hookpolicy.MAX_POLICY_FILES + 1):
        managed(fragments, f"{number:03}.json", json.dumps({}))
    why = hook_policy_refusal(None, (tmp_path,))
    assert why is not None and "more than" in why and str(tmp_path) in why
    # The one past the bound is the one that carries the switch.
    managed(fragments, "999.json", json.dumps({"disableAllHooks": True}))
    assert hook_policy_refusal(None, (tmp_path,)) is not None


def test_exactly_the_limit_is_read_in_full(tmp_path: Path) -> None:
    fragments = tmp_path / "managed-settings.d"
    for number in range(hookpolicy.MAX_POLICY_FILES):
        managed(fragments, f"{number:03}.json", json.dumps({}))
    assert hook_policy_refusal(None, (tmp_path,)) is None
    # With the main file the directory holds one more than the limit.
    managed(tmp_path, "managed-settings.json", json.dumps({}))
    assert hook_policy_refusal(None, (tmp_path,)) is not None
    # The last of the files at the limit is read, so a switch in it is found.
    (tmp_path / "managed-settings.json").unlink()
    managed(fragments, "063.json", json.dumps({"allowManagedHooksOnly": True}))
    why = hook_policy_refusal(None, (tmp_path,))
    assert why is not None and "063.json" in why


def test_the_reading_of_a_large_directory_is_bounded(tmp_path: Path) -> None:
    fragments = tmp_path / "managed-settings.d"
    for number in range(hookpolicy.MAX_POLICY_FILES * 3):
        managed(fragments, f"{number:04}.json", json.dumps({}))
    assert len(hookpolicy._policy_files(tmp_path)) == hookpolicy.MAX_POLICY_FILES + 1  # pyright: ignore[reportPrivateUsage]
