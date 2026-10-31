"""The operators' relay wired into the daemon, the file sink, and issues from lines.
Round 11, items 1 to 3.

The relayer is driven one ``step`` at a time with a fake clock, so each test states
which pass forwarded what. The command relay runs real short commands, since its
timeout, its process group and the event loop staying free are the behaviours under
test."""

import asyncio
import json
import os
import stat
import sys
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.cli import _relay_rows  # pyright: ignore[reportPrivateUsage]
from dromon.codec import decode, encode
from dromon.configview import Overrides
from dromon.daemon import Daemon, ProjectMismatch, protected_dirs
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.mailbox import listing
from dromon.model import (
    EventRejected,
    LineAddress,
    MessageKind,
    MessageSent,
    OperatorAddress,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ConfigFileError, ProjectConfig, config_path, parse
from dromon.relay import (
    FIRST_BACKOFF_SECONDS,
    SWEEP_SECONDS,
    AsyncRelay,
    CommandRelay,
    FileRelay,
    Notice,
    OperatorRelayer,
    RelayConfig,
    RelayDefinition,
    RelayError,
    RelayKind,
    encode_notice,
    sink_refusal,
)
from dromon.scheduler import (
    AGENT_SEND_TOOL_SCHEMA,
    SEND_TOOL_SCHEMA,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """An asynchronous relay that keeps what it accepts, or refuses while told to."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.notices: list[Notice] = []
        self.refuse = refuse

    async def deliver(self, notice: Notice) -> None:
        if self.refuse:
            raise RelayError("channel unavailable")
        self.notices.append(notice)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.ensure_operator("ana")
        found.ensure_operator("ben")
        yield found


def relayer_for(
    scheduler: Scheduler, routes: dict[str, AsyncRelay], clock: Clock | None = None
) -> OperatorRelayer:
    relayer = OperatorRelayer(scheduler, routes, clock or Clock())
    scheduler.on_operator_mail = relayer.notify
    return relayer


def call(tool: SdkMcpTool[Any], args: dict[str, object]) -> tuple[str, bool]:
    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


# Item 1. Forwarding through the relayer.


def test_a_relayed_message_is_delivered_and_stays_unread(
    scheduler: Scheduler,
) -> None:
    """Fact. Relay delivery is delivery, not reading, so ``dromon mail --unread``
    still lists the message until the operator reads or acknowledges it."""
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    question = scheduler.ask("alpha", ANA, "Merge now?", ("yes", "no"))
    relayer.ready.clear()
    note = scheduler.send(LineAddress("alpha"), ANA, "done")
    assert relayer.ready.is_set(), "recording mail to an operator wakes the relayer"
    asyncio.run(relayer.step())
    assert [n.message_id for n in ana.notices] == [question, note]
    messages = scheduler.state.messages
    assert messages[question].delivered and not messages[question].read
    unread, total = listing(scheduler.state, ANA, unread_only=True)
    assert total == 2 and [m for m, _ in unread] == [question, note]
    assert relayer.report()["delivered"] == 2
    asyncio.run(relayer.step())
    assert len(ana.notices) == 2, "a delivered message is not relayed again"


def test_a_failing_relay_keeps_its_notice_and_backs_off(scheduler: Scheduler) -> None:
    clock = Clock()
    ana = Recorder(refuse=True)
    relayer = relayer_for(scheduler, {"ana": ana}, clock)
    message_id = scheduler.send(LineAddress("alpha"), ANA, "done")
    wait = asyncio.run(relayer.step())
    assert wait == FIRST_BACKOFF_SECONDS
    assert not scheduler.state.messages[message_id].delivered
    report = relayer.report()
    assert report["failing"] == 1
    assert report["failures"] == [
        {
            "message_id": message_id,
            "operator": "ana",
            "attempts": 1,
            "reason": "channel unavailable",
            "retry_in_seconds": FIRST_BACKOFF_SECONDS,
        }
    ]
    ana.refuse = False
    asyncio.run(relayer.step())
    assert ana.notices == [], "a notice in its backoff is not tried again"
    clock.now += FIRST_BACKOFF_SECONDS
    assert asyncio.run(relayer.step()) == SWEEP_SECONDS
    assert [n.message_id for n in ana.notices] == [message_id]
    assert scheduler.state.messages[message_id].delivered
    assert relayer.report()["failing"] == 0


def test_the_backoff_doubles_up_to_its_bound(scheduler: Scheduler) -> None:
    clock = Clock()
    relayer = relayer_for(scheduler, {"ana": Recorder(refuse=True)}, clock)
    scheduler.send(LineAddress("alpha"), ANA, "done")
    retries: list[object] = []
    waits: list[float] = []
    for _ in range(9):
        waits.append(asyncio.run(relayer.step()))
        failures = cast(list[dict[str, object]], relayer.report()["failures"])
        retries.append(failures[0]["retry_in_seconds"])
        clock.now += 1000
    assert retries == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
    assert waits == [5.0, 10.0, 20.0, 40.0, *[SWEEP_SECONDS] * 5], (
        "a pass waits for the backoff or the sweep, whichever ends first"
    )


def test_a_defect_in_a_relay_fails_its_notice_not_the_relayer(
    scheduler: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken:
        async def deliver(self, notice: Notice) -> None:
            raise KeyError("bug")

    good = Recorder()
    relayer = relayer_for(scheduler, {"ana": Broken(), "ben": good})
    first = scheduler.send(LineAddress("alpha"), ANA, "for ana")
    second = scheduler.send(LineAddress("alpha"), BEN, "for ben")
    asyncio.run(relayer.step())
    assert not scheduler.state.messages[first].delivered
    assert [n.message_id for n in good.notices] == [second]
    assert "the relay failed unexpectedly" in str(relayer.report()["failures"])
    assert "KeyError" in capsys.readouterr().err


def test_mail_for_an_unrouted_operator_waits_for_a_client(
    scheduler: Scheduler,
) -> None:
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    for_ben = scheduler.send(LineAddress("alpha"), BEN, "for ben")
    asyncio.run(relayer.step())
    assert ana.notices == []
    assert not scheduler.state.messages[for_ben].delivered
    assert relayer.report()["unrouted"] == 1
    scheduler.collect(BEN)
    asyncio.run(relayer.step())
    assert relayer.report()["unrouted"] == 0


def test_a_client_delivery_during_the_relay_is_recorded_once(
    scheduler: Scheduler,
) -> None:
    """A client listing the mailbox while the relay runs delivers the message
    first, and the relay's acceptance then records nothing more, since the log
    refuses a second ``message_delivered``."""

    class Racing:
        def __init__(self) -> None:
            self.notices: list[str] = []

        async def deliver(self, notice: Notice) -> None:
            self.notices.append(notice.message_id)
            scheduler.collect(ANA)

    racing = Racing()
    relayer = relayer_for(scheduler, {"ana": racing})
    first = scheduler.send(LineAddress("alpha"), ANA, "one")
    second = scheduler.send(LineAddress("alpha"), ANA, "two")
    asyncio.run(relayer.step())
    assert racing.notices == [first], "the second was delivered before its turn"
    assert scheduler.state.messages[second].delivered
    assert relayer.report()["delivered"] == 0
    assert scheduler.relayed(first) is False


def test_a_stop_between_acceptance_and_the_record_relays_again(
    tmp_path: Path,
) -> None:
    """At least once. The relay accepts and the relayer is stopped before it
    records the delivery, as a crash would stop it. After a restart the same
    message identifier is relayed again, which lets a reader see the repeat."""
    accepted = asyncio.Event()
    seen: list[str] = []

    class Accepting:
        async def deliver(self, notice: Notice) -> None:
            seen.append(notice.message_id)
            accepted.set()
            await asyncio.Event().wait()

    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        first = Scheduler(log, no_engine, hold_wakes=True)
        first.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        first.ensure_operator("ana")
        message_id = first.send(LineAddress("alpha"), ANA, "done")
        relayer = relayer_for(first, {"ana": Accepting()})

        async def crash() -> None:
            task = asyncio.create_task(relayer.step())
            await accepted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(crash())
        assert not log.state.messages[message_id].delivered
    with EventLog(path) as log:
        second = Scheduler(log, no_engine, hold_wakes=True)
        again = Recorder()
        asyncio.run(relayer_for(second, {"ana": again}).step())
        assert seen == [message_id]
        assert [n.message_id for n in again.notices] == [message_id]
        assert log.state.messages[message_id].delivered


def test_new_routes_apply_from_the_next_pass(scheduler: Scheduler) -> None:
    clock = Clock()
    old, new = Recorder(refuse=True), Recorder()
    relayer = relayer_for(scheduler, {"ana": old}, clock)
    message_id = scheduler.send(LineAddress("alpha"), ANA, "done")
    asyncio.run(relayer.step())
    relayer.ready.clear()
    relayer.set_routes({"ana": new})
    assert relayer.ready.is_set()
    asyncio.run(relayer.step())
    assert [n.message_id for n in new.notices] == [message_id], (
        "a changed route is tried at once, without waiting out the old backoff"
    )


def test_the_relayer_survives_a_failing_pass_and_stops_when_told() -> None:
    class Failing:
        @property
        def state(self) -> State:
            raise RuntimeError("the log is gone")

        def relayed(self, message_id: str) -> bool:
            return False

    async def scenario() -> None:
        stop = asyncio.Event()
        relayer = OperatorRelayer(Failing(), {}, Clock())
        task = asyncio.create_task(relayer.run(stop))
        await asyncio.sleep(0.05)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


# The command relay off the event loop.


def notice(message_id: str = "m-1", kind: MessageKind = MessageKind.NOTE) -> Notice:
    return Notice(message_id, "alpha", "ana", kind, "body", (), None)


def test_the_command_relay_delivers_without_blocking_the_loop(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    script = (
        "import sys,time; time.sleep(0.3);"
        f" open({str(out)!r},'w').write(sys.stdin.read())"
    )
    relay = CommandRelay([sys.executable, "-c", script], 5)

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        counting = asyncio.create_task(ticker())
        await relay.deliver(notice())
        counting.cancel()
        return ticks

    assert asyncio.run(scenario()) >= 10, "the loop ran while the command did"
    assert json.loads(out.read_text()) == json.loads(encode_notice(notice()))


def test_a_hung_command_times_out_and_its_group_is_killed(tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    script = (
        "import os,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"open({str(child)!r},'w').write(str(p.pid)); time.sleep(60)"
    )
    relay = CommandRelay([sys.executable, "-c", script], 1.0)
    started = time.monotonic()
    with pytest.raises(RelayError, match="timed out after 1.0s"):
        asyncio.run(relay.deliver(notice()))
    assert time.monotonic() - started < 10
    pid = int(child.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child outlived the timeout")


def test_a_failing_command_reports_its_status_and_bounded_detail() -> None:
    script = "import sys; sys.stderr.write('x' * 100000); sys.exit(3)"
    relay = CommandRelay([sys.executable, "-c", script], 5)
    with pytest.raises(RelayError, match="exited with status 3") as caught:
        asyncio.run(relay.deliver(notice()))
    assert len(str(caught.value)) < 300


def test_a_missing_command_cannot_start(tmp_path: Path) -> None:
    relay = CommandRelay([str(tmp_path / "absent")], 5)
    with pytest.raises(RelayError, match="could not start"):
        asyncio.run(relay.deliver(notice()))


# Item 2. The file sink.


def test_the_file_sink_appends_one_line_per_notice_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "notices.jsonl"
    relay = FileRelay(path, list, ())
    asyncio.run(relay.deliver(notice("m-1")))
    asyncio.run(relay.deliver(notice("m-2", MessageKind.ISSUE)))
    lines = path.read_text().splitlines()
    assert [json.loads(line)["message_id"] for line in lines] == ["m-1", "m-2"]
    assert json.loads(lines[1])["kind"] == "issue"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_file_sink_refuses_a_link_and_an_open_file(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    target.chmod(0o600)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(RelayError, match="could not be opened"):
        FileRelay(link, list, ()).forward(notice())
    shared = tmp_path / "shared.jsonl"
    shared.write_text("")
    shared.chmod(0o644)
    with pytest.raises(RelayError, match="open to group or others"):
        FileRelay(shared, list, ()).forward(notice())
    assert shared.read_text() == "" and target.read_text() == ""


def test_the_file_sink_is_never_in_a_lines_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    (workdir / "deep").mkdir(parents=True)
    protected = workdir / ".dromon"
    protected.mkdir()
    inside = workdir / "deep" / "notices.jsonl"
    assert "lies in the working directory" in str(
        sink_refusal(inside, [workdir], [protected])
    )
    assert sink_refusal(protected / "notices.jsonl", [workdir], [protected]) is None
    assert sink_refusal(tmp_path / "notices.jsonl", [workdir], [protected]) is None
    assert "does not exist" in str(
        sink_refusal(tmp_path / "absent" / "n.jsonl", [], [])
    )
    alias = tmp_path / "alias"
    alias.symlink_to(workdir)
    assert sink_refusal(alias / "notices.jsonl", [workdir], []) is not None
    moving: list[Path] = []
    relay = FileRelay(tmp_path / "notices.jsonl", lambda: moving, [])
    relay.forward(notice("m-1"))
    moving.append(tmp_path)
    with pytest.raises(RelayError, match="lies in the working directory"):
        relay.forward(notice("m-2"))
    assert len((tmp_path / "notices.jsonl").read_text().splitlines()) == 1


# The project file.


def test_the_relays_and_operators_tables_parse() -> None:
    config = parse(
        "version = 1\n"
        "[relays.mail]\ncommand = ['/usr/local/bin/notify', '--to', 'ana']\n"
        "timeout_seconds = 10\n"
        "[relays.tail]\nkind = 'file'\npath = '.dromon/run/notices.jsonl'\n"
        "[operators.ana]\nrelay = 'tail'\n[operators.ben]\nrelay = 'mail'\n"
    )
    assert config.relays == RelayConfig(
        {
            "mail": RelayDefinition(
                RelayKind.COMMAND,
                command=("/usr/local/bin/notify", "--to", "ana"),
                timeout_seconds=10.0,
            ),
            "tail": RelayDefinition(
                RelayKind.FILE, path=".dromon/run/notices.jsonl", timeout_seconds=30.0
            ),
        },
        {"ana": "tail", "ben": "mail"},
    )
    assert parse("version = 1\n").relays == RelayConfig()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[relays.a]\ncommand = []\n", "relays.a.command: must be a list of 1 to 64"),
        ("[relays.a]\ncommand = ['']\n", "non-empty text arguments"),
        ("[relays.a]\ncommand = 'notify'\n", "must be a list"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = 0\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = 3601\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = true\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\npath = 'x'\n", "is for a file relay"),
        ("[relays.a]\nkind = 'file'\n", "relays.a.path: must be a path"),
        ("[relays.a]\nkind = 'file'\npath = 'x'\ncommand = ['n']\n", "command relay"),
        ("[relays.a]\nkind = 'mail'\n", "must be command or file"),
        ("[relays.a]\ncommand = ['n']\nshell = true\n", "relays.a.shell: is not"),
        ("[relays.A]\ncommand = ['n']\n", "is not an identifier"),
        ("[operators.ana]\nrelay = 'none'\n", "operators.ana.relay: must name a relay"),
        (
            "[relays.a]\ncommand = ['n']\n[operators.ana]\nrelay = 'a'\nvia = 'x'\n",
            "operators.ana.via: is not a known key",
        ),
        ("relays = 3\n", "relays: must be a table"),
    ],
)
def test_a_bad_relays_table_is_refused(text: str, message: str) -> None:
    with pytest.raises(
        ConfigFileError, match=message.replace("(", r"\(").replace(")", r"\)")
    ):
        parse(f"version = 1\n{text}")


# The daemon: start, reload and status.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def file_relay_project(path: str) -> str:
    return (
        "version = 1\n"
        f"[relays.tail]\nkind = 'file'\npath = '{path}'\n"
        "[operators.ana]\nrelay = 'tail'\n"
    )


def make_daemon(paths: ProjectPaths, log: EventLog, config: ProjectConfig) -> Daemon:
    scheduler = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        root=paths.root,
        protected=protected_dirs(paths),
    )
    return Daemon(
        paths,
        log,
        "2026-10-31T00:00:00Z",
        scheduler,
        project=config,
        command_line=Overrides(),
        project_file_present=True,
    )


def ask(daemon: Daemon, op: str, args: dict[str, object]) -> dict[str, object]:
    line = {"id": "r", "op": op, "v": 1, "args": args}
    response = daemon.respond((json.dumps(line) + "\n").encode())
    assert response.ok and response.result is not None, response.error_message
    return response.result


def refusal(daemon: Daemon, op: str, args: dict[str, object]) -> str:
    line = {"id": "r", "op": op, "v": 1, "args": args}
    response = daemon.respond((json.dumps(line) + "\n").encode())
    assert not response.ok
    return str(response.error_message)


def test_a_reload_moves_the_route_and_status_shows_the_relays(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-sink"
    outside.mkdir()
    first, second = outside / "first.jsonl", outside / "second.jsonl"
    file = config_path(paths.state_dir)
    file.write_text(file_relay_project(str(first)))
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, parse(file.read_text(), paths.root))
        relayer = daemon.operator_relayer()
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon.scheduler.ensure_operator("ana")
        one = daemon.scheduler.send(LineAddress("alpha"), ANA, "one")
        asyncio.run(relayer.step())
        file.write_text(file_relay_project(str(second)))
        ask(daemon, "reload_project", {})
        two = daemon.scheduler.send(LineAddress("alpha"), ANA, "two")
        asyncio.run(relayer.step())
        status = cast(dict[str, object], ask(daemon, "status", {})["relays"])
    assert [json.loads(x)["message_id"] for x in first.read_text().splitlines()] == [
        one
    ]
    assert [json.loads(x)["message_id"] for x in second.read_text().splitlines()] == [
        two
    ]
    assert status["routed"] == ["ana"] and status["delivered"] == 2
    assert _relay_rows(status) == [
        "relays for @ana, 2 relayed since the start, 0 waiting unrouted, 0 failing"
    ]


def test_a_sink_in_a_lines_workdir_is_refused_at_start_and_reload(
    paths: ProjectPaths,
) -> None:
    file = config_path(paths.state_dir)
    file.write_text(file_relay_project("notices.jsonl"))
    refused = parse(file.read_text(), paths.root)
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        with pytest.raises(ProjectMismatch, match="relays.tail.path"):
            daemon.relay_routes(refused)
        daemon.operator_relayer()
        assert "lies in the working directory" in refusal(daemon, "reload_project", {})
        assert daemon.project == ProjectConfig(), "a refused reload changes nothing"
        file.write_text(file_relay_project(".dromon/run/notices.jsonl"))
        (paths.state_dir / "run").mkdir(mode=0o700, exist_ok=True)
        ask(daemon, "reload_project", {})


def test_a_file_line_moving_onto_the_sink_is_refused_too(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    """A line the reloaded file declares counts, though the log does not hold it
    yet, so a file cannot bring a line and a notice file into one directory."""
    sink = tmp_path / "share"
    sink.mkdir()
    config = parse(
        file_relay_project("share/notices.jsonl")
        + f"[lines.beta]\nmodel = '{HAIKU}'\nworkdir = 'share'\n",
        paths.root,
    )
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        with pytest.raises(ProjectMismatch, match="lies in the working directory"):
            daemon.relay_routes(config)


def test_a_line_configured_onto_the_sink_stops_its_writes(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-moved"
    outside.mkdir()
    file = config_path(paths.state_dir)
    file.write_text(file_relay_project(str(outside / "notices.jsonl")))
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, parse(file.read_text(), paths.root))
        relayer = daemon.operator_relayer()
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon.scheduler.ensure_operator("ana")
        daemon.scheduler.configure_line("alpha", LineConfig(workdir=str(outside)))
        message_id = daemon.scheduler.send(LineAddress("alpha"), ANA, "one")
        asyncio.run(relayer.step())
        assert not log.state.messages[message_id].delivered
        assert "lies in the working directory" in str(relayer.report()["failures"])


# Item 3. Issues from lines.


def test_a_line_raises_an_issue_and_the_operator_lists_and_closes_it(
    scheduler: Scheduler,
) -> None:
    text, error = call(
        scheduler.send_tool("alpha"),
        {"to": "@ana", "body": "list_mail hides notes", "issue": True},
    )
    assert not error
    issue_id = text.removeprefix("raised as issue ")
    other = scheduler.send(LineAddress("alpha"), ANA, "a plain note")
    assert scheduler.state.messages[issue_id].kind is MessageKind.ISSUE
    scheduler.collect(ANA)
    found, total = listing(scheduler.state, ANA, issues_only=True)
    assert (total, [m for m, _ in found]) == (1, [issue_id])
    scheduler.acknowledge(ANA, issue_id)
    assert listing(scheduler.state, ANA, issues_only=True)[1] == 0
    second = scheduler.send(LineAddress("alpha"), ANA, "x", issue=True)
    scheduler.collect(ANA)
    scheduler.send(ANA, LineAddress("alpha"), "fixed", reply_to=second)
    assert listing(scheduler.state, ANA, issues_only=True)[1] == 0, (
        "a reply closes an issue too"
    )
    assert scheduler.state.messages[other].kind is MessageKind.NOTE


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"to": "alpha", "body": "x", "issue": True}, "only a line raises an issue"),
        ({"to": "@ana", "body": "x", "issue": "yes"}, "issue must be true or false"),
    ],
)
def test_an_issue_goes_only_to_an_operator(
    scheduler: Scheduler, args: dict[str, object], message: str
) -> None:
    text, error = call(scheduler.send_tool("alpha"), args)
    assert error and message in text


def test_an_issue_is_never_a_reply_and_only_a_line_raises_one(
    scheduler: Scheduler,
) -> None:
    note = scheduler.send(ANA, LineAddress("alpha"), "hello")
    with pytest.raises(EventRejected, match="opens a thread"):
        scheduler.state.check(
            MessageSent(
                "m-x", LineAddress("alpha"), ANA, MessageKind.ISSUE, "x", reply_to=note
            )
        )
    with pytest.raises(EventRejected, match="only a line raises an issue"):
        scheduler.send(ANA, LineAddress("alpha"), "x", issue=True)


def test_an_agent_is_not_offered_the_issue_flag() -> None:
    line_properties = cast(dict[str, object], SEND_TOOL_SCHEMA["properties"])
    agent_properties = cast(dict[str, object], AGENT_SEND_TOOL_SCHEMA["properties"])
    assert "issue" in line_properties and "issue" not in agent_properties
    assert set(agent_properties) == set(line_properties) - {"issue"} | {"correlation"}


def test_an_issue_survives_the_codec_and_reaches_the_relay(
    scheduler: Scheduler,
) -> None:
    event = MessageSent("m-1", LineAddress("alpha"), ANA, MessageKind.ISSUE, "x")
    assert decode(encode(event)) == event
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    scheduler.send(LineAddress("alpha"), ANA, "a tool misleads", issue=True)
    asyncio.run(relayer.step())
    assert [n.kind for n in ana.notices] == [MessageKind.ISSUE]


def test_the_mail_query_lists_issues(paths: ProjectPaths) -> None:
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon.scheduler.ensure_operator("ana")
        issue = daemon.scheduler.send(LineAddress("alpha"), ANA, "x", issue=True)
        daemon.scheduler.send(LineAddress("alpha"), ANA, "y")
        result = ask(daemon, "mail", {"operator": "ana", "issues_only": True})
        assert result["total"] == 1
        entries = cast(list[dict[str, object]], result["messages"])
        assert [(e["message_id"], e["kind"]) for e in entries] == [(issue, "issue")]
        assert "booleans" in refusal(
            daemon, "mail", {"operator": "ana", "issues_only": 1}
        )


def test_status_names_each_failing_notice_and_is_quiet_without_relays() -> None:
    assert _relay_rows({"routed": [], "unrouted": 0}) == []
    rows = _relay_rows(
        {
            "routed": ["ana"],
            "delivered": 0,
            "unrouted": 2,
            "failing": 1,
            "failures": [
                {
                    "message_id": "m-1",
                    "operator": "ana",
                    "attempts": 3,
                    "reason": "the relay command timed out after 30.0s",
                    "retry_in_seconds": 20.0,
                }
            ],
        }
    )
    assert rows == [
        "relays for @ana, 0 relayed since the start, 2 waiting unrouted, 1 failing",
        "  m-1 for @ana, attempt 3, again in 20.0s:"
        " the relay command timed out after 30.0s",
    ]
