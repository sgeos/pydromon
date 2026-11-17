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
from dromon.privatefiles import open_log

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
    assert lines[0].startswith("dromon bridge started, pid")
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


@pytest.mark.parametrize("alias", ["parent", "hardlink", "fifo"])
def test_an_aliased_or_nonregular_log_is_refused(tmp_path: Path, alias: str) -> None:
    directory = tmp_path / "logs"
    directory.mkdir(mode=0o700)
    target = tmp_path / "keep"
    target.write_text("keep", encoding="utf-8")
    log = directory / "bridge.log"
    if alias == "parent":
        directory.rmdir()
        directory.symlink_to(tmp_path, target_is_directory=True)
        log = directory / "keep"
    elif alias == "hardlink":
        log.hardlink_to(target)
    else:
        os.mkfifo(log)
    result = _python("import dromon; print('served')", log, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "served"
    assert target.read_text(encoding="utf-8") == "keep"


def test_existing_log_permissions_are_private_and_missing_directory_is_created(
    tmp_path: Path,
) -> None:
    log = tmp_path / "logs" / "bridge.log"
    assert _python("import dromon", log).returncode == 0
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    log.chmod(0o644)
    assert _python("import dromon", log).returncode == 0
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_bridge_diagnostics_scrub_credentials_and_omit_arguments(
    tmp_path: Path,
) -> None:
    log = tmp_path / "bridge.log"
    secret = "bridge-private-test-value"  # noqa: S105, synthetic credential
    result = _python(
        "import sys, os\n"
        f"sys.argv = ['bridge', '{secret}']\n"
        "import dromon\n"
        f"os.write(2, b'PASSWORD={secret}\\n')\n"
        f"print('Bearer {secret}', file=sys.stderr)\n"
        f"raise RuntimeError('API_KEY={secret}')",
        log,
    )
    assert result.returncode != 0
    text = log.read_text(encoding="utf-8")
    assert secret not in text and "argv" not in text
    assert "RuntimeError" in text and "bridge started" in text


def test_a_log_directory_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    with pytest.raises(OSError, match="not owned"):
        open_log(tmp_path / "bridge.log")


def test_credentials_split_between_native_writes_are_scrubbed(tmp_path: Path) -> None:
    log = tmp_path / "bridge.log"
    result = _python(
        "import dromon, os\n"
        "os.write(2, b'PASSWORD=split-')\n"
        "os.write(2, b'credential-value\\n')\n"
        "os.write(2, b'-----BEGIN PRIVATE KEY-----\\n')\n"
        "os.write(2, b'private-body\\n-----END PRIVATE KEY-----\\n')",
        log,
    )
    assert result.returncode == 0
    text = log.read_text(encoding="utf-8")
    assert "split-credential-value" not in text
    assert "private-body" not in text


def test_a_log_worker_that_cannot_start_preserves_stderr(tmp_path: Path) -> None:
    result = _python(
        "import dromon, sys, threading\n"
        "def fail(self):\n    raise RuntimeError('no thread')\n"
        "threading.Thread.start = fail\n"
        f"assert not dromon._capture_bridge_log({str(tmp_path / 'log')!r})\n"
        "print('still served', file=sys.stderr)",
        None,
    )
    assert result.returncode == 0
    assert result.stderr.strip() == "still served"


@pytest.fixture
def private_project() -> Generator[Path]:
    """A project whose ``.dromon``, ``.dromon/run`` and ``secret`` cannot be read, as a
    Grok line's profile makes them. A mode of 000 is the closest a test has to a
    sandbox rule, since a test cannot apply one inside the sandbox that runs it."""
    # A short path, since the socket path inside it has a limit of 103 bytes.
    root = Path(tempfile.mkdtemp(prefix="pj", dir="/tmp"))
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
