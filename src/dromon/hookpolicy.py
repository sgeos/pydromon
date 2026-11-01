"""Whether Claude Code's hooks can be switched off where Dromon can see it.

**Round 14 review, item 1.** A view-only line, one with ``ViewImage`` and without
``Read``, is held to images by a ``PreToolUse`` hook, because Claude Code does not call
the permission callback for a ``Read`` inside its working directory. Claude Code has
two settings that stop hooks from running, ``disableAllHooks`` and
``allowManagedHooksOnly``. **Fact, found in the strings of the bundled Claude Code
2.1.310 by the audit line.** Their message reads "disableAllHooks or
allowManagedHooksOnly is set in settings or by policy". **Hypothesis.** Either one
stops an SDK hook callback from being called, so the confinement would fall back to the
permission callback, which is the path the operator found is not consulted.

Dromon's own settings document and project settings cannot carry them in practice, but
the operator's ``[settings]`` document is free text and a managed policy file is the
machine's. This module reads those where it can and names the setting it finds, so a
view-only line is refused rather than started without its confinement. **Limit.** A
policy that arrives some other way, such as a mobile device management profile or a
registry key, cannot be seen from here. The adapter's check that every ``Read`` had a
hook call is the second line of defence for that case.

**Porting note.** The directories are plain data. The reader is bounded by the number
of entries in them and by ``MAX_POLICY_FILES`` and ``MAX_POLICY_BYTES``.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

HOOK_SWITCHES: Final = ("disableAllHooks", "allowManagedHooksOnly")
"""The settings that stop Claude Code from running hooks."""

MANAGED_DIRECTORIES: Final = (
    Path("/Library/Application Support/ClaudeCode"),
    Path("/etc/claude-code"),
)
"""Where Claude Code reads its managed policy on macOS and on Linux. Each holds a
``managed-settings.json`` and a ``managed-settings.d`` directory of fragments."""

MANAGED_FILE: Final = "managed-settings.json"
MANAGED_FRAGMENTS: Final = "managed-settings.d"
MAX_POLICY_FILES: Final = 64
MAX_POLICY_BYTES: Final = 1024 * 1024


def _policy_files(directory: Path) -> list[Path]:
    """The policy files in ``directory``, the main file then the fragments in name
    order, at most ``MAX_POLICY_FILES``. A name that is absent is skipped."""
    files: list[Path] = []
    main = directory / MANAGED_FILE
    if main.exists():
        files.append(main)
    fragments = directory / MANAGED_FRAGMENTS
    try:
        names = sorted(
            entry for entry in fragments.iterdir() if entry.suffix == ".json"
        )
    except FileNotFoundError:
        names = []
    except OSError:
        # A fragments directory that cannot be listed is a policy Dromon cannot see.
        files.append(fragments)
        names = []
    files.extend(names)
    return files[:MAX_POLICY_FILES]


def _switch(document: Mapping[str, object]) -> str | None:
    for key in HOOK_SWITCHES:
        if document.get(key) is True:
            return key
    return None


def _file_refusal(file: Path) -> str | None:
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
    except OSError as error:
        return f"Dromon cannot read the managed policy {file}: {error}"
    if len(raw) > MAX_POLICY_BYTES:
        return f"the managed policy {file} is too large for Dromon to read"
    try:
        document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"Dromon cannot read the managed policy {file} as JSON"
    if not isinstance(document, dict):
        return f"the managed policy {file} is not a JSON object"
    found = _switch(cast(dict[str, object], document))
    if found is None:
        return None
    return f"the managed policy {file} sets {found}, which stops hooks from running"


def hook_policy_refusal(
    settings: Mapping[str, object] | None,
    directories: Sequence[Path] | None = None,
) -> str | None:
    """Why a view-only line must not start, or ``None``. The operator's settings
    document is read first, then each managed policy file in ``directories``. A file
    that exists and cannot be read as a JSON object also refuses, because a policy
    that cannot be seen cannot be shown to leave hooks on, which fails closed."""
    if settings is not None:
        found = _switch(settings)
        if found is not None:
            return (
                f"the line's settings document sets {found}, which stops hooks from"
                " running"
            )
    for directory in MANAGED_DIRECTORIES if directories is None else directories:
        for file in _policy_files(directory):
            why = _file_refusal(file)
            if why is not None:
                return why
    return None
