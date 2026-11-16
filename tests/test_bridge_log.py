"""The tool server's own log, and its start with the project's private directories
unreadable. **Round 17, items 1 and 2 of the Grok failure.** A Grok line's tool server
died in about a tenth of a second under the engine's sandbox, and the engine kept none
of its standard error."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from dromon import BRIDGE_LOG_VARIABLE, acp

INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
)


def _python(
    code: str, log: Path | str | None, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k != BRIDGE_LOG_VARIABLE}
    if log is not None:
        environment[BRIDGE_LOG_VARIABLE] = str(log)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=timeout,
    )


def test_a_server_that_ends_normally_leaves_a_start_and_an_end(tmp_path: Path) -> None:
    log = tmp_path / "bridge.log"
    result = _python("import dromon", log)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("dromon bridge started, argv")
    assert lines[-1].startswith("dromon bridge ended normally, pid")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert result.stderr == "", "standard error went to the log, not to the engine"


def test_a_failure_while_the_server_starts_is_named_in_the_log(tmp_path: Path) -> None:
    log = tmp_path / "bridge.log"
    result = _python("import dromon\nraise RuntimeError('the failing call')", log)
    assert result.returncode != 0
    text = log.read_text(encoding="utf-8")
    assert "RuntimeError: the failing call" in text
    assert "dromon bridge started" in text


def test_a_server_that_is_killed_leaves_a_start_and_no_end(tmp_path: Path) -> None:
    log = tmp_path / "bridge.log"
    result = _python(
        f"import dromon, os, signal\nos.kill(os.getpid(), {signal.SIGKILL})", log
    )
    assert result.returncode == -signal.SIGKILL
    text = log.read_text(encoding="utf-8")
    assert "dromon bridge started" in text and "ended" not in text


def test_without_a_log_variable_nothing_is_redirected(tmp_path: Path) -> None:
    result = _python("import dromon, sys\nprint('err', file=sys.stderr)", None)
    assert result.stderr.strip() == "err"
    assert list(tmp_path.iterdir()) == []


def test_a_log_path_that_is_a_link_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "other.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "bridge.log"
    link.symlink_to(target)
    result = _python("import dromon, sys\nprint('err', file=sys.stderr)", link)
    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "keep"
    assert result.stderr.strip() == "err", "the server ran, with its own standard error"


def test_a_log_path_that_cannot_be_made_does_not_stop_the_server(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-such-directory" / "bridge.log"
    result = _python("import dromon\nprint('served')", missing)
    assert result.returncode == 0 and result.stdout.strip() == "served"


def test_the_servers_environment_names_the_log_only_where_one_is_given(
    tmp_path: Path,
) -> None:
    plain = acp.mcp_servers(["b", "--line", "x"], "tok")[0]["env"]
    assert plain == [{"name": "DROMON_LINE_TOKEN", "value": "tok"}]
    logged = acp.mcp_servers(["b"], "tok", tmp_path / "x.log")[0]["env"]
    assert logged == [
        {"name": "DROMON_LINE_TOKEN", "value": "tok"},
        {"name": BRIDGE_LOG_VARIABLE, "value": str(tmp_path / "x.log")},
    ]


@pytest.fixture
def private_project() -> Generator[Path]:
    """A project whose ``.dromon``, ``.dromon/run`` and ``secret`` cannot be read, as a
    Grok line's profile makes them. A mode of 000 is the closest a test has to a
    sandbox rule, since a test cannot apply one inside the sandbox that runs it."""
    # A short path, since the socket path inside it has a limit of 103 bytes.
    root = Path(tempfile.mkdtemp(prefix="pj"))
    (root / ".dromon" / "run").mkdir(parents=True)
    (root / "secret").mkdir()
    (root / "secret" / "key").write_text("not for lines", encoding="utf-8")
    for name in ("secret", ".dromon/run", ".dromon"):
        (root / name).chmod(0)
    try:
        yield root
    finally:
        for name in (".dromon", ".dromon/run", "secret"):
            (root / name).chmod(0o700)
        shutil.rmtree(root, ignore_errors=True)


def test_the_bridge_answers_initialize_with_the_private_directories_unreadable(
    private_project: Path,
) -> None:
    """Review of the Grok failure of 2026-11-16, item 2. The handshake answers before
    any tool call reaches the daemon, so it must not need a path the profile denies."""
    root = private_project
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-m",
            "dromon.cli",
            "--project",
            str(root),
            "--runtime-dir",
            str(root / ".dromon" / "run" / "preflight-x"),
            "mcp",
            "--line",
            "grok-review",
        ],
        input=INITIALIZE + "\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "DROMON_LINE_TOKEN": "tok"},
    )
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.splitlines()[0])
    assert answer["id"] == 1 and answer["result"]["serverInfo"]["name"] == "dromon"
