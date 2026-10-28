"""Small, explicit process environments for external engines.

The daemon environment is input to these selectors, never the engine environment.
Each adapter adds only the variables its engine needs to the common operating-system
set. Engine credentials are handled separately by the adapter strategy.
"""

from collections.abc import Mapping, Sequence

BASE_ENVIRONMENT: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM", "TMPDIR")
"""The executable search path, the engine's isolated home, locale, terminal type,
and temporary directory. Missing optional entries stay missing."""


def allowlisted(environment: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    """Copy only named variables that exist in the supplied environment."""
    return {name: environment[name] for name in names if name in environment}
