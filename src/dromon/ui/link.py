"""The ``Daemon`` port over the project's operator socket, which the entry point wires.

Trust. Whoever can connect to the socket has operator authority, so this adapter adds
nothing to it and checks nothing. It only turns every way a request can fail into
``Unreachable``, which the runtime shows as a notice. ``FleetProjects`` reaches another
project's daemon the same way, through that daemon's own socket, once the fleet index
names it live and the daemon answering says it is the one named.
"""

from pathlib import Path
from typing import Final

from dromon import fleet, registry
from dromon.client import DaemonNotRunning, call
from dromon.paths import STATE_DIR_NAME, ProjectPaths
from dromon.protocol import Operation, ProtocolError, Response
from dromon.ui.model import ProjectRow
from dromon.ui.runtime import Daemon, Unreachable

TIMEOUT_SECONDS: Final = 2.0
"""How long the client waits for an answer, which bounds how long it can stall."""


class SocketDaemon:
    def __init__(self, paths: ProjectPaths) -> None:
        self._socket = paths.socket

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        try:
            return call(self._socket, operation, TIMEOUT_SECONDS, args)
        except DaemonNotRunning as error:
            if error.missing:
                raise Unreachable(
                    "the daemon is not running and its socket is gone",
                    gone=True,
                    missing=True,
                ) from error
            raise Unreachable("the daemon refused the connection", gone=True) from error
        except (ProtocolError, TimeoutError, OSError) as error:
            raise Unreachable(f"the daemon did not answer: {error}") from error


class FleetProjects:
    """The ``Projects`` port over the fleet index. Round 13, item 4. A project is
    opened only when its entry is live and the daemon answering its socket gives the
    pid and root the entry names, as every fleet command requires."""

    def __init__(self, directory: Path, start_of: registry.StartOf) -> None:
        self._directory = directory
        self._start_of = start_of

    def _survey(self) -> list[fleet.Indexed]:
        try:
            return fleet.survey(self._directory, self._start_of)
        except fleet.FleetError as error:
            raise Unreachable(str(error)) from error

    def names(self) -> tuple[ProjectRow, ...]:
        return tuple(
            ProjectRow(
                indexed.entry.name, indexed.liveness.value, str(indexed.entry.root)
            )
            for indexed in self._survey()
        )

    def open(self, project: str) -> Daemon:
        try:
            entry = fleet.resolve(self._survey(), project)
        except fleet.FleetError as error:
            raise Unreachable(str(error)) from error
        daemon = SocketDaemon(
            ProjectPaths(entry.root, entry.root / STATE_DIR_NAME, entry.socket.parent)
        )
        response = daemon.call(Operation.STATUS, {})
        try:
            fleet.verify(entry, response.result or {})
        except fleet.FleetError as error:
            raise Unreachable(str(error)) from error
        return daemon
