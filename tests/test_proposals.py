"""Private proposal records and their operator-facing list and show commands."""

import json
import os
import stat
from pathlib import Path

import pytest

from dromon import proposals


def sample(proposal_id: str = "p-0123456789ab") -> proposals.Proposal:
    return proposals.Proposal(
        proposal_id=proposal_id,
        created_at="2026-10-27T12:00:00Z",
        line_id="worker",
        workdir="/project/worktrees/worker",
        message="Fix the parser edge case.",
        files=("src/parser.py", "tests/test_parser.py"),
        patch="diff --git a/src/parser.py b/src/parser.py\n",
    )


def test_proposals_round_trip_in_private_state(tmp_path: Path) -> None:
    found = sample()
    written = proposals.write(tmp_path, found)
    assert written == tmp_path / ".dromon" / "proposals" / "p-0123456789ab.json"
    assert proposals.read(tmp_path, found.proposal_id) == found
    assert proposals.list_all(tmp_path) == [found]
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("files", "message", "patch", "reason"),
    [
        (("../outside.py",), "Fix", "diff", "invalid proposal file path"),
        (("a.py", "a.py"), "Fix", "diff", "contains duplicates"),
        ((), "Fix", "diff", "must list"),
        (("a.py",), "  ", "diff", "message is empty"),
        (("a.py",), "Fix", " ", "patch is empty"),
    ],
)
def test_invalid_proposal_fields_are_refused(
    tmp_path: Path,
    files: tuple[str, ...],
    message: str,
    patch: str,
    reason: str,
) -> None:
    with pytest.raises(proposals.ProposalError, match=reason):
        proposals.write(
            tmp_path,
            proposals.Proposal(
                proposal_id="p-0123456789ab",
                created_at="2026-10-27T12:00:00Z",
                line_id="worker",
                workdir="/project/worktrees/worker",
                message=message,
                files=files,
                patch=patch,
            ),
        )


def test_proposal_reader_rejects_symlink_and_malformed_records(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".dromon" / "proposals"
    directory.mkdir(parents=True, mode=0o700)
    source = tmp_path / "outside"
    source.write_text("{}", encoding="utf-8")
    (directory / "p-0123456789ab.json").symlink_to(source)
    with pytest.raises(proposals.ProposalError, match="cannot read proposal"):
        proposals.read(tmp_path, "p-0123456789ab")
    (directory / "p-0123456789ab.json").unlink()
    (directory / "p-0123456789ab.json").write_text(
        json.dumps({"unexpected": True}), encoding="utf-8"
    )
    os.chmod(directory / "p-0123456789ab.json", 0o600)
    with pytest.raises(proposals.ProposalError, match="invalid shape"):
        proposals.read(tmp_path, "p-0123456789ab")


def test_proposal_reader_refuses_invalid_identifiers(tmp_path: Path) -> None:
    with pytest.raises(proposals.ProposalError, match="invalid proposal identifier"):
        proposals.read(tmp_path, "../proposal")
