"""Private, bounded proposal records made by session lines for operator review.

Proposals contain untrusted text. They are suggestions only and are never applied by
Dromon. The operator can list and inspect them with ``dromon proposal``.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dromon.paths import ensure_private_dir

MAX_PROPOSAL_BYTES: Final = 256 * 1024
MAX_PROPOSAL_MESSAGE_BYTES: Final = 8 * 1024
MAX_PROPOSAL_PATCH_BYTES: Final = 192 * 1024
MAX_PROPOSAL_FILES: Final = 64
PROPOSALS_DIRECTORY: Final = "proposals"
PROPOSAL_ID: Final = re.compile(r"p-[0-9a-f]{12}")


class ProposalError(ValueError):
    """A proposal is invalid, missing or cannot be read or written safely."""


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    created_at: str
    line_id: str
    workdir: str
    message: str
    files: tuple[str, ...]
    patch: str


def write(root: Path, proposal: Proposal) -> Path:
    """Persist one new proposal under the private project state directory."""
    _validate(proposal)
    data = json.dumps(
        {
            "proposal_id": proposal.proposal_id,
            "created_at": proposal.created_at,
            "line_id": proposal.line_id,
            "workdir": proposal.workdir,
            "message": proposal.message,
            "files": list(proposal.files),
            "patch": proposal.patch,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(data) > MAX_PROPOSAL_BYTES:
        raise ProposalError(f"proposal is over {MAX_PROPOSAL_BYTES} bytes")

    state = root / ".dromon"
    try:
        ensure_private_dir(state)
        directory = state / PROPOSALS_DIRECTORY
        ensure_private_dir(directory)
        directory_fd = _open_private_directory(directory)
        try:
            name = f"{proposal.proposal_id}.json"
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise ProposalError(f"cannot write proposal: {error.strerror}") from error
    return directory / f"{proposal.proposal_id}.json"


def list_all(root: Path) -> list[Proposal]:
    """Read proposals in identifier order, refusing malformed or unsafe entries."""
    directory = root / ".dromon" / PROPOSALS_DIRECTORY
    if not directory.exists():
        return []
    try:
        directory_fd = _open_private_directory(directory)
        try:
            names = sorted(
                name for name in os.listdir(directory_fd) if name.endswith(".json")
            )
            if len(names) > 1024:
                raise ProposalError("proposal directory has more than 1024 entries")
            return [_read_at(directory_fd, name) for name in names]
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise ProposalError(f"cannot list proposals: {error.strerror}") from error


def read(root: Path, proposal_id: str) -> Proposal:
    """Read one proposal by its validated identifier."""
    if PROPOSAL_ID.fullmatch(proposal_id) is None:
        raise ProposalError("invalid proposal identifier")
    directory_fd = _open_private_directory(root / ".dromon" / PROPOSALS_DIRECTORY)
    try:
        return _read_at(directory_fd, f"{proposal_id}.json")
    except OSError as error:
        raise ProposalError(f"cannot read proposal: {error.strerror}") from error
    finally:
        os.close(directory_fd)


def _open_private_directory(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise ProposalError(f"{path} is not a private directory owned by this user")
    return descriptor


def _read_at(directory_fd: int, name: str) -> Proposal:
    stem = name.removesuffix(".json")
    if PROPOSAL_ID.fullmatch(stem) is None or name != f"{stem}.json":
        raise ProposalError(f"invalid proposal filename {name!r}")
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ProposalError(f"proposal {stem} is not a private regular file")
        data = handle.read(MAX_PROPOSAL_BYTES + 1)
    if len(data) > MAX_PROPOSAL_BYTES:
        raise ProposalError(f"proposal {stem} is over {MAX_PROPOSAL_BYTES} bytes")
    try:
        decoded: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProposalError(f"proposal {stem} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ProposalError(f"proposal {stem} has an invalid shape")
    value = cast(dict[str, object], decoded)
    if set(value) != {
        "proposal_id",
        "created_at",
        "line_id",
        "workdir",
        "message",
        "files",
        "patch",
    }:
        raise ProposalError(f"proposal {stem} has an invalid shape")
    record = value
    raw_files = value["files"]
    if not isinstance(raw_files, list):
        raise ProposalError(f"proposal {stem} has an invalid file list")
    files = cast(list[object], raw_files)
    if not all(isinstance(item, str) for item in files):
        raise ProposalError(f"proposal {stem} has an invalid file list")
    proposal = Proposal(
        proposal_id=_record_text(record, "proposal_id", stem),
        created_at=_record_text(record, "created_at", stem),
        line_id=_record_text(record, "line_id", stem),
        workdir=_record_text(record, "workdir", stem),
        message=_record_text(record, "message", stem),
        files=tuple(cast(str, item) for item in files),
        patch=_record_text(record, "patch", stem),
    )
    _validate(proposal)
    if proposal.proposal_id != stem:
        raise ProposalError(f"proposal filename {stem} does not match its content")
    return proposal


def _record_text(record: dict[str, object], key: str, stem: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise ProposalError(f"proposal {stem} has invalid text fields")
    return value


def _validate(proposal: Proposal) -> None:
    if PROPOSAL_ID.fullmatch(proposal.proposal_id) is None:
        raise ProposalError("invalid proposal identifier")
    for label, value, limit in (
        ("created_at", proposal.created_at, 64),
        ("line_id", proposal.line_id, 64),
        ("workdir", proposal.workdir, 4096),
    ):
        if not value or len(value.encode("utf-8")) > limit:
            raise ProposalError(f"proposal {label} is empty or too long")
    if (
        not proposal.message.strip()
        or len(proposal.message.encode("utf-8")) > MAX_PROPOSAL_MESSAGE_BYTES
    ):
        raise ProposalError("proposal message is empty or too long")
    if (
        not proposal.patch.strip()
        or len(proposal.patch.encode("utf-8")) > MAX_PROPOSAL_PATCH_BYTES
    ):
        raise ProposalError("proposal patch is empty or too long")
    if not 1 <= len(proposal.files) <= MAX_PROPOSAL_FILES:
        raise ProposalError(f"proposal must list 1 to {MAX_PROPOSAL_FILES} files")
    if len(set(proposal.files)) != len(proposal.files):
        raise ProposalError("proposal file list contains duplicates")
    for path in proposal.files:
        candidate = Path(path)
        if (
            not path
            or "\x00" in path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or len(path.encode("utf-8")) > 4096
        ):
            raise ProposalError(f"invalid proposal file path {path!r}")
