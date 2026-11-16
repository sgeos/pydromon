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
conflicted at every merge of two such branches, are replaced. Version 6's digests are
the base set's pin, since no change came between.

How to answer a failure. A branch that changed the codec's types or the replayed state
adds ``src/dromon/statechanges/BRANCH.txt``, and then the pin file the failure names,
with the content it gives. A merge of two such branches needs a pin for the set they
make together, which the failure also names. A pin is never edited, since editing one
would accept a change without a new version. The history replayed is ``SCENARIO``
from ``conftest.py``, which the codec's tests keep covering every event kind, so a
change of meaning for an event it does not reach is left to ``dromond --check`` and to
the check after each start from a snapshot."""

import enum
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from dromon import statesnapshot
from dromon.model import Event, HoldCause, State

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


def replay_digest(scenario: tuple[Event, ...]) -> str:
    state = State()
    for event in scenario:
        state.check(event)
        state.apply(event)
    encoded = statesnapshot.canonical(statesnapshot.encode_value(state, State))
    return hashlib.sha256(encoded).hexdigest()


def current(scenario: tuple[Event, ...]) -> Pin:
    return Pin(
        statesnapshot.STATE_VERSION,
        statesnapshot.codec_digest(),
        replay_digest(scenario),
        statesnapshot.STATE_CHANGES,
    )


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


def read(*entries: Entry) -> tuple[str, ...]:
    return statesnapshot.read_state_changes(Directory(*entries))  # pyright: ignore[reportArgumentType]


def test_the_state_changes_are_read_sorted_without_the_readme() -> None:
    names = read(
        Entry("fix--b.txt"),
        Entry("README.md"),
        Entry("base.txt"),
        Entry(".DS_Store"),
        Entry("__pycache__", is_file=False),
    )
    assert names == ("base", "fix--b")


@pytest.mark.parametrize(
    "entry",
    [
        Entry("notes.md"),
        Entry("Upper.txt"),
        Entry("feat/one.txt"),
        Entry("sub.txt", is_file=False),
        Entry("a b.txt"),
        Entry("-leading.txt"),
    ],
)
def test_a_state_change_of_another_form_is_refused(entry: Entry) -> None:
    with pytest.raises(ValueError, match="not a state change file"):
        read(Entry("base.txt"), entry)


def test_no_state_change_or_too_many_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="not even the base"):
        read()
    monkeypatch.setattr(statesnapshot, "MAX_STATE_CHANGES", 2)
    with pytest.raises(ValueError, match="more than 2"):
        read(Entry("a.txt"), Entry("b.txt"), Entry("c.txt"))


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
    assert replay_digest(scenario) == pinned.replay
    assert replay_digest(scenario[:-1]) != pinned.replay
