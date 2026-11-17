"""Dromon, a multi-agent harness daemon. This package is the Python prototype.

The seed provides version information and the two command entry points only. See
``docs/architecture/DESIGN.md`` for the planned system.
"""

import os
from importlib.metadata import version

DISTRIBUTION = "pydromon"
"""The distribution name. The import package is ``dromon``."""

__version__: str = version(DISTRIBUTION)

BRIDGE_LOG_VARIABLE = "DROMON_BRIDGE_LOG"
"""The file a tool server writes its standard error and its fatal errors to, when a
caller names one. **Round 17, item 1.** An engine that starts the server gives it no
standard error to read, so a server that dies before it answers is seen only as a
closed connection. The caller names a file where the engine's own sandbox allows a
write, and the server marks it when it starts and again when it ends, so a file with a
start and no end may indicate a killed server or a diagnostic that could not drain.
Pending diagnostics may be lost on abrupt termination. A traceback names the failing
call after credential filtering."""


def _capture_bridge_log(path: str | None, *, descriptor: int = 2) -> bool:
    """Capture scrubbed diagnostics in a verified private regular file. A log that
    cannot be opened is refused without preventing the server from serving."""
    if not path:
        return False
    from dromon.bridgelog import capture  # noqa: PLC0415

    return capture(path, descriptor=descriptor)


_capture_bridge_log(os.environ.get(BRIDGE_LOG_VARIABLE))

__all__ = ["BRIDGE_LOG_VARIABLE", "DISTRIBUTION", "__version__"]
