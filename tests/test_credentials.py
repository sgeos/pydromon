"""Round 15, item 1. Credential-shaped values are masked in the free text the event log
records, and everything else in an event is left as it was.

The sample credentials are built from parts at run time, so that no scanner reads this
file as holding one, and none is a real value."""

import dataclasses
import sqlite3
import typing
from pathlib import Path

import pytest

from dromon import credentials, model
from dromon.credentials import MASK, scrub_event, scrub_text
from dromon.eventlog import EventLog
from dromon.model import (
    BriefSet,
    Event,
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    RoleNoted,
    RunEnded,
    RunStarted,
    RunStatus,
    TaskNoted,
)

ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
ENGINE = "claude-opus-5-5"
AT = "2026-11-05T00:00:00Z"


def make(prefix: str, body: str) -> str:
    return prefix + body


FAKES = {
    "github": make("ghp_", "a1B2" * 9),
    "github fine": make("github_pat_", "A1b2C3" * 5),
    "gitlab": make("glpat-", "abcd1234" * 3),
    "slack": make("xoxb-", "1234567890-abcdefghij"),
    "anthropic": make("sk-ant-", "api03-" + "Zy9x" * 6),
    "openai": make("sk-", "Qw3r" * 10),
    "stripe": make("sk_live_", "Ab12" * 5),
    "npm": make("npm_", "a1b2c3d4e5" * 3 + "a1b2c3"),
    "huggingface": make("hf_", "AbCd1234" * 4),
    "amazon key": make("ASIA", "ABCDEFGHIJKLMNOP"),
    "amazon session": make("IQoJb3JpZ2lu", "X1y2" * 20 + "=="),
    "google": make("AIza", "Sy" + "A1b2C3d4" * 4 + "x"),
    "google oauth": make("ya29.", "a0Af" + "B1c2" * 8),
    "json web token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
}


@pytest.mark.parametrize("kind", sorted(FAKES))
def test_a_known_token_is_masked_wherever_it_stands(kind: str) -> None:
    token = FAKES[kind]
    for text in (
        f"token {token} end",
        f"({token})",
        f"line one\n{token}\nline three",
        f"export X='{token}'",
    ):
        found = scrub_text(text)
        assert token not in found
        assert MASK in found
    assert scrub_text(f"a {token} b") == f"a {MASK} b"


def test_a_private_key_block_is_masked_to_its_end_or_the_end_of_the_text() -> None:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
    whole = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    assert scrub_text(f"see\n{whole}\nafter") == f"see\n{MASK}\nafter"
    cut = f"-----BEGIN RSA PRIVATE KEY-----\n{body}"
    assert scrub_text(f"see {cut}") == f"see {MASK}"


def test_the_forms_of_an_environment_dump_and_a_service_reply_are_masked() -> None:
    assert (
        scrub_text("AWS_SESSION_TOKEN=abcdefgh12345678") == f"AWS_SESSION_TOKEN={MASK}"
    )
    assert scrub_text("DB_PASSWORD=hunter2hunter2 next") == f"DB_PASSWORD={MASK} next"
    reply = '{"AccessKeyId": "k", "SessionToken": "FwoGabcdefghijklmnop", "n": 1}'
    found = scrub_text(reply)
    assert "FwoGabcdefghijklmnop" not in found
    assert f'"SessionToken": "{MASK}"' in found
    assert '"AccessKeyId": "k"' in found
    assert scrub_text("Authorization: Bearer abcdef0123456789") == (
        f"Authorization: Bearer {MASK}"
    )
    assert scrub_text("https://user:pa55word@host/x") == f"https://{MASK}@host/x"


@pytest.mark.parametrize(
    "text",
    [
        "commit 0123456789abcdef0123456789abcdef01234567 is fine",
        "digest " + "ab" * 32,
        "id 123e4567-e89b-12d3-a456-426614174000",
        "the key point is that a token of thanks is a secret kindness",
        "SESSION=short",
        "TOKEN=abc",
        "see sk-short and ghp_short and hf_x",
        "the password: is asked at the door",
        '{"token": "short"}',
        "Bearer abc",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert scrub_text(text) == text


def test_lower_case_names_and_a_user_and_password_without_a_scheme_are_masked() -> None:
    # Review of 3344ca6. The upper case pattern and the scheme pattern missed these.
    assert scrub_text("github_token=abcdefgh12345678 ok") == f"github_token={MASK} ok"
    assert scrub_text("run --api-key=abcdefgh12345678") == f"run --api-key={MASK}"
    assert scrub_text("Db_Password=hunter2hunter2") == f"Db_Password={MASK}"
    assert (
        scrub_text("psql admin:s3cretpw@db.internal/x") == f"psql {MASK}@db.internal/x"
    )
    # An image reference with a digest, an address, a time and prose are left alone.
    for kept in (
        "image python:3.12@sha256:" + "ab" * 32,
        "mail ana@example.org about it",
        "at 12:30@home",
        "git@github.com:org/repo.git",
        "token_count=12",
        "session=short",
    ):
        assert scrub_text(kept) == kept, kept


def test_a_password_with_a_slash_or_an_at_sign_is_masked_whole() -> None:
    # Review of 03ccbf3. The classes of the first patterns stopped at both, so the rest
    # of the password was recorded.
    cases = {
        "https://user:p@ss@host/x": f"https://{MASK}@host/x",
        "https://user:pa/ss@host/x?y=1": f"https://{MASK}@host/x?y=1",
        "https://user:a/b@c@host.example": f"https://{MASK}@host.example",
        "ssh://git:/tmp/key@host": f"ssh://{MASK}@host",
        "psql admin:p@ss@db.internal/x": f"psql {MASK}@db.internal/x",
        "psql admin:@pass@db.internal": f"psql {MASK}@db.internal",
    }
    for text, expected in cases.items():
        found = scrub_text(text)
        assert found == expected, text
        assert scrub_text(found) == found, "idempotent"


def test_text_with_a_port_a_path_a_digest_or_an_address_is_left_alone() -> None:
    for kept in (
        "https://host:8080/pkg@1.0.0",
        "https://example.com:8443/@scope/name",
        "https://example.com/a@b",
        "docker://registry:5000/img@sha256:" + "ab" * 32,
        "ssh://git@github.com/org/repo",
        "git@github.com:org/repo.git",
        "rsync host:dir/file@name now",
        "python:3.12@sha256:" + "ab" * 32,
        "mail ana@example.org and bob@example.org",
        "at 12:30@home",
    ):
        assert scrub_text(kept) == kept, kept


def test_masking_is_idempotent_and_never_lengthens_a_text_past_a_mask() -> None:
    text = f"a {FAKES['github']} b AWS_SECRET_ACCESS_KEY=abcdefgh12345678 c"
    once = scrub_text(text)
    assert scrub_text(once) == once
    assert len(once) < len(text)


def test_a_named_secret_is_masked_by_value_when_long_enough() -> None:
    value = "correct-horse-battery"
    assert scrub_text(f"it is {value}!", [value]) == f"it is {MASK}!"
    assert scrub_text(f"{value}{value}", [value]) == f"{MASK}{MASK}"
    # A value inside a longer one is masked once, the longer first.
    longer = value + "-staple"
    assert scrub_text(longer, [value, longer]) == MASK
    short = "abc"
    assert scrub_text(f"an {short} here", [short]) == f"an {short} here"
    assert scrub_text("nothing", [""]) == "nothing"


def test_the_values_of_granted_names_come_from_the_environment() -> None:
    granted = {"alpha": ("API_ONE", "API_TWO", "MISSING"), "beta": ("API_ONE", "EMPTY")}
    environment = {
        "API_ONE": "one-value-here",
        "API_TWO": "two-value-here",
        "EMPTY": "",
    }
    assert credentials.environment_values(granted, environment) == (
        "one-value-here",
        "two-value-here",
    )
    assert credentials.environment_values({}, environment) == ()
    many = {
        "a": tuple(f"N{index}" for index in range(credentials.MAX_SECRET_VALUES + 9))
    }
    found = credentials.environment_values(
        many, {name: f"value-{name}" for name in many["a"]}
    )
    assert len(found) == credentials.MAX_SECRET_VALUES


def test_an_event_has_only_its_text_fields_masked() -> None:
    token = FAKES["github"]
    sent = MessageSent(
        "m1", ALPHA, ANA, MessageKind.QUESTION, f"use {token}?", (f"yes {token}", "no")
    )
    found = scrub_event(sent)
    assert isinstance(found, MessageSent)
    assert found.body == f"use {MASK}?"
    assert found.choices == (f"yes {MASK}", "no")
    assert (found.message_id, found.sender, found.recipient, found.kind) == (
        "m1",
        ALPHA,
        ANA,
        MessageKind.QUESTION,
    )
    brief = BriefSet(
        "alpha", f"t {token}", "d", (f"/a/{token}",), "n", "c" * 40, "d" * 40, AT
    )
    masked = scrub_event(brief)
    assert isinstance(masked, BriefSet)
    assert token not in masked.task + "".join(masked.artifacts)
    unchanged = MessageSent("m2", ALPHA, ANA, MessageKind.NOTE, "plain")
    assert scrub_event(unchanged) is unchanged
    other = LineCreated("alpha", ENGINE, "subscription")
    assert scrub_event(other) is other


def test_every_event_class_is_classified_as_scrubbed_or_without_free_text() -> None:
    """A new event class fails this until someone decides whether it holds text."""
    without_text = {
        "AgentRegistered": "an identifier and a digest",
        "AgentRevoked": "an identifier",
        "BudgetHandoffRequested": "a provider name and a time",
        "ContextHandedOff": "an enumeration, identifiers and a digest",
        "DisclosureChecked": "a time",
        "LeaseReleased": "a name",
        "LineAwakened": "an identifier",
        "LineConfigured": "sealed by the sealed store",
        "LineCostMarked": "an identifier",
        "LineCostRecorded": "an identifier, a whole number and two flags",
        "LineCreated": "identifiers and enumerations",
        "LineHeld": "an identifier",
        "LineHibernated": "an identifier",
        "LineOverridden": "operator settings, a residual the document names",
        "LineRebound": "identifiers and enumerations",
        "LineReleased": "an identifier",
        "LineRetired": "an identifier and a time",
        "LineSession": "an engine's session identifier",
        "LineSessionCleared": "an identifier",
        "LineStateChanged": "an enumeration",
        "LineTurnEnded": "a commit name",
        "MessageCorrelated": "an identifier",
        "MessageDelivered": "an identifier",
        "MessagePrioritized": "an identifier",
        "MessageRead": "an identifier",
        "MessageReturned": "identifiers",
        "MessageUnconfirmed": "identifiers",
        "OperatorRegistered": "an identifier",
        "OwnershipChanged": "identifiers and times",
        "PeerDeclared": "operator configuration",
        "PeerKeyed": "an identifier and a digest",
        "PeerOpened": "identifiers and a time",
        "PeerRemoved": "an identifier",
        "ProjectOverridden": "operator settings, a residual the document names",
        "QuestionSuperseded": "identifiers",
        "QuestionWithdrawn": "an identifier",
        "RoleDeclared": "operator configuration",
        "RoleHeld": "an identifier and a time",
        "RoleRetired": "an identifier and a time",
        "RunCleared": "identifiers and a time",
        "SeatBound": "identifiers",
        "SeatUnbound": "identifiers",
        "TurnCorrelated": "identifiers",
        "WakeIssued": "an identifier",
        "WakeRequested": "identifiers",
        "WakeWithdrawn": "an identifier",
    }
    every = {cls.__name__ for cls in typing.get_args(model.Event.__value__)}
    covered = {cls.__name__ for cls in credentials.COVERED}
    assert every == covered | set(without_text)
    assert not covered & set(without_text)
    for cls, names in credentials.COVERED.items():
        fields = {field.name for field in dataclasses.fields(cls)}
        assert set(names) <= fields


def test_a_run_started_event_has_its_arguments_masked_and_its_names_kept() -> None:
    token = FAKES["github"]
    started = RunStarted(
        "r-1",
        "alpha",
        None,
        None,
        "full-gate",
        ("tool", f"--token={token}", f"--auth={token}"),
        "/w",
        "out.txt",
        4242,
        AT,
        AT,
    )
    found = scrub_event(started)
    assert isinstance(found, RunStarted)
    assert token not in "".join(found.argv) and found.argv[0] == "tool"
    assert found.command == "full-gate" and found.pid == 4242


def test_the_events_left_unscrubbed_on_purpose_are_named_with_a_reason() -> None:
    # The classification test above lists them. The configuration events pass through
    # whole, since the daemon decodes them again to build an engine.
    token = FAKES["github"]
    configured = model.LineOverridden("alpha", f'{{"instructions": "{token}"}}')
    assert scrub_event(configured) is configured
    for name in ("LineConfigured", "LineOverridden", "ProjectOverridden"):
        assert name not in {cls.__name__ for cls in credentials.COVERED}


def test_choices_that_differ_only_by_a_credential_are_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    # Review of 3344ca6. Masked, the two are equal, and an answer names a choice by its
    # text, so storing them would be ambiguous and dropping one silent. The refusal is
    # right. The reason must be told and must not echo a value.
    first, second = FAKES["github"], FAKES["gitlab"]
    question = MessageSent(
        "m1",
        ALPHA,
        ANA,
        MessageKind.QUESTION,
        "which?",
        (f"use {first}", f"use {second}"),
    )
    with pytest.raises(model.InvalidField) as caught:
        scrub_event(question)
    text = str(caught.value)
    assert "masked" in text and "repeat" in text
    assert first not in text and second not in text
    with EventLog(tmp_path / "events.sqlite") as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", ENGINE, "subscription"))
        with pytest.raises(model.InvalidField):
            log.append(question)
        assert "m1" not in log.state.messages


def test_the_log_stores_and_applies_the_masked_text(tmp_path: Path) -> None:
    token = FAKES["amazon session"]
    path = tmp_path / "events.sqlite"
    events: tuple[Event, ...] = (
        OperatorRegistered("ana"),
        LineCreated("alpha", ENGINE, "subscription"),
        MessageSent("m1", ANA, ALPHA, MessageKind.NOTE, f"the token is {token}"),
    )
    with EventLog(path) as log:
        for event in events:
            log.append(event)
        entry = log.append(
            MessageSent("m9", ANA, ALPHA, MessageKind.NOTE, f"again {token}")
        )
        assert isinstance(entry.event, MessageSent)
        assert entry.event.body == f"again {MASK}"
        assert log.state.messages["m9"].body == f"again {MASK}"
        assert log.state.messages["m1"].body == f"the token is {MASK}"
    connection = sqlite3.connect(path)
    try:
        stored = [row[0] for row in connection.execute("SELECT payload FROM events")]
    finally:
        connection.close()
    assert stored
    assert all(token not in payload for payload in stored)
    assert any(MASK in payload for payload in stored)


def test_named_secrets_are_masked_in_the_log(tmp_path: Path) -> None:
    value = "plain-words-secret-value"
    with EventLog(tmp_path / "events.sqlite") as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", ENGINE, "subscription"))
        log.secrets = lambda: [value]
        log.append(MessageSent("m1", ANA, ALPHA, MessageKind.NOTE, f"is {value}"))
        assert log.state.messages["m1"].body == f"is {MASK}"


def test_a_failing_secret_source_refuses_an_event_with_text_and_stores_nothing(
    tmp_path: Path,
) -> None:
    # Review of 03ccbf3. The patterns alone would store a literal value they cannot
    # know, so the event is refused, and the refusal does not echo the failure.
    value = "plain-words-secret-value"
    with EventLog(tmp_path / "events.sqlite") as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", ENGINE, "subscription"))
        before = log.last_seq

        def broken() -> list[str]:
            raise RuntimeError(f"cannot read {value}")

        log.secrets = broken
        with pytest.raises(credentials.SecretsUnavailable) as caught:
            log.append(MessageSent("m2", ANA, ALPHA, MessageKind.NOTE, f"is {value}"))
        assert isinstance(caught.value, model.EventRejected)
        assert "RuntimeError" in str(caught.value)
        assert value not in str(caught.value)
        assert log.last_seq == before
        assert "m2" not in log.state.messages
        # An event with no text field is stored as before, and the source recovers.
        log.append(LineCreated("beta", ENGINE, "subscription"))
        log.secrets = lambda: [value]
        log.append(MessageSent("m2", ANA, ALPHA, MessageKind.NOTE, f"is {value}"))
        assert log.state.messages["m2"].body == f"is {MASK}"


def test_a_start_from_a_snapshot_stores_nothing_and_every_later_append_is_scrubbed(
    tmp_path: Path,
) -> None:
    """Review of 7744806, for the merge of the state snapshot. The restore only reads
    the log and the snapshot, which hold what ``append`` stored, so it cannot store an
    unmasked event. The daemon sets ``secrets`` after the open, and the first append
    after a restore is scrubbed and fails closed as any other."""
    value = "plain-words-secret-value"
    path = tmp_path / "events.sqlite"
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", ENGINE, "subscription"))
        log.secrets = lambda: [value]
        log.append(MessageSent("m1", ANA, ALPHA, MessageKind.NOTE, f"is {value}"))
    with EventLog(path, snapshots=True) as restored:
        assert restored.start.snapshot == "used"
        assert restored.secrets is None, "the open appended nothing, so none is needed"
        assert restored.last_seq == restored.start.snapshot_seq
        assert restored.state.messages["m1"].body == f"is {MASK}"
        restored.secrets = lambda: [value]
        restored.append(MessageSent("m2", ANA, ALPHA, MessageKind.NOTE, f"is {value}"))
        assert restored.state.messages["m2"].body == f"is {MASK}"

        def broken() -> list[str]:
            raise RuntimeError("no source")

        restored.secrets = broken
        before = restored.last_seq
        with pytest.raises(credentials.SecretsUnavailable):
            restored.append(
                MessageSent("m3", ANA, ALPHA, MessageKind.NOTE, f"is {value}")
            )
        assert restored.last_seq == before
    stored = [path, path.with_name(path.name + ".snapshot")]
    assert stored[1].exists(), "the snapshot was written, so it was read"
    for file in stored:
        assert value.encode() not in file.read_bytes(), file.name


def test_the_secret_source_is_asked_only_for_an_event_with_text(
    tmp_path: Path,
) -> None:
    asked: list[int] = []

    def source() -> list[str]:
        asked.append(1)
        return []

    with EventLog(tmp_path / "events.sqlite") as log:
        log.secrets = source
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", ENGINE, "subscription"))
        assert asked == []
        log.append(MessageSent("m1", ANA, ALPHA, MessageKind.NOTE, "hello"))
        assert asked == [1]


def test_each_kind_of_note_and_summary_is_masked_by_its_field() -> None:
    token = FAKES["amazon session"]
    texts: tuple[tuple[Event, str], ...] = (
        (TaskNoted("alpha", "m2", "t1", "done", f"saw {token}"), "body"),
        (
            RoleNoted(
                "core",
                "e1",
                "alpha",
                model.RoleNoteKind.NOTE,
                f"x {token}",
                "c" * 40,
                "d" * 40,
                AT,
            ),
            "body",
        ),
        (RunEnded("r1", RunStatus.EXITED, 0, None, None, f"s {token}", AT), "note"),
    )
    for event, name in texts:
        found = scrub_event(event)
        assert getattr(found, name) == getattr(event, name).replace(token, MASK)
        assert token not in getattr(found, name)
