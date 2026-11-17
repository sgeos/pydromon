"""Round 17, item 6. A child started for a module of ``dromon`` runs the parent's
own ``dromon``, while isolated mode still keeps the working directory and
``PYTHONPATH`` off its path.

The extracted tree is a copy of this checkout's ``src``, with one probe module the
installed package does not hold, so a child that imported the installed package
could not find it at all."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dromon import modulecommand
from dromon.modulecommand import module_command

SOURCE = Path(__file__).resolve().parents[1] / "src"

PROBE = (
    "import importlib.util, sys\n"
    "print(__file__)\n"
    "print(importlib.util.find_spec('planted') is not None)\n"
    "print(sys.argv[1:])\n"
)

PARENT = (
    "import subprocess, sys\n"
    "from dromon.modulecommand import module_command\n"
    "found = subprocess.run(module_command('dromon._probe', 'a b', '--c'),"
    " capture_output=True, text=True, check=False)\n"
    "print(found.stdout, end='')\n"
    "print(found.stderr, end='', file=sys.stderr)\n"
)


@pytest.fixture
def extracted(tmp_path: Path) -> Path:
    tree = tmp_path / "review" / "src"
    shutil.copytree(SOURCE / "dromon", tree / "dromon")
    (tree / "dromon" / "_probe.py").write_text(PROBE, encoding="utf-8")
    return tree


def test_a_child_runs_the_tree_under_test(extracted: Path, tmp_path: Path) -> None:
    """The parent imports the extracted tree from ``PYTHONPATH``, as a review of an
    extracted tree does, and its isolated child runs the same tree. A module planted
    in the working directory and on ``PYTHONPATH`` stays out of the child's reach."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "planted.py").write_text("", encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": f"{extracted}{os.pathsep}{planted}"}
    found = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PARENT],
        cwd=planted,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert found.returncode == 0, found.stderr
    where, reached, arguments = found.stdout.splitlines()
    assert Path(where).resolve() == (extracted / "dromon" / "_probe.py").resolve()
    assert reached == "False"
    assert arguments == "['a b', '--c']"


def test_the_plain_isolated_form_reaches_the_installed_package(
    extracted: Path,
) -> None:
    """The defect this replaces. ``-I -m`` ignores ``PYTHONPATH``, so the child
    looks in the installed package, which holds no probe."""
    found = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-m", "dromon._probe"],
        env={**os.environ, "PYTHONPATH": str(extracted)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert found.returncode != 0
    assert "No module named dromon._probe" in found.stderr


@pytest.mark.parametrize("module", ["os", "dromonx.launcher", "evil", ""])
def test_only_a_module_of_dromon_is_started(module: str) -> None:
    with pytest.raises(ValueError, match="is not a module of dromon"):
        module_command(module)


def test_the_command_is_isolated_and_names_the_module() -> None:
    command = module_command("dromon.runs", "x")
    assert command[:3] == [sys.executable, "-I", "-c"]
    assert command[4:] == [modulecommand.package_root(), "dromon.runs", "x"]
    assert (
        Path(command[4], "dromon", "modulecommand.py").resolve()
        == Path(modulecommand.__file__).resolve()
    )
