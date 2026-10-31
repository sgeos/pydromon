"""The ``Daemon`` port over the project's operator socket, which the entry point wires.

Trust. Whoever can connect to the socket has operator authority, so this adapter adds
nothing to it and checks nothing. It only turns every way a request can fail into
``Unreachable``, which the runtime shows as a notice.
"""

from typing import Final

from dromon.client import DaemonNotRunning, call
from dromon.paths import ProjectPaths
from dromon.protocol import Operation, ProtocolError, Response
from dromon.ui.runtime import Unreachable

TIMEOUT_SECONDS: Final = 2.0
"""How long the client waits for an answer, which bounds how long it can stall."""


class SocketDaemon:
    def __init__(self, paths: ProjectPaths) -> None:
        self._socket = paths.socket

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        try:
            return call(self._socket, operation, TIMEOUT_SECONDS, args)
        except DaemonNotRunning as error:
            raise Unreachable("the daemon is not running", gone=True) from error
        except (ProtocolError, TimeoutError, OSError) as error:
            raise Unreachable(f"the daemon did not answer: {error}") from error
