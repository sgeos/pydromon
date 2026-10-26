"""The JSON-lines process the Pi, Cursor Agent and Antigravity adapters start.

The harness here is a small Python script, never a real harness. **Cursor review, F5.**
A stopped or finished harness must take every process it started with it.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from dromon import jsonlines
from dromon.jsonlines import (
    CLOUD_PREFIXES,
    CREDENTIAL_SUFFIXES,
    is_cloud_or_credential,
    json_lines_factory,
)

CHILD_SCRIPT = (
    "import json, subprocess, sys, time\n"
    "child = subprocess.Popen(['/bin/sleep', '60'])\n"
    "sys.stdout.write(json.dumps({'child': child.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
)
"""A harness that starts a child in its own process group and reports its pid."""


def write_harness(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "harness"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def gone(pid: int) -> bool:
    """Whether ``pid`` ends within five seconds, as an orphan is reaped by init."""
    for _ in range(100):
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def child_of(body: str, tmp_path: Path, *, stop: bool) -> int:
    write_harness(tmp_path / "bin", body)
    factory = json_lines_factory("harness", keep_input=False)
    async with factory([], tmp_path, {"PATH": str(tmp_path / "bin")}) as harness:
        record = await harness.receive()
        assert record is not None
        child = int(record["child"])
        assert alive(child)
        if stop:
            await harness.stop()
    return child


def test_stopping_a_harness_ends_the_children_it_started(tmp_path: Path) -> None:
    async def scenario() -> bool:
        child = await child_of(CHILD_SCRIPT + "time.sleep(60)\n", tmp_path, stop=True)
        return await gone(child)

    assert asyncio.run(scenario())


def test_a_child_left_behind_by_an_exited_harness_is_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child holds the harness's output open, so the harness counts as running
    until the wait for its exit passes and its group is signalled."""
    monkeypatch.setattr(jsonlines, "EXIT_SECONDS", 0.5)

    async def scenario() -> bool:
        child = await child_of(CHILD_SCRIPT, tmp_path, stop=False)
        return await gone(child)

    assert asyncio.run(scenario())


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_CONFIG",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "SOME_SERVICE_PASSWORD",
        "SOME_ACCESS_KEY",
    ],
)
def test_cloud_and_credential_variables_are_named(name: str) -> None:
    assert is_cloud_or_credential(name)


@pytest.mark.parametrize("name", ["PATH", "HOME", "LANG", "TMPDIR", "AWSX", "TOKENS"])
def test_ordinary_variables_are_not(name: str) -> None:
    assert not is_cloud_or_credential(name)
    assert not name.startswith(CLOUD_PREFIXES)
    assert not name.endswith(CREDENTIAL_SUFFIXES)
