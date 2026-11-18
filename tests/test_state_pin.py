"""``scripts/state_pin.py``, which writes the pin ``test_state_version`` asks for.

**Round 18, item 3.** The test and the command share one computation, so a pin the
command writes is the pin the test accepts. These tests check that, that the command
never replaces a pin, and each of its refusals."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import state_pin
from dromon import statesnapshot
from dromon.model import Event
from state_pin import Pin, current, decide, parse_pin, pin_path, pin_text

SCRIPT = Path(state_pin.__file__)


def merged(scenario: tuple[Event, ...]) -> Pin:
    """The pin of a set this tree does not hold, as a merge of two state changing
    branches gives, with the current digests."""
    now = current(scenario)
    changes = tuple(sorted((*now.changes, "feat--merged-elsewhere")))
    return Pin(statesnapshot.state_version_of(changes), now.codec, now.replay, changes)


def test_a_missing_pin_is_written_as_the_test_reads_it(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    outcome = decide(pin, tmp_path, write=True)
    assert outcome.code == 0
    path = pin_path(pin.version, tmp_path)
    assert parse_pin(path.read_text(encoding="utf-8")) == pin
    assert path.name == pin_path(pin.version).name
    assert str(path) in outcome.message


def test_check_names_the_missing_pin_and_writes_nothing(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    outcome = decide(pin, tmp_path, write=False)
    assert outcome.code == 1
    assert outcome.message.startswith("missing")
    assert pin_text(pin) in outcome.message
    assert list(tmp_path.iterdir()) == []


def test_a_present_pin_is_accepted(scenario: tuple[Event, ...], tmp_path: Path) -> None:
    pin = merged(scenario)
    pin_path(pin.version, tmp_path).write_text(pin_text(pin), encoding="utf-8")
    assert decide(pin, tmp_path, write=True).code == 0


def test_a_pin_with_other_digests_is_refused_and_left_alone(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    other = Pin(pin.version, pin.codec, "0" * 64, pin.changes)
    path = pin_path(pin.version, tmp_path)
    path.write_text(pin_text(other), encoding="utf-8")
    outcome = decide(pin, tmp_path, write=True)
    assert outcome.code == 1
    assert "src/dromon/statechanges/BRANCH.txt" in outcome.message
    assert path.read_text(encoding="utf-8") == pin_text(other)


def test_a_pin_of_another_form_is_refused_and_left_alone(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    path = pin_path(pin.version, tmp_path)
    path.write_text("colour blue\n", encoding="utf-8")
    outcome = decide(pin, tmp_path, write=True)
    assert outcome.code == 1
    assert "cannot be read" in outcome.message
    assert path.read_text(encoding="utf-8") == "colour blue\n"


def test_an_unusable_set_is_refused(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    unusable = Pin(
        statesnapshot.UNUSABLE_STATE_VERSION, pin.codec, pin.replay, pin.changes
    )
    outcome = decide(unusable, tmp_path, write=True)
    assert outcome.code == 1
    assert "unusable" in outcome.message
    assert list(tmp_path.iterdir()) == []


def test_a_directory_that_cannot_be_written_is_refused(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    pin = merged(scenario)
    outcome = decide(pin, tmp_path / "absent", write=True)
    assert outcome.code == 1
    assert "could not be created" in outcome.message


def test_a_scenario_is_loaded_only_from_a_file_that_holds_one(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cannot be imported"):
        state_pin.load_scenario(tmp_path / "absent.py")
    empty = tmp_path / "conftest.py"
    empty.write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no SCENARIO"):
        state_pin.load_scenario(empty)


def test_the_loaded_scenario_is_the_test_suite_s(scenario: tuple[Event, ...]) -> None:
    assert state_pin.load_scenario() == scenario


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, the test's own interpreter and script
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_command_writes_the_pin_the_test_computes(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    """The command, in a process of its own, writes the pin of this tree that the
    state version test computes and finds in ``tests/state_pins``."""
    done = run("--pins", str(tmp_path))
    assert done.returncode == 0, done.stderr
    now = current(scenario)
    written = pin_path(now.version, tmp_path).read_text(encoding="utf-8")
    assert parse_pin(written) == now
    assert written == pin_path(now.version).read_text(encoding="utf-8")


def test_the_command_checks_the_tree_s_own_pins(tmp_path: Path) -> None:
    shutil.copytree(state_pin.PIN_DIRECTORY, tmp_path / "pins")
    done = run("--check", "--pins", str(tmp_path / "pins"))
    assert done.returncode == 0, done.stderr
    assert "already pins" in done.stdout


def test_the_command_refuses_a_pin_directory_that_is_not_one(tmp_path: Path) -> None:
    done = run("--pins", str(tmp_path / "absent"))
    assert done.returncode == 2
    assert "is not a directory" in done.stderr
