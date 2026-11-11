"""The pin of ``STATE_VERSION``. **Review of 49236ad, item 3, required by the
operator.** A change to what applying an event means, which keeps the state's shape
and does not raise ``STATE_VERSION``, was invisible at a start, which then restored a
snapshot the new code would not have built. This test pins each version with the
digest of the codec's types and the digest of the state a fixed history replays to,
so a change to either fails here until the version is raised and a pin is added.

How to answer a failure. Raise ``STATE_VERSION`` in ``dromon.statesnapshot`` and
append a pin for the new version with the digests the failure names. A pin is never
edited and never added under a version already pinned, which the second test refuses,
so the version cannot be left as it was. The history replayed is ``SCENARIO`` from
``conftest.py``, which the codec's tests keep covering every event kind, so a change
of meaning for an event it does not reach is left to ``dromond --check`` and to the
check after each start from a snapshot."""

import enum
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import pytest

from dromon import statesnapshot
from dromon.model import Event, HoldCause, State

PINS: tuple[tuple[int, str, str], ...] = (
    # 49236ad, round 15.
    (
        1,
        "579cbc82124241279a3a820b8179b605389efab4a84a8a6114e7c94379faaec0",
        "68217455f3da62c874e955025e1c9f1f652ee270d32bfc353aaec87a1d50fd70",
    ),
    # The round 15 follow-up, line_turn_ended's cause and brief_set's dirty state.
    (
        2,
        "fb2f80e283f55550762d8105fa90c08a1c2bf1504698b7cc3cc3ebefbfc552ef",
        "6e738ee5176bda170f29893bb148943196489dc6045b8366bc2a9352c6ead538",
    ),
    # The merge of feat/r15-runs-disclosure, disclosure passes and bytes and run tails.
    (
        3,
        "1ed89a710b96536b115ace4a5ba7d4a40e54d561cbd6823c874af7bfbb946d46",
        "4c36dab8af2c877d5f0014b3e6cb484412012e63f49cfa53bef7cdbf0d13a746",
    ),
    # Round 16, item 3, run_started's workdir_commit.
    (
        4,
        "62b1cf5f7c4f454877281837d58503115fd4bc031930171200a7fd97e620776b",
        "4ef0b2f9a3fd046894cedd1ebc30028c12f4ebc24f0ca989ed257bada1652209",
    ),
)
"""Each ``STATE_VERSION`` with its ``codec_digest`` and its replay digest, oldest
first, appended to and never edited."""


def replay_digest(scenario: tuple[Event, ...]) -> str:
    state = State()
    for event in scenario:
        state.check(event)
        state.apply(event)
    encoded = statesnapshot.canonical(statesnapshot.encode_value(state, State))
    return hashlib.sha256(encoded).hexdigest()


def test_the_state_version_is_pinned_with_the_codec_and_a_replay(
    scenario: tuple[Event, ...],
) -> None:
    now = (
        statesnapshot.STATE_VERSION,
        statesnapshot.codec_digest(),
        replay_digest(scenario),
    )
    assert now == PINS[-1], (
        "the codec's types or the state the scenario replays to changed, so raise"
        " STATE_VERSION in dromon.statesnapshot and append to PINS"
        f" {(now[0] + 1 if now[0] == PINS[-1][0] else now[0], now[1], now[2])!r}"
    )


def test_each_version_is_pinned_once_in_order() -> None:
    versions = [version for version, _, _ in PINS]
    assert versions == sorted(set(versions)), versions
    digests = [digest for _, codec, replay in PINS for digest in (codec, replay)]
    assert all(len(digest) == 64 for digest in digests)


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
    assert replay_digest(scenario[:-1]) != PINS[-1][2]
