"""The pin of ``STATE_VERSION``. **Review of 49236ad, item 3, required by the
operator.** A change to what applying an event means, which keeps the state's shape
and does not change ``STATE_VERSION``, was invisible at a start, which then restored a
snapshot the new code would not have built. This test pins each version with the
digest of the codec's types and the digest of the state a fixed history replays to,
so a change to either fails here until the version changes and a pin is added.

**Round 17, item 3.** The version is the digest of the set of state change files in
``src/dromon/statechanges/``, and each pin is one file in ``tests/state_pins/`` named
for the version it pins, so two branches that change the state add different files
and never conflict. The whole numbers 1 to 6 and their pins in one tuple, which
conflicted at every merge of two such branches, are replaced. Version 6's codec digest
is the base set's pin, since no change came between. Its replay digest is of the longer
history the review of 0db077c called for, which ``disclosure_history`` gives, and was
written once, before the pin reached ``main``.

How to answer a failure. A branch that changed the codec's types or the replayed state
adds ``src/dromon/statechanges/BRANCH.txt``, and then the pin file the failure names,
with the content it gives. A merge of two such branches needs a pin for the set they
make together, which the failure also names. A pin is never edited, since editing one
would accept a change without a new version. The history replayed is ``SCENARIO``
from ``conftest.py``, which the codec's tests keep covering every event kind, and then
enough disclosure checks to pass each of their caps. A change of meaning for an event
the history does not reach is left to ``dromond --check`` and to the check after each
start from a snapshot. **Residual, inference from reading ``State.apply``.** Its other
caps, ``MAX_TASK_RECORDS``, ``MAX_CORRELATIONS`` and
``MAX_REQUEST_KEYS_PER_PARTY``, and the saturation at ``MAX_COST_MICRO_USD``, are not
reached by the history either, so a change to one is not caught here."""

import enum
import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from dromon import model, statesnapshot
from dromon.model import DisclosureChecked, Event, HoldCause, State

PIN_DIRECTORY = Path(__file__).parent / "state_pins"

DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Pin:
    version: str
    codec: str
    replay: str
    changes: tuple[str, ...]


def pin_path(version: str) -> Path:
    return PIN_DIRECTORY / f"{version[:16]}.txt"


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
    return Pin(
        statesnapshot.STATE_VERSION,
        statesnapshot.codec_digest(),
        replay_digest(pinned_history(scenario)),
        statesnapshot.STATE_CHANGES,
    )


def test_the_history_reaches_past_every_disclosure_cap(
    scenario: tuple[Event, ...],
) -> None:
    added = disclosure_history()
    failures = sum(1 for e in added if e.matched_lines > 0)
    passes = len(added) - failures
    sized = sum(1 for e in added if e.checked_bytes is not None)
    assert (failures, passes) == (RETAINED_FAILURES, RETAINED_PASSES)
    assert failures > model.MAX_DISCLOSURE_CHECKS, "raise RETAINED_FAILURES"
    assert passes > model.MAX_DISCLOSURE_PASSES, "raise RETAINED_PASSES"
    assert sized > model.MAX_DISCLOSURE_SIZES, "raise both"
    state = replay(pinned_history(scenario))
    assert len(state.disclosure_checks["alpha"]) == model.MAX_DISCLOSURE_CHECKS
    assert len(state.disclosure_passes["alpha"]) == model.MAX_DISCLOSURE_PASSES
    assert len(state.disclosure_sizes["alpha"]) == model.MAX_DISCLOSURE_SIZES


@pytest.mark.parametrize(
    "cap", ["MAX_DISCLOSURE_CHECKS", "MAX_DISCLOSURE_PASSES", "MAX_DISCLOSURE_SIZES"]
)
def test_a_changed_disclosure_cap_moves_the_replay(
    scenario: tuple[Event, ...], monkeypatch: pytest.MonkeyPatch, cap: str
) -> None:
    """The audit's mutation, which the pin did not catch."""
    before = replay_digest(pinned_history(scenario))
    monkeypatch.setattr(model, cap, getattr(model, cap) + 1)
    assert replay_digest(pinned_history(scenario)) != before


def test_the_state_version_is_pinned_with_the_codec_and_a_replay(
    scenario: tuple[Event, ...],
) -> None:
    now = current(scenario)
    path = pin_path(now.version)
    added = f"add {path.relative_to(PIN_DIRECTORY.parents[1])} holding\n{pin_text(now)}"
    assert path.exists(), (
        "no pin names this set of state changes. If this is a merge of branches"
        " that each changed the state, or a branch that added its state change"
        f" file, {added}"
    )
    pinned = parse_pin(path.read_text(encoding="utf-8"))
    assert pinned == now, (
        "the codec's types or the state the scenario replays to changed while the set"
        " of state changes did not. Add src/dromon/statechanges/BRANCH.txt saying what"
        " changed, and then the pin this test asks for. Never edit a pin"
    )


def pins() -> list[tuple[Path, Pin]]:
    return [
        (path, parse_pin(path.read_text(encoding="utf-8")))
        for path in sorted(PIN_DIRECTORY.iterdir())
    ]


def test_each_pin_is_named_for_the_set_it_pins() -> None:
    found = pins()
    assert found, "no pin at all"
    for path, pin in found:
        assert pin.changes == tuple(sorted(set(pin.changes))), path
        assert statesnapshot.state_version_of(pin.changes) == pin.version, path
        assert path == pin_path(pin.version), path
    versions = [pin.version for _, pin in found]
    assert len(versions) == len(set(versions))


def test_every_pinned_change_is_still_recorded() -> None:
    """A state change file is never removed or renamed, so each set a pin names is
    within the set this code holds."""
    held = set(statesnapshot.STATE_CHANGES)
    for path, pin in pins():
        assert set(pin.changes) <= held, path


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "at least one change"),
        ("version x\n", "not a SHA-256"),
        ("colour blue\n", "unexpected line"),
        (f"version {'a' * 64}\ncodec {'b' * 64}\nreplay {'c' * 64}\n", "at least one"),
        (
            f"version {'a' * 64}\nversion {'a' * 64}\ncodec {'b' * 64}\n",
            "unexpected line",
        ),
        (
            f"codec {'b' * 64}\nversion {'a' * 64}\nreplay {'c' * 64}\nchange base\n",
            "in the form",
        ),
    ],
)
def test_a_pin_of_another_form_is_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_pin(text)


def test_a_merge_of_two_state_changes_has_its_own_version() -> None:
    """Two branches each add a file. The merge holds both, so its version is neither
    branch's, and a snapshot either wrote is ignored by the merged code."""
    one = statesnapshot.state_version_of(("base", "feat--one"))
    other = statesnapshot.state_version_of(("base", "feat--other"))
    both = statesnapshot.state_version_of(("base", "feat--one", "feat--other"))
    assert len({one, other, both}) == 3


class Entry:
    def __init__(self, name: str, is_file: bool = True) -> None:
        self.name = name
        self._is_file = is_file

    def is_file(self) -> bool:
        return self._is_file


class Directory:
    def __init__(self, *entries: Entry) -> None:
        self.entries = entries

    def iterdir(self) -> tuple[Entry, ...]:
        return self.entries


def read(*entries: Entry) -> statesnapshot.StateChanges:
    return statesnapshot.read_state_changes(Directory(*entries))  # pyright: ignore[reportArgumentType]


def test_the_state_changes_are_read_sorted_without_the_readme() -> None:
    found = read(
        Entry("fix--b.txt"),
        Entry("README.md"),
        Entry("base.txt"),
        Entry(".DS_Store"),
        Entry("__pycache__", is_file=False),
    )
    assert found == statesnapshot.StateChanges(("base", "fix--b"), (), None)


@pytest.mark.parametrize(
    "entry",
    [
        Entry("notes.md"),
        Entry("Upper.txt"),
        Entry("feat/one.txt"),
        Entry("sub.txt", is_file=False),
        Entry("a b.txt"),
        Entry("-leading.txt"),
        Entry("base.txt.orig"),
        Entry("fix--b.txt~"),
    ],
)
def test_a_state_change_of_another_form_is_left_out_and_named(entry: Entry) -> None:
    """Review of 0db077c, finding 4. Such an entry raised at import, which stopped
    every command. It is now left out, and named for the gate's warning."""
    found = read(Entry("base.txt"), entry)
    assert found == statesnapshot.StateChanges(("base",), (entry.name,), None)


def test_no_state_change_or_too_many_is_a_problem_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = read(Entry("base.txt.orig"))
    assert empty.problem is not None and "not even the base" in empty.problem
    assert empty.ignored == ("base.txt.orig",)
    monkeypatch.setattr(statesnapshot, "MAX_STATE_CHANGES", 2)
    many = read(Entry("a.txt"), Entry("b.txt"), Entry("c.txt"))
    assert many.problem == "more than 2 state changes"
    assert many.names == ()


class Unreadable:
    def iterdir(self) -> tuple[Entry, ...]:
        raise PermissionError(13, "Permission denied")


def test_a_directory_that_cannot_be_read_is_a_problem() -> None:
    found = statesnapshot.read_state_changes(Unreadable())  # pyright: ignore[reportArgumentType]
    assert found.problem is not None and "Permission denied" in found.problem


def test_an_unusable_set_refuses_every_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The log is replayed instead, which is always correct."""
    monkeypatch.setattr(
        statesnapshot,
        "FOUND_STATE_CHANGES",
        statesnapshot.StateChanges((), (), "no state change is recorded"),
    )
    with pytest.raises(
        statesnapshot.SnapshotRefused, match="state version is unusable"
    ):
        statesnapshot.read(tmp_path / "absent.snapshot", 1, lambda _: None)


def test_a_stray_file_does_not_stop_the_package_importing(tmp_path: Path) -> None:
    """The audit's case. A copy of the package with a merge tool's leftover in
    ``statechanges`` imports, gives the same version, and names the leftover."""
    package = Path(statesnapshot.__file__).parent
    copy = tmp_path / "tree" / "dromon"
    shutil.copytree(package, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / statesnapshot.STATE_CHANGES_PACKAGE / "base.txt.orig").write_text("")
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        " from dromon import statesnapshot as s;"
        " print(s.STATE_VERSION, s.FOUND_STATE_CHANGES.ignored, s.__file__)"
    )
    found = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", probe, str(copy.parent)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert found[0] == statesnapshot.STATE_VERSION
    assert found[1] == "('base.txt.orig',)"
    assert Path(found[2]).is_relative_to(copy)


def test_the_installed_set_is_read_from_the_package() -> None:
    directory = (
        Path(statesnapshot.__file__).parent / statesnapshot.STATE_CHANGES_PACKAGE
    )
    on_disk = sorted(p.stem for p in directory.glob("*.txt"))
    assert list(statesnapshot.STATE_CHANGES) == on_disk
    assert "base" in on_disk


# The digest the pin rests on.


def test_the_codec_digest_is_stable_within_a_process() -> None:
    assert statesnapshot.codec_digest() == statesnapshot.codec_digest()


@dataclass(frozen=True)
class Probe:
    name: str
    count: int | None
    tags: tuple[str, ...]
    sizes: dict[str, int]
    kind: Literal["a", "b"]
    held: HoldCause | None
    seen: frozenset[str] = field(default_factory=frozenset[str])


type Alias = Probe | None


@pytest.mark.parametrize(
    ("hint", "written"),
    [
        (str, "str"),
        (None, "None"),
        (int | None, "None | int"),
        (None | int, "None | int"),
        (tuple[str, ...], "tuple[str, ...]"),
        (tuple[str, int], "tuple[str, int]"),
        (dict[str, int | None], "dict[str, None | int]"),
        (Mapping[str, int], "Mapping[str, int]"),
        (frozenset[str], "frozenset[str]"),
        (Literal["a", "b"], "Literal['a', 'b']"),
        (HoldCause | None, "HoldCause | None"),
        (Alias, "None | Probe"),
        (Probe, "Probe"),
    ],
)
def test_a_hint_is_written_without_python_s_own_text(
    hint: object, written: str
) -> None:
    assert statesnapshot.hint_text(hint) == written


def test_a_hint_with_no_text_is_refused() -> None:
    with pytest.raises(TypeError, match="no text"):
        statesnapshot.hint_text("str")


def test_a_change_to_an_event_field_changes_the_codec_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = statesnapshot.codec_digest()
    real = statesnapshot.hint_text

    def renamed(hint: object) -> str:
        text = real(hint)
        return "FailureCause2" if text == "FailureCause" else text

    monkeypatch.setattr(statesnapshot, "hint_text", renamed)
    assert statesnapshot.codec_digest() != before


class Before(enum.Enum):
    ONE = "one"


class After(enum.Enum):
    ONE = "one"
    TWO = "two"


def test_the_lines_name_each_field_and_each_enumeration_s_values() -> None:
    lines = statesnapshot.type_lines((Probe,))
    assert "Probe.count: None | int" in lines
    assert "Probe.kind: Literal['a', 'b']" in lines
    assert f"HoldCause = {[cause.value for cause in HoldCause]!r}" in lines
    assert lines == sorted(lines)


def test_a_change_to_an_enumeration_s_values_changes_its_line() -> None:
    before = statesnapshot.type_lines((Before,))
    after = statesnapshot.type_lines((After,))
    assert [line.split(" = ")[1] for line in before] == ["['one']"]
    assert [line.split(" = ")[1] for line in after] == ["['one', 'two']"]


def test_a_different_replay_fails_the_pin(scenario: tuple[Event, ...]) -> None:
    pinned = parse_pin(pin_path(statesnapshot.STATE_VERSION).read_text("utf-8"))
    history = pinned_history(scenario)
    assert replay_digest(history) == pinned.replay
    assert replay_digest(history[:-1]) != pinned.replay
    assert replay_digest(scenario) != pinned.replay


@pytest.mark.parametrize("name", ["valid.txt", "stray.orig", ".hidden"])
def test_directory_consumption_is_bounded_before_sorting(name: str) -> None:
    class LargeDirectory:
        consumed = 0

        def iterdir(self) -> Iterator[Entry]:
            for _ in range(10000):
                self.consumed += 1
                yield Entry(name)

    directory = LargeDirectory()
    found = statesnapshot.read_state_changes(directory)  # pyright: ignore[reportArgumentType]
    assert directory.consumed == statesnapshot.MAX_STATE_CHANGE_ENTRIES + 1
    assert found.names == () and found.ignored == ()
    assert found.problem == (
        f"more than {statesnapshot.MAX_STATE_CHANGE_ENTRIES} state change directory"
        " entries"
    )


def test_ignored_names_are_bounded_and_overflow_refuses_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(statesnapshot, "MAX_IGNORED_STATE_CHANGES", 2)
    accepted = read(Entry("base.txt"), Entry("b.orig"), Entry("a.orig"))
    assert accepted == statesnapshot.StateChanges(("base",), ("a.orig", "b.orig"), None)
    refused = read(Entry("base.txt"), Entry("c.orig"), Entry("b.orig"), Entry("a.orig"))
    assert refused == statesnapshot.StateChanges(
        (), ("a.orig", "b.orig"), "more than 2 ignored state change entries"
    )


def test_directory_entry_limit_accepts_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(statesnapshot, "MAX_STATE_CHANGE_ENTRIES", 2)
    assert read(Entry("base.txt"), Entry(".hidden")) == statesnapshot.StateChanges(
        ("base",), (), None
    )


def test_an_iterator_read_error_refuses_a_partial_set() -> None:
    class InterruptedDirectory:
        def iterdir(self) -> Iterator[Entry]:
            yield Entry("base.txt")
            raise PermissionError("directory iteration refused")

    found = statesnapshot.read_state_changes(InterruptedDirectory())  # pyright: ignore[reportArgumentType]
    assert found.names == () and found.ignored == ()
    assert found.problem is not None and "directory iteration refused" in found.problem
