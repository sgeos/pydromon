"""Where a project's daemon keeps its state and its socket, and the checks on them.

Each project has a state directory, ``.dromon`` under the project root, holding the
event log. The runtime directory holds the client socket, the single-instance lock and
the detached daemon's output. It defaults to ``.dromon/run`` and can be moved with
``DROMON_RUNTIME_DIR``, because a Unix domain socket path has a short length limit that
deep project paths can exceed.

Security boundary. Both directories must be owned by the current user and closed to
group and others, and they are created that way. The socket's access control rests on
the runtime directory's mode, which every Unix honours. A peer credential check on each
connection would add defence in depth, and is technical debt because its interface
differs between macOS and Linux.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

STATE_DIR_NAME: Final = ".dromon"
RUNTIME_DIR_ENV: Final = "DROMON_RUNTIME_DIR"

MAX_SOCKET_PATH_BYTES: Final = 103
"""The longest socket path accepted. macOS allows 104 bytes in ``sun_path`` including
the terminating NUL, and Linux allows 108, so the smaller bound serves both."""


class PathError(Exception):
    """A state or runtime path is unusable or unsafe."""


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path
    state_dir: Path
    runtime_dir: Path

    @property
    def event_log(self) -> Path:
        return self.state_dir / "events.sqlite"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "dromond.sock"

    @property
    def agent_socket(self) -> Path:
        """The socket external agents connect to, which exists only when the project
        lets agents in."""
        return self.runtime_dir / "agents.sock"

    @property
    def lock(self) -> Path:
        return self.runtime_dir / "dromond.lock"

    @property
    def daemon_output(self) -> Path:
        return self.runtime_dir / "dromond.log"


def project_paths(root: Path, runtime_dir: Path | None = None) -> ProjectPaths:
    """Return the paths for the project at ``root``, without touching the filesystem.

    ``runtime_dir`` overrides the environment, which overrides the default.
    """
    resolved = root.resolve()
    if not resolved.is_dir():
        raise PathError(f"the project root {resolved} is not a directory")
    state_dir = resolved / STATE_DIR_NAME
    if runtime_dir is None:
        configured = os.environ.get(RUNTIME_DIR_ENV)
        runtime_dir = Path(configured) if configured else state_dir / "run"
    paths = ProjectPaths(resolved, state_dir, runtime_dir.resolve())
    size = len(os.fsencode(paths.socket))
    if size > MAX_SOCKET_PATH_BYTES:
        raise PathError(
            f"the socket path {paths.socket} is {size} bytes, over the limit of"
            f" {MAX_SOCKET_PATH_BYTES}. Set {RUNTIME_DIR_ENV} to a shorter directory"
            " that only you can access."
        )
    return paths


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` with mode 0700 if absent, then verify it is private.

    Raises ``PathError`` if it is not a directory, is a symbolic link, is owned by
    another user, or is accessible to group or others. An existing directory is never
    loosened or tightened silently, because a mode the operator set may be deliberate.
    """
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise PathError(f"cannot create {path}: {error}") from error
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PathError(f"{path} is not a plain directory")
    if info.st_uid != os.getuid():
        raise PathError(f"{path} is owned by another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PathError(
            f"{path} has mode {stat.S_IMODE(info.st_mode):o}, which group or others can"
            " access. Restrict it to 700."
        )
