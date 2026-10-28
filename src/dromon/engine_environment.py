"""Small, explicit process environments for external engines.

The daemon environment is input to these selectors, never the engine environment.
Each adapter adds only the variables its engine needs to the common operating-system
set. Engine credentials are handled separately by the adapter strategy.
"""

from collections.abc import Mapping, Sequence

BASE_ENVIRONMENT: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM", "TMPDIR")
"""The executable search path, the engine's isolated home, locale, terminal type,
and temporary directory. Missing optional entries stay missing."""

TOOL_SEARCH_VARIABLE = "ENABLE_TOOL_SEARCH"
"""The Agent SDK's engine variable that keeps Claude Code's tool search enabled."""
TOOL_SEARCH_PINNED = "true"
"""Stable tool search keeps the tool schemas from changing between requests."""

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
