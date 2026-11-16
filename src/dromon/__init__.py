"""Dromon, a multi-agent harness daemon. This package is the Python prototype.

The seed provides version information and the two command entry points only. See
``docs/architecture/DESIGN.md`` for the planned system.
"""

import atexit
import contextlib
import os
import sys
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
start and no end is a server that was killed, and a file with a traceback names the
failing call."""


def _capture_bridge_log(
    path: str | None, *, argv: list[str] | None = None, descriptor: int = 2
) -> bool:
    """Send ``descriptor`` to ``path`` and mark the start and the end. Returns whether
    it did. Done as this package is first imported, because a failure in the imports
    that follow is the case the log is for. The file is created by this call, never
    followed through a link, truncated, and written with mode 0600, so a path that a
    line can write cannot be turned on another file. Never raises, since a server that
    cannot log must still serve."""
    if not path:
        return False
    try:
        opened = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError:
        return False
    try:
        os.dup2(opened, descriptor)
    except OSError:
        return False
    finally:
        os.close(opened)
    import faulthandler  # noqa: PLC0415
    import time  # noqa: PLC0415

    def mark(what: str) -> None:
        with contextlib.suppress(OSError):
            os.write(
                descriptor,
                f"dromon bridge {what}, pid {os.getpid()}, at"
                f" {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n".encode(),
            )

    mark("started, argv " + " ".join(sys.argv if argv is None else argv))
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        faulthandler.enable(file=descriptor)
    atexit.register(mark, "ended normally")
    return True


_capture_bridge_log(os.environ.get(BRIDGE_LOG_VARIABLE))

__all__ = ["BRIDGE_LOG_VARIABLE", "DISTRIBUTION", "__version__"]
