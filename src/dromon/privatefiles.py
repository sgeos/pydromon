"""Descriptor based file access that refuses links in every directory component.

Paths are untrusted. Directory descriptors keep a rename from redirecting a later
open. The kernel still controls the duration of filesystem operations, so callers
that need a wall clock bound must run these operations on a bounded worker.
"""

import contextlib
import os
import stat
from pathlib import Path


def open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory without following links. Missing components may
    be created privately. The returned descriptor belongs to the caller."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_regular(path: Path) -> int:
    """Open one singly linked regular file for reading without following links."""
    directory = open_directory(path.parent)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("not a singly linked regular file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_log(path: Path) -> int:
    """Validate before truncation and correct permissions on the open descriptors.
    Refuse an unowned directory or file, aliases and special files."""
    directory = open_directory(path.parent, create=True)
    try:
        if os.fstat(directory).st_uid != os.getuid():
            raise OSError("log directory is not owned by this user")
        os.fchmod(directory, 0o700)
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise OSError("log is not a private regular file")
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
