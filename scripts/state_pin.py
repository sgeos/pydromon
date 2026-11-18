#!/usr/bin/env python3
"""The pin of the state version, computed once for the test and for this command.

**Why. Round 18, item 3.** A merge of two branches that each changed the state holds a
set of state changes that neither branch pinned, so ``tests/test_state_version.py``
fails on it and names the pin file to add, with its content. That content was copied
from the failure by hand. This module holds the computation the test makes, so the
test and the command cannot disagree, and the command writes the pin the test asks
for.

**Usage**, from the repository, with the project's environment, since it imports
``dromon`` and the test scenario.

    uv run python scripts/state_pin.py            # write the missing pin
    uv run python scripts/state_pin.py --check    # say what is missing, write nothing

**What it refuses.** A pin is never edited, since editing one would accept a change of
the codec's types or of the replayed state without a new version. So the command
writes only a pin that does not exist, by exclusive creation, and a pin that exists
with other content is refused with the advice the test gives, which is to add
``src/dromon/statechanges/BRANCH.txt``. A set of state changes that is unusable is
refused, since its version names no set. Exit codes are 0 when the pin is present or
written, 1 when it is refused or, under ``--check``, missing, and 2 for a usage error.

**Residuals.** The scenario is ``SCENARIO`` from ``tests/conftest.py``, imported by
its path, so this command runs only in a checkout with its tests. A pin written here
is as trustworthy as the tree it was computed from, so a branch that changed the state
without meaning to is caught only by its review, as it was when the pin was copied by
hand.
"""

import argparse
import hashlib
import importlib.util
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dromon import statesnapshot
from dromon.model import DisclosureChecked, Event, State

ROOT = Path(__file__).resolve().parent.parent
PIN_DIRECTORY = ROOT / "tests" / "state_pins"
CONFTEST = ROOT / "tests" / "conftest.py"

DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Pin:
    version: str
    codec: str
    replay: str
    changes: tuple[str, ...]


def pin_path(version: str, directory: Path = PIN_DIRECTORY) -> Path:
    return directory / f"{version[:16]}.txt"


def pin_text(pin: Pin) -> str:
    lines = [f"version {pin.version}", f"codec {pin.codec}", f"replay {pin.replay}"]
    return "".join(
        f"{line}\n" for line in (*lines, *(f"change {c}" for c in pin.changes))
    )


def parse_pin(text: str) -> Pin:
    """A pin file, or ``ValueError`` for any other form."""
    fields: dict[str, str] = {}
    changes: list[str] = []
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "change":
            changes.append(value)
        elif key in ("version", "codec", "replay") and key not in fields:
            if DIGEST.fullmatch(value) is None:
                raise ValueError(f"{key} is not a SHA-256 digest")
            fields[key] = value
        else:
            raise ValueError(f"unexpected line {line!r}")
    if len(fields) != 3 or not changes:
        raise ValueError("a pin needs version, codec, replay and at least one change")
    pin = Pin(fields["version"], fields["codec"], fields["replay"], tuple(changes))
    if pin_text(pin) != text:
        raise ValueError("a pin is written in the form pin_text gives")
    return pin


RETAINED_FAILURES = 30
RETAINED_PASSES = 150
"""How many failing and passing disclosure checks the pinned history adds for one
line. They are fixed numbers, not derived from the caps, so a change of a cap changes
which checks the state keeps, and so the replay digest. A test fails if a cap is
raised to or past them."""


def disclosure_history() -> tuple[DisclosureChecked, ...]:
    """**Review of 0db077c, informational, decided by the operator as item 6.** How
    many disclosure checks the state keeps for a line is part of what applying an
    event means, since a snapshot kept under one cap and restored under another
    differs from a full replay. ``SCENARIO`` held two checks, which reach no cap, so a
    changed cap left the pin unmoved. These reach past ``MAX_DISCLOSURE_CHECKS``,
    ``MAX_DISCLOSURE_PASSES`` and ``MAX_DISCLOSURE_SIZES``, and every fourth pass
    has no byte count, as a tool's refusal has none."""
    events: list[DisclosureChecked] = []
    for number in range(RETAINED_FAILURES + RETAINED_PASSES):
        failed = number % 6 == 0
        hour, rest = divmod(number, 3600)
        at = f"2026-10-29T{hour:02d}:{rest // 60:02d}:{rest % 60:02d}Z"
        size = None if not failed and number % 4 == 1 else 1000 + number
        events.append(DisclosureChecked("alpha", at, 3 if failed else 0, size))
    return tuple(events)


def pinned_history(scenario: tuple[Event, ...]) -> tuple[Event, ...]:
    """The history the pin replays, ``SCENARIO`` and then ``disclosure_history``."""
    return (*scenario, *disclosure_history())


def replay(history: tuple[Event, ...]) -> State:
    state = State()
    for event in history:
        state.check(event)
        state.apply(event)
    return state


def replay_digest(history: tuple[Event, ...]) -> str:
    encoded = statesnapshot.canonical(
        statesnapshot.encode_value(replay(history), State)
    )
    return hashlib.sha256(encoded).hexdigest()


def current(scenario: tuple[Event, ...]) -> Pin:
    """The pin of the code this process imported, for the given scenario."""
    return Pin(
        statesnapshot.STATE_VERSION,
        statesnapshot.codec_digest(),
        replay_digest(pinned_history(scenario)),
        statesnapshot.STATE_CHANGES,
    )


def load_scenario(conftest: Path = CONFTEST) -> tuple[Event, ...]:
    """``SCENARIO`` from the test suite's ``conftest.py``, imported by path. It
    raises ``RuntimeError`` where the file is absent or holds no scenario."""
    spec = importlib.util.spec_from_file_location("dromon_state_pin_conftest", conftest)
    if spec is None or spec.loader is None or not conftest.is_file():
        raise RuntimeError(f"{conftest} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found: object = getattr(module, "SCENARIO", None)
    if not isinstance(found, tuple):
        raise RuntimeError(f"{conftest} holds no SCENARIO tuple")
    return cast(tuple[Event, ...], found)


@dataclass(frozen=True)
class Outcome:
    code: int
    message: str


def decide(pin: Pin, directory: Path, *, write: bool) -> Outcome:
    """What the command does for ``pin`` in ``directory``, and does it when
    ``write`` is set. It never replaces a file."""
    if pin.version == statesnapshot.UNUSABLE_STATE_VERSION:
        problem = statesnapshot.FOUND_STATE_CHANGES.problem or "it is unusable"
        return Outcome(1, f"refused: the set of state changes is unusable, {problem}")
    path = pin_path(pin.version, directory)
    text = pin_text(pin)
    if path.exists():
        try:
            held = parse_pin(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            return Outcome(1, f"refused: {path} exists and cannot be read, {error}")
        if held == pin:
            return Outcome(0, f"{path} already pins this set of state changes")
        return Outcome(
            1,
            f"refused: {path} pins this set of state changes with other digests, so"
            " the codec's types or the replayed state changed while the set did not."
            " Add src/dromon/statechanges/BRANCH.txt saying what changed, and run"
            " this again. A pin is never edited",
        )
    if not write:
        return Outcome(1, f"missing: {path} would hold\n{text}")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except OSError as error:
        return Outcome(1, f"refused: {path} could not be created, {error}")
    return Outcome(0, f"wrote {path}\n{text}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="state_pin.py", description="Write the state version's missing pin."
    )
    parser.add_argument(
        "--check", action="store_true", help="say what is missing and write nothing"
    )
    parser.add_argument(
        "--pins", type=Path, default=PIN_DIRECTORY, help="the pin directory"
    )
    arguments = parser.parse_args(argv)
    check: bool = arguments.check
    directory: Path = arguments.pins
    if not directory.is_dir():
        print(f"state_pin.py: {directory} is not a directory", file=sys.stderr)
        return 2
    try:
        scenario = load_scenario()
    except RuntimeError as error:
        print(f"state_pin.py: refused: {error}", file=sys.stderr)
        return 1
    outcome = decide(current(scenario), directory, write=not check)
    stream = sys.stderr if outcome.code else sys.stdout
    print(f"state_pin.py: {outcome.message}", file=stream)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())
