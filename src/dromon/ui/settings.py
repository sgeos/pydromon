"""Settings in the client core: typed values from what the operator types.

The client knows no setting of its own. The daemon's ``settings`` result names every
key, its kind and the values a choice accepts, so this module only reads typed text
by kind, and a client written against this protocol learns of a new setting without
a new release. The daemon checks every value again, in meaning as well as in kind.
"""

import re
from dataclasses import dataclass
from typing import Final

from dromon.ui.commands import PROJECT_PREFIX, Set, SetSetting, Setting
from dromon.ui.data import SettingEntry, Settings

TRUE: Final = frozenset({"on", "true", "yes", "1"})
FALSE: Final = frozenset({"off", "false", "no", "0"})
DECIMAL: Final = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")
LIST_SEPARATORS: Final = re.compile(r"[,\s]+")


def rows(settings: Settings) -> list[tuple[bool, SettingEntry]]:
    """The settings panel's rows, the line's and then the project's, each marked
    with whether it belongs to the project."""
    return [(False, entry) for entry in settings.line] + [
        (True, entry) for entry in settings.project
    ]


def command_key(project: bool, entry: SettingEntry) -> str:
    """The key as ``/set`` takes it."""
    return (PROJECT_PREFIX if project else "") + entry.key


def find(settings: Settings, key: str) -> tuple[bool, SettingEntry] | None:
    project = key.startswith(PROJECT_PREFIX)
    bare = key[len(PROJECT_PREFIX) :] if project else key
    pool = settings.project if project else settings.line
    for entry in pool:
        if entry.key == bare:
            return project, entry
    return None


@dataclass(frozen=True, slots=True)
class Problem:
    """Why typed text is not a value, kept apart from text values themselves."""

    reason: str


def value_of(entry: SettingEntry, text: str) -> Setting | Problem:
    """``text`` as a value of ``entry``'s kind, or the problem with it."""
    stripped = text.strip()
    match entry.kind:
        case "bool":
            if stripped.lower() in TRUE:
                return True
            if stripped.lower() in FALSE:
                return False
            return Problem(f"{entry.key} takes on or off")
        case "int":
            if stripped.isascii() and stripped.isdigit() and len(stripped) <= 7:
                return int(stripped)
            return Problem(f"{entry.key} takes a whole number")
        case "decimal":
            if DECIMAL.fullmatch(stripped) is not None:
                return stripped
            return Problem(f"{entry.key} takes a decimal number such as 2.5")
        case "list":
            if stripped.lower() == "none":
                return ()
            return tuple(item for item in LIST_SEPARATORS.split(stripped) if item)
        case "text" | "choice":
            if not stripped:
                return Problem(f"{entry.key} takes a value")
            if entry.choices and stripped not in entry.choices:
                return Problem(f"{entry.key} takes one of {', '.join(entry.choices)}")
            return stripped
        case _:
            return Problem(f"{entry.key} cannot be changed from a client")


def request_of(settings: Settings | None, command: Set) -> SetSetting | str:
    """The request a ``/set``, ``/unset``, ``/model`` or ``/effort`` makes, or the
    reason it makes none."""
    if settings is None:
        return "the settings have not arrived yet. Try again in a moment"
    found = find(settings, command.key)
    if found is None:
        return f"no setting {command.key}. /config lists them"
    project, entry = found
    if not entry.editable:
        return f"{command.key} cannot be changed from a client. {entry.summary}"
    if command.text is None:
        return SetSetting(project, entry.key, None)
    value = value_of(entry, command.text)
    if isinstance(value, Problem):
        return value.reason
    return SetSetting(project, entry.key, value)


def values_for(entry: SettingEntry) -> tuple[str, ...]:
    """What a value of ``entry`` may complete to."""
    if entry.kind == "bool":
        return ("on", "off")
    if entry.kind == "list":
        return ("none",)
    return entry.choices


def prefill(project: bool, entry: SettingEntry) -> str:
    """The input that edits ``entry``, holding its current value."""
    value = "" if entry.shown == "none" and entry.kind == "list" else entry.shown
    if entry.kind == "list":
        value = value.replace(", ", ",")
    return f"/set {command_key(project, entry)} {value}".rstrip() + " "
