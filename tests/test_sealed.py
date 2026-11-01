"""Literal values of a line's configuration kept out of the event log.

Task of 2026-11-01. A literal value of an MCP server's ``env`` or ``headers``, or of
a settings document's ``env``, is sealed into the owner-only store beside the log, and
the event names its location and a keyed digest. The engine is built with the values
put back, a store that cannot give them refuses the build naming each location and
never a value, and a log written before sealing is reported and never migrated."""

import json
import os
import stat
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codec import DecodeError, decode, encode
from dromon.daemon import apply_project, report_unsealed
from dromon.driver import Strategy
from dromon.engine import EngineRefused
from dromon.eventlog import EventLog, read_entries
from dromon.lineconfig import (
    ConfigError,
    LineConfig,
    McpServer,
    encode_config,
    parse_servers,
)
from dromon.model import (
    InvalidField,
    LineConfigured,
    LineCreated,
    SealedValue,
)
from dromon.projectconfig import parse
from dromon.scheduler import LineEngine, LineSpec, Scheduler
from dromon.sealed import (
    SEALED_TEXT,
    SealedError,
    SealedStore,
    digest_of,
    literal_places,
    seal,
    store_path,
    unseal,
)
from dromon.settings import parse_settings

HAIKU = "claude-haiku-5-5"
ENV_VALUE = "s3cr3t-token-value"
HEADER = "Bearer h3ad3r-value"
LEVEL = "debug-literal"
DIGEST = "a" * 64


def literal_config() -> LineConfig:
    """A configuration with one literal of each sealed kind, and values that stay as
    written: a reference, a value that holds one, and an empty value."""
    return LineConfig(
        mcp_servers=(
            (
                "docs",
                McpServer(
                    "http",
                    url="https://docs.example/mcp",
                    headers=(("Authorization", HEADER), ("X-Ref", "Bearer ${REF}")),
                ),
            ),
            (
                "search",
                McpServer(
                    "stdio",
                    command="search-mcp",
                    env=(
                        ("FROM_ENV", "${SEARCH_TOKEN}"),
                        ("TOKEN", ENV_VALUE),
                    ),
                ),
            ),
        ),
        settings=parse_settings({"env": {"EMPTY": "", "LOG_LEVEL": LEVEL}}),
    )


SECRETS = (ENV_VALUE, HEADER, LEVEL)


class Built:
    """An engine nobody runs, which keeps the spec it was built with."""

    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id


class World:
    def __init__(self, path: Path) -> None:
        self.log = EventLog(path)
        self.built: list[LineSpec] = []
        self.scheduler = Scheduler(
            self.log, self.build, hold_wakes=True, wall_clock=self.wall
        )

    @staticmethod
    def wall() -> datetime:
        return datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)

    @staticmethod
    def build_nothing(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        raise AssertionError("no engine is built in this test")

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        self.built.append(spec)
        return cast(LineEngine, Built(spec))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """A world whose log is closed at the end of the test, so that its connection is
    never left to the garbage collector, which reports it as a ``ResourceWarning``
    in whichever later test collection happens to run."""
    target = World(tmp_path / "events.sqlite")
    yield target
    target.log.close()


def log_text(path: Path) -> str:
    """Every payload the log holds, as one text."""
    return "\n".join(encode(entry.event) for entry in read_entries(path))


# Sealing and unsealing.


def test_sealing_takes_each_literal_out_and_keeps_references() -> None:
    config = literal_config()
    places = [at for at, _ in literal_places(config)]
    assert places == [
        ("mcp_servers", "docs", "headers", "Authorization"),
        ("mcp_servers", "search", "env", "TOKEN"),
        ("settings", "env", "LOG_LEVEL"),
    ]
    sealing = seal(config)
    text = encode_config(sealing.config)
    for secret in SECRETS:
        assert secret not in text
    assert text.count(SEALED_TEXT) == len(places)
    assert "Bearer ${REF}" in text and "${SEARCH_TOKEN}" in text
    assert [value.at for value in sealing.sealed] == places
    for value in sealing.sealed:
        key, kept = sealing.values[value.digest]
        assert digest_of(bytes.fromhex(key), kept) == value.digest


def test_a_configuration_without_literals_is_unchanged() -> None:
    config = LineConfig(
        mcp_servers=(("search", McpServer("stdio", command="s", env=(("T", "${T}"),))),)
    )
    sealing = seal(config)
    assert sealing.config == config and sealing.sealed == () and not sealing.values


def test_a_configuration_copied_from_a_view_is_refused(world: World) -> None:
    shown = seal(literal_config()).config
    with pytest.raises(SealedError, match="give each value"):
        seal(shown)
    before = world.log.last_seq
    with pytest.raises(ConfigError, match="mcp_servers.search.env.TOKEN"):
        world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, shown)
    assert world.log.last_seq == before


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("tok3n${NOPE_VALUE-IN-ERR}", "malformed variable reference at character 6"),
        ("${DROMON_TAILTEXT}", "reserved variable, at character 1"),
    ],
)
@pytest.mark.parametrize("table", ["env", "headers"])
def test_a_refused_reference_names_its_place_and_no_part_of_the_value(
    value: str, kind: str, table: str
) -> None:
    """Audit's finding V1. A credential that holds a stray ``${`` is refused before
    sealing, so its error must keep the value out as sealing does."""
    server = (
        {"command": "s", "env": {"TOKEN": value}}
        if table == "env"
        else {"type": "http", "url": "https://x", "headers": {"TOKEN": value}}
    )
    with pytest.raises(ConfigError) as raised:
        parse_servers({"s": server})
    message = str(raised.value)
    assert f"server s {table} TOKEN" in message and kind in message
    pieces = {value[i : i + 3] for i in range(len(value) - 2)}
    assert [piece for piece in pieces if piece in message] == []


def test_equal_values_have_unrelated_digests() -> None:
    config = LineConfig(
        mcp_servers=(
            ("a", McpServer("stdio", command="a", env=(("T", ENV_VALUE),))),
            ("b", McpServer("stdio", command="b", env=(("T", ENV_VALUE),))),
        )
    )
    first, second = seal(config).sealed
    assert first.digest != second.digest


def test_unsealing_gives_back_the_configuration(tmp_path: Path) -> None:
    config = literal_config()
    sealing = seal(config)
    store = SealedStore(tmp_path / "store")
    store.add(sealing.values)
    assert unseal(sealing.config, sealing.sealed, store) == config


def test_a_missing_value_names_its_place_and_never_a_value(tmp_path: Path) -> None:
    sealing = seal(literal_config())
    store = SealedStore(tmp_path / "store")
    with pytest.raises(SealedError) as raised:
        unseal(sealing.config, sealing.sealed, store)
    message = str(raised.value)
    assert "mcp_servers.search.env.TOKEN" in message
    assert "settings.env.LOG_LEVEL" in message
    for secret in SECRETS:
        assert secret not in message


def test_a_value_changed_in_the_store_is_refused(tmp_path: Path) -> None:
    sealing = seal(literal_config())
    store = SealedStore(tmp_path / "store")
    changed = {d: (key, value + "x") for d, (key, value) in sealing.values.items()}
    store.add(changed)
    with pytest.raises(SealedError, match="no valid value"):
        unseal(sealing.config, sealing.sealed, store)


def test_a_place_the_configuration_does_not_hold_sealed_is_refused(
    tmp_path: Path,
) -> None:
    sealing = seal(literal_config())
    store = SealedStore(tmp_path / "store")
    store.add(sealing.values)
    stray = (*sealing.sealed, SealedValue(("settings", "env", "OTHER"), DIGEST))
    with pytest.raises(SealedError, match="no sealed value at settings.env.OTHER"):
        unseal(sealing.config, stray, store)
    with pytest.raises(SealedError, match="no sealed value"):
        unseal(literal_config(), sealing.sealed, store)


# The store.


def test_the_store_is_owner_only_and_pruned(tmp_path: Path) -> None:
    store = SealedStore(tmp_path / "store")
    assert store.read() == {}
    sealing = seal(literal_config())
    store.add(sealing.values)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.read() == dict(sealing.values)
    kept = sealing.sealed[0].digest
    store.retain({kept})
    assert set(store.read()) == {kept}
    assert [p.name for p in tmp_path.iterdir()] == ["store"]


def test_a_store_others_may_read_is_refused(tmp_path: Path) -> None:
    store = SealedStore(tmp_path / "store")
    store.add(seal(literal_config()).values)
    os.chmod(store.path, 0o640)
    with pytest.raises(SealedError, match="owner only"):
        store.read()


def test_a_link_in_the_store_s_place_is_not_followed(tmp_path: Path) -> None:
    """The link leads to a well-formed owner-only store, so only the refusal to
    follow it refuses it, which is the guard under test."""
    target = SealedStore(tmp_path / "elsewhere")
    target.add(seal(literal_config()).values)
    link = tmp_path / "store"
    link.symlink_to(target.path)
    with pytest.raises(SealedError, match="cannot open the sealed store"):
        SealedStore(link).read()


def test_a_directory_in_the_store_s_place_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "store"
    path.mkdir(mode=0o700)
    with pytest.raises(SealedError, match="not a regular file"):
        SealedStore(path).read()


def test_a_named_pipe_in_the_store_s_place_is_refused_without_blocking(
    tmp_path: Path,
) -> None:
    """A pipe with no writer would block an open that waits for one, so this test
    would hang rather than fail if the store opened it without ``O_NONBLOCK``."""
    path = tmp_path / "store"
    os.mkfifo(path, 0o600)
    with pytest.raises(SealedError, match="not a regular file"):
        SealedStore(path).read()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"v": 2, "values": {}}',
        '{"v": 1}',
        '{"v": 1, "values": []}',
        '{"v": 1, "values": {"d": "x"}}',
        '{"v": 1, "values": {"d": {"key": 1, "value": "x"}}}',
        '{"v": 1, "values": {"d": {"key": "k", "value": "x", "more": 1}}}',
    ],
)
def test_a_malformed_store_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "store"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(SealedError):
        SealedStore(path).read()


def test_a_read_only_store_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "store"
    writer = SealedStore(path)
    sealing = seal(literal_config())
    writer.add(sealing.values)
    before = path.read_bytes()
    reader = SealedStore(path, writable=False)
    reader.add(seal(literal_config()).values)
    reader.retain(())
    assert path.read_bytes() == before
    assert reader.read() == dict(sealing.values)


# The event and its encoding.


def test_the_event_round_trips_and_an_empty_seal_is_left_out() -> None:
    sealed = (SealedValue(("mcp_servers", "s", "env", "T"), DIGEST),)
    event = LineConfigured("alpha", "{}", sealed)
    assert decode(encode(event)) == event
    plain = LineConfigured("alpha", "{}")
    assert "sealed" not in json.loads(encode(plain))["data"]
    assert decode(encode(plain)) == plain


def test_an_empty_sealed_array_is_refused_on_decoding() -> None:
    wire = json.loads(encode(LineConfigured("alpha", "{}")))
    wire["data"]["sealed"] = []
    with pytest.raises(DecodeError, match="non-empty"):
        decode(json.dumps(wire))


@pytest.mark.parametrize(
    "at",
    [
        (),
        ("mcp_servers", "s", "env"),
        ("mcp_servers", "s", "args", "T"),
        ("settings", "hooks", "T"),
        ("sandbox", "env", "T"),
        ("mcp_servers", "", "env", "T"),
    ],
)
def test_a_sealed_value_stands_only_where_sealing_puts_one(
    at: tuple[str, ...],
) -> None:
    with pytest.raises(InvalidField):
        SealedValue(at, DIGEST)


def test_a_sealed_value_needs_a_digest() -> None:
    with pytest.raises(InvalidField, match="digest"):
        SealedValue(("settings", "env", "T"), "A" * 64)


def test_the_event_names_each_place_once_in_order() -> None:
    first = SealedValue(("settings", "env", "A"), DIGEST)
    second = SealedValue(("settings", "env", "B"), DIGEST)
    with pytest.raises(InvalidField, match="once, in order"):
        LineConfigured("alpha", "{}", (second, first))
    with pytest.raises(InvalidField, match="once, in order"):
        LineConfigured("alpha", "{}", (first, first))


# The scheduler and the daemon.


def test_a_configured_line_keeps_its_literals_out_of_the_log(world: World) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    text = log_text(world.log.path)
    for secret in SECRETS:
        assert secret not in text
    assert SEALED_TEXT in text
    line = world.log.state.lines["alpha"]
    assert len(line.sealed) == 3
    store = SealedStore(store_path(world.log.path))
    assert {value for _, value in store.read().values()} == set(SECRETS)


def test_the_engine_is_built_with_the_values_put_back(world: World) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    world.scheduler.engine_for("alpha")
    (spec,) = world.built
    assert spec.config == literal_config()


def test_a_lost_store_refuses_the_engine_naming_each_place(world: World) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    store_path(world.log.path).unlink()
    with pytest.raises(EngineRefused) as raised:
        world.scheduler.engine_for("alpha")
    message = str(raised.value)
    assert "mcp_servers.search.env.TOKEN" in message and "configure the line" in message
    for secret in SECRETS:
        assert secret not in message
    assert world.built == []


def test_a_store_that_cannot_be_written_refuses_the_configuration(
    tmp_path: Path,
) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            World.build_nothing,
            hold_wakes=True,
            wall_clock=World.wall,
            sealed_store=SealedStore(tmp_path / "missing" / "store"),
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        before = log.last_seq
        with pytest.raises(SealedError, match="cannot write the sealed store"):
            scheduler.configure_line("alpha", literal_config())
        assert log.last_seq == before


def test_a_new_configuration_prunes_the_values_it_no_longer_needs(
    world: World,
) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    world.scheduler.configure_line("alpha", LineConfig())
    assert SealedStore(store_path(world.log.path)).read() == {}
    assert world.log.state.lines["alpha"].sealed == ()


def test_a_line_configured_before_sealing_is_reported_and_left_alone(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.log.append(LineCreated("alpha", HAIKU, Strategy.SUBSCRIPTION.value))
    world.log.append(LineConfigured("alpha", encode_config(literal_config())))
    before = world.log.last_seq
    assert world.scheduler.unsealed_lines() == {"alpha": 3}
    assert world.scheduler.event_log_status()["unsealed_lines"] == {"alpha": 3}
    report_unsealed(world.scheduler)
    assert "line alpha's configuration holds 3 literal" in capsys.readouterr().err
    assert world.log.last_seq == before
    world.scheduler.engine_for("alpha")
    assert world.built[0].config == literal_config()


PROJECT = """\
version = 1

[lines.alpha]
model = "claude-haiku-5-5"

[lines.alpha.mcp_servers.search]
command = "search-mcp"
env = { TOKEN = "s3cr3t-token-value" }
"""


def test_a_start_does_not_configure_a_sealed_line_again(world: World) -> None:
    project = parse(PROJECT)
    apply_project(world.scheduler, world.log, project)
    assert ENV_VALUE not in log_text(world.log.path)
    after = world.log.last_seq
    apply_project(world.scheduler, world.log, project)
    assert world.log.last_seq == after


def test_a_start_whose_store_is_lost_seals_the_project_values_again(
    world: World,
) -> None:
    project = parse(PROJECT)
    apply_project(world.scheduler, world.log, project)
    store_path(world.log.path).unlink()
    after = world.log.last_seq
    apply_project(world.scheduler, world.log, project)
    assert world.log.last_seq == after + 1
    world.scheduler.engine_for("alpha")
    assert world.built[0].config == project.lines["alpha"].config
    assert ENV_VALUE not in log_text(world.log.path)


def test_replay_needs_no_store(world: World) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    store_path(world.log.path).unlink()
    world.log.close()
    with EventLog(world.log.path) as reopened:
        line = reopened.state.lines["alpha"]
        assert len(line.sealed) == 3 and line.config is not None
        assert ENV_VALUE not in line.config


def test_the_effective_configuration_holds_no_value(world: World) -> None:
    world.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, literal_config())
    shown = encode_config(world.scheduler.effective_config("alpha"))
    for secret in SECRETS:
        assert secret not in shown
    assert world.scheduler.effective_config("alpha") != literal_config()
