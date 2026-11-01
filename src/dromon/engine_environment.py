"""Small, explicit process environments for external engines.

The daemon environment is input to these selectors, never the engine environment.
Each adapter adds only the variables its engine needs to the common operating-system
set. Engine credentials are handled separately by the adapter strategy.
"""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

BASE_ENVIRONMENT: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM", "TMPDIR")
"""The executable search path, the engine's isolated home, locale, terminal type,
and temporary directory. Missing optional entries stay missing."""

TOOL_SEARCH_VARIABLE = "ENABLE_TOOL_SEARCH"
"""The Agent SDK's engine variable that keeps Claude Code's tool search enabled."""
TOOL_SEARCH_PINNED = "true"
"""Stable tool search keeps the tool schemas from changing between requests."""

SHELL_VARIABLE = "SHELL"
"""The absolute shell executable selected by the operator's daemon environment."""

CLAUDE_SHELL_VARIABLE = "CLAUDE_CODE_SHELL"
"""Claude Code's explicit shell override for Bash tool commands."""

CLAUDE_FALLBACK_SHELLS = ("/bin/bash", "/bin/zsh", "/usr/bin/bash", "/usr/bin/zsh")
"""Bounded system paths, independent of an inherited executable search path."""


class ShellSelectionError(ValueError):
    """The daemon has no usable absolute shell executable for its engine lines."""


def selected_shell(environment: Mapping[str, str]) -> str:
    """Return the daemon's executable ``SHELL`` path or refuse an unsafe selection."""
    value = environment.get(SHELL_VARIABLE)
    if not value:
        raise ShellSelectionError("SHELL is not set in the daemon environment")
    path = Path(value)
    if not path.is_absolute():
        raise ShellSelectionError("SHELL must be an absolute path")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ShellSelectionError("SHELL must name an executable file")
    return value


def selected_claude_shell(environment: Mapping[str, str]) -> str:
    """Prefer a supported daemon shell, then an existing system Bash or Zsh.

    Fixed paths avoid executing an unrelated program found through PATH. The same
    selector supplies the engine environment and the line's reported settings.
    """
    try:
        value = selected_shell(environment)
    except ShellSelectionError:
        value = ""
    if value and Path(value).name in {"bash", "zsh"}:
        return value
    for candidate in CLAUDE_FALLBACK_SHELLS:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return candidate
    raise ShellSelectionError(
        "no executable bash or zsh is available at a known system path"
    )


CLAUDE_ENGINE_ENVIRONMENT: tuple[str, ...] = (
    "USER",
    "LOGNAME",
    TOOL_SEARCH_VARIABLE,
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "PWD",
    "TRACEPARENT",
    "TRACESTATE",
    "BAGGAGE",
)
"""Claude Code inputs set by Dromon or the pinned Agent SDK before it starts the
engine, plus the account names Claude Code uses to find its macOS Keychain sign-in.
The adapter does not forward other daemon variables."""


def allowlisted(environment: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    """Copy only named variables that exist in the supplied environment."""
    return {name: environment[name] for name in names if name in environment}
