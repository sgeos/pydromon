"""The bounded, in-memory tail of what each line's engine is doing."""

from dromon.activity import (
    MAX_ENTRIES,
    MAX_TEXT,
    TRUNCATED,
    ActivityKind,
    ActivityLog,
    ignore,
)


def kinds_and_texts(log: ActivityLog, line_id: str) -> list[tuple[ActivityKind, str]]:
    return [(entry.kind, entry.text) for entry in log.after(line_id, 0)]


def test_entries_are_kept_per_line_with_rising_revisions() -> None:
    log = ActivityLog()
    assert log.seq == 0 and log.after("alpha", 0) == []
    log.record("alpha", ActivityKind.TURN_STARTED, "prompt")
    log.record("beta", ActivityKind.TOOL, "Read")
    log.record("alpha", ActivityKind.TEXT, "hi")
    assert log.seq == 3
    assert kinds_and_texts(log, "alpha") == [
        (ActivityKind.TURN_STARTED, "prompt"),
        (ActivityKind.TEXT, "hi"),
    ]
    assert [entry.seq for entry in log.after("alpha", 0)] == [1, 3]
    assert [entry.text for entry in log.after("alpha", 1)] == ["hi"]


def test_a_chunk_extends_the_text_entry_and_takes_a_new_revision() -> None:
    log = ActivityLog()
    log.record("alpha", ActivityKind.TEXT, "hel", chunk=True)
    first = log.after("alpha", 0)[0]
    log.record("alpha", ActivityKind.TEXT, "lo", chunk=True)
    (extended,) = log.after("alpha", first.seq)
    assert (extended.entry_id, extended.text) == (first.entry_id, "hello")
    assert extended.seq > first.seq


def test_whole_messages_and_other_kinds_start_new_entries() -> None:
    log = ActivityLog()
    log.record("alpha", ActivityKind.TEXT, "one")
    log.record("alpha", ActivityKind.TEXT, "two")
    log.record("alpha", ActivityKind.TOOL, "Bash")
    log.record("alpha", ActivityKind.TEXT, "three", chunk=True)
    assert [text for _, text in kinds_and_texts(log, "alpha")] == [
        "one",
        "two",
        "Bash",
        "three",
    ]


def test_reasoning_collapses_into_one_entry() -> None:
    log = ActivityLog()
    for _ in range(5):
        log.record("alpha", ActivityKind.THINKING, "")
    assert kinds_and_texts(log, "alpha") == [(ActivityKind.THINKING, "")]


def test_text_is_bounded_and_a_truncated_entry_takes_no_more_chunks() -> None:
    log = ActivityLog()
    log.record("alpha", ActivityKind.TEXT, "x" * (MAX_TEXT + 10))
    (entry,) = log.after("alpha", 0)
    assert len(entry.text) == MAX_TEXT and entry.text.endswith(TRUNCATED)
    log.record("alpha", ActivityKind.TEXT, "more", chunk=True)
    assert [e.text for e in log.after("alpha", entry.seq)] == ["more"]
    grown = ActivityLog()
    for _ in range(MAX_TEXT):
        grown.record("alpha", ActivityKind.TEXT, "ab", chunk=True)
    assert all(len(e.text) <= MAX_TEXT for e in grown.after("alpha", 0))


def test_a_line_keeps_only_its_newest_entries() -> None:
    log = ActivityLog()
    for index in range(MAX_ENTRIES + 5):
        log.record("alpha", ActivityKind.TOOL, str(index))
    kept = log.after("alpha", 0)
    assert len(kept) == MAX_ENTRIES
    assert kept[0].text == "5" and kept[-1].text == str(MAX_ENTRIES + 4)


def test_an_observer_records_into_its_line_and_ignore_records_nothing() -> None:
    log = ActivityLog()
    observe = log.observer("alpha")
    observe(ActivityKind.TEXT, "a", chunk=True)
    observe(ActivityKind.TEXT, "b", chunk=True)
    assert kinds_and_texts(log, "alpha") == [(ActivityKind.TEXT, "ab")]
    ignore(ActivityKind.TEXT, "lost", chunk=True)
    assert log.seq == 2


def test_entries_serialize_for_the_client_protocol() -> None:
    log = ActivityLog()
    log.record("alpha", ActivityKind.TURN_ENDED, "success")
    assert log.after("alpha", 0)[0].to_json() == {
        "entry_id": 1,
        "seq": 1,
        "kind": "turn_ended",
        "text": "success",
    }
