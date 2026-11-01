"""The fleet, round 13. ``docs/architecture/FLEET.md`` is normative.

The tests stand in for the process table and for every daemon's socket, since a
sandbox denies ``ps`` and Unix sockets. Every request a fleet command makes is
recorded with the socket it was sent to, so that a test can show no operation reaches
a daemon except through the socket its own index entry names.
"""

import inspect
import math
import os
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from dromon import cli, exits, fleet, grok, registry
from dromon import daemon as daemon_module
from dromon.codex import server_arguments
from dromon.lineconfig import LineConfig
from dromon.daemon import fleet_registration, make_index, protected_dirs
from dromon.engine import EngineRefused
from dromon.federation import NO_FEDERATION
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import ErrorCode, Operation, Response

STARTED = "Sat Oct 31 00:00:00 2026"
NO_PROCESS: dict[int, str] = {}
"""A process table that lists no process."""
LATER = "Sat Oct 31 01:00:00 2026"
COMMIT = "f5f8da12a726e441853c244a683276ca26ef5428"


def index() -> Path:
    return fleet.configured_index(os.environ)


def entry_for(root: Path, pid: int, name: str | None = None) -> fleet.IndexEntry:
    return fleet.IndexEntry(
        name or root.name,
        root,
        root / ".dromon" / "run" / "dromond.sock",
        pid,
        STARTED,
        COMMIT,
    )


type Handler = Callable[[Operation, dict[str, object]], Response]


@dataclass
class Fleet:
    """Daemons by socket, each answering from a handler, and a process table."""

    table: dict[int, str] = field(default_factory=dict[int, str])
    handlers: dict[Path, Handler] = field(default_factory=dict[Path, Handler])
    calls: list[tuple[Path, Operation, dict[str, object]]] = field(
        default_factory=list[tuple[Path, Operation, dict[str, object]]]
    )
    timeouts: list[float] = field(default_factory=list[float])

    def add(
        self, root: Path, pid: int, *, live: bool = True, answers: bool = True
    ) -> fleet.IndexEntry:
        root.mkdir(parents=True, exist_ok=True)
        entry = entry_for(root, pid)
        fleet.write_index_entry(index(), entry)
        if live:
            self.table[pid] = STARTED
        if answers:
            self.handlers[entry.socket] = daemon(entry)
        return entry

    def start_of(self, pid: int) -> str | None:
        return self.table.get(pid)

    def call(
        self,
        socket_path: Path,
        operation: Operation,
        timeout_seconds: float = 5.0,
        args: dict[str, object] | None = None,
    ) -> Response:
        self.timeouts.append(timeout_seconds)
        given = dict(args or {})
        self.calls.append((socket_path, operation, given))
        handler = self.handlers.get(socket_path)
        if handler is None:
            raise cli.DaemonNotRunning(f"no daemon listens on {socket_path}")
        return handler(operation, given)

    def sockets(self) -> set[Path]:
        return {socket for socket, _, _ in self.calls}


def daemon(entry: fleet.IndexEntry, *, pid: int | None = None) -> Handler:
    """A daemon that says ``pid``, by default the entry's, and answers each fleet
    operation with something of its own project."""

    def handle(operation: Operation, args: dict[str, object]) -> Response:
        match operation:
            case Operation.STATUS:
                return Response(
                    "r",
                    True,
                    result={
                        "pid": entry.pid if pid is None else pid,
                        "project": str(entry.root),
                        "daemon": {"commit": COMMIT, "stale": entry.name == "tide"},
                        "event_log": {"lines": 2, "open_questions": 1},
                        "lines": [],
                    },
                )
            case Operation.MAIL:
                return Response(
                    "r",
                    True,
                    result={
                        "total": 1,
                        "unread": 3,
                        "messages": [
                            {
                                "message_id": f"m-{entry.name}",
                                "sender": "core",
                                "kind": "issue",
                                "read": False,
                            }
                        ],
                    },
                )
            case Operation.QUESTIONS:
                return Response(
                    "r",
                    True,
                    result={
                        "questions": [
                            {
                                "question_id": f"q-{entry.name}",
                                "line": "core",
                                "operator": "ana",
                                "text": "may I?",
                            }
                        ]
                    },
                )
            case _:
                return Response("r", True, result={"message_id": "m-new", **args})

    return handle


@pytest.fixture
def two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry]:
    found = Fleet()
    harbor = found.add(tmp_path / "harbor", 101)
    tide = found.add(tmp_path / "tide", 102)
    monkeypatch.setattr(cli, "call", found.call)
    monkeypatch.setattr(cli, "fleet_start_of", found.start_of)
    return found, harbor, tide


# The index.


def test_tests_never_touch_the_real_index(tmp_path: Path) -> None:
    """The guard of ``conftest``. The index lies in the registry each test is given."""
    used = index()
    assert used != fleet.index_directory(registry.default_directory())
    assert used.is_relative_to(tmp_path.parent)
    hosts = fleet.configured_hosts_file(os.environ)
    assert hosts != fleet.default_hosts_file()
    assert hosts.is_relative_to(tmp_path.parent)


def test_two_daemons_are_listed_and_read_back(tmp_path: Path) -> None:
    harbor = entry_for(tmp_path / "harbor", 101)
    tide = entry_for(tmp_path / "tide", 102)
    for entry in (tide, harbor):
        fleet.write_index_entry(index(), entry)
    assert fleet.read_index(index()) == [harbor, tide]
    assert stat.S_IMODE(index().lstat().st_mode) == registry.DIRECTORY_MODE
    for path in index().glob("*.json"):
        assert stat.S_IMODE(path.lstat().st_mode) == registry.ENTRY_MODE


def test_an_index_not_yet_made_is_empty() -> None:
    assert not os.path.lexists(index())
    assert fleet.read_index(index()) == []


def test_a_restarted_daemon_replaces_its_entry_and_removes_only_its_own(
    tmp_path: Path,
) -> None:
    first = entry_for(tmp_path / "harbor", 101)
    second = entry_for(tmp_path / "harbor", 202)
    fleet.write_index_entry(index(), first)
    fleet.write_index_entry(index(), second)
    assert fleet.read_index(index()) == [second]
    assert not fleet.remove_index_entry(index(), first)
    assert fleet.read_index(index()) == [second]
    assert fleet.remove_index_entry(index(), second)
    assert fleet.read_index(index()) == []


def test_a_malformed_or_misplaced_entry_is_ignored(tmp_path: Path) -> None:
    good = entry_for(tmp_path / "harbor", 101)
    fleet.write_index_entry(index(), good)
    registry.write_private(index() / "junk.json", b"{not json")
    moved = index() / f"{'0' * 32}.json"
    registry.write_private(moved, good.text().encode())
    wide = index() / f"{fleet.entry_key(tmp_path / 'wide')}.json"
    wide.write_text(entry_for(tmp_path / "wide", 7).text())
    wide.chmod(0o644)
    assert fleet.read_index(index()) == [good]


def test_a_wide_index_is_refused(tmp_path: Path) -> None:
    fleet.write_index_entry(index(), entry_for(tmp_path / "harbor", 101))
    index().chmod(0o755)
    with pytest.raises(fleet.FleetError, match="mode 755"):
        fleet.read_index(index())


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"name": ""}, "1 to 255 bytes"),
        ({"name": "a/b"}, "slash"),
        ({"name": "a\nb"}, "control"),
        ({"root": Path("relative")}, "absolute"),
        ({"socket": Path("/run/peers.sock")}, "dromond.sock"),
        ({"pid": 0}, "positive"),
        ({"commit": "not hex"}, "hexadecimal"),
        ({"name": "tide"}, "not the last step of the root"),
    ],
)
def test_an_entry_is_checked(changes: dict[str, object], reason: str) -> None:
    fields: dict[str, object] = {
        "name": "harbor",
        "root": Path("/srv/harbor"),
        "socket": Path("/srv/harbor/.dromon/run/dromond.sock"),
        "pid": 1,
        "started": STARTED,
        "commit": None,
    }
    fields.update(changes)
    with pytest.raises(fleet.FleetError, match=reason):
        fleet.IndexEntry(**fields)  # pyright: ignore[reportArgumentType]


def test_liveness_reads_the_start_time_and_never_trusts_an_unreadable_table(
    tmp_path: Path,
) -> None:
    entry = entry_for(tmp_path / "harbor", 101)

    def unreadable(_pid: int) -> str | None:
        raise RuntimeError("ps is denied")

    assert fleet.liveness(entry, {101: STARTED}.get) is fleet.Liveness.LIVE
    assert fleet.liveness(entry, {101: LATER}.get) is fleet.Liveness.STALE
    assert fleet.liveness(entry, NO_PROCESS.get) is fleet.Liveness.STALE
    assert fleet.liveness(entry, unreadable) is fleet.Liveness.UNKNOWN


# The daemon's side.


def harbor_paths(short_runtime: Path) -> ProjectPaths:
    """A project beside the short runtime directory, so its socket path stays under
    the limit wherever the temporary directory is, as a macOS one outside a sandbox
    is not."""
    root = short_runtime.parent / "harbor"
    root.mkdir()
    return project_paths(root, short_runtime)


def test_a_daemon_registers_whatever_its_federation_mode(short_runtime: Path) -> None:
    paths = harbor_paths(short_runtime)
    entry = fleet_registration(
        paths,
        registry.configured_directory(os.environ),
        {os.getpid(): STARTED}.get,
        None,
    )
    assert entry is not None
    assert entry.socket == paths.socket and entry.pid == os.getpid()
    assert fleet.read_index(index()) == [entry]


def test_a_daemon_that_cannot_be_listed_starts_unlisted(
    short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = harbor_paths(short_runtime)

    def unreadable(_pid: int) -> str | None:
        raise RuntimeError("ps is denied")

    directory = registry.configured_directory(os.environ)
    assert fleet_registration(paths, directory, unreadable, None) is None
    assert fleet_registration(paths, directory, NO_PROCESS.get, None) is None
    assert "not listed in the fleet index" in capsys.readouterr().err
    assert fleet.read_index(index()) == []


def test_every_line_is_denied_the_index_whatever_the_mode(
    short_runtime: Path,
) -> None:
    paths = harbor_paths(short_runtime)
    denied = protected_dirs(paths, NO_FEDERATION)
    assert Path(os.path.realpath(index())) in denied
    default = fleet.index_directory(registry.default_directory())
    assert Path(os.path.realpath(default)) in denied


def test_an_entry_whose_name_is_not_its_roots_is_never_read_or_verified(
    tmp_path: Path,
) -> None:
    """Review of 7ff033f, G3."""
    root = tmp_path / "harbor"
    entry = entry_for(root, 101)
    forged = entry.text().replace('"name":"harbor"', '"name":"tide"')
    with pytest.raises(fleet.FleetError, match="not the last step"):
        fleet.parse_index_entry(forged)
    directory = fleet.ensure_index(registry.configured_directory(os.environ))
    path = directory / f"{entry.key}{registry.ENTRY_SUFFIX}"
    registry.write_private(path, forged.encode("utf-8"))
    assert fleet.read_index_entry(path) is None
    assert fleet.read_index(directory) == []
    bypassed = entry_for(root, 101)
    object.__setattr__(bypassed, "name", "tide")
    with pytest.raises(fleet.FleetError, match="not the one the index names"):
        fleet.verify(bypassed, {"pid": 101, "project": str(root)})


def test_the_daemon_makes_the_index_owner_only_before_any_engine(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review of 7ff033f, G5. ``run`` calls ``make_index`` before the scheduler, and
    so any engine, is built."""
    directory = registry.configured_directory(os.environ)
    assert not index().exists()
    assert make_index(directory)
    assert stat.S_IMODE(index().stat().st_mode) == 0o700
    assert make_index(directory), "a second start finds it made"
    index().chmod(0o755)
    assert not make_index(directory)
    assert "must have mode 700" in capsys.readouterr().err
    source = inspect.getsource(daemon_module.run)
    assert source.index("make_index(registry_dir)") < source.index("Scheduler(")


def test_the_codex_profile_denies_the_index_once_the_daemon_has_made_it(
    short_runtime: Path,
) -> None:
    """Review of 7ff033f, G5. Codex drops a protected path that does not exist, so
    the denial holds only once ``make_index`` has made the directory."""
    paths = harbor_paths(short_runtime)
    real = os.path.realpath(index())

    def filesystem() -> dict[str, object]:
        protected = protected_dirs(paths, NO_FEDERATION)
        text = server_arguments([], LineConfig(), protected)
        entry = next(a for a in text if a.startswith("permissions.dromon-line="))
        profile = entry.removeprefix("permissions.dromon-line=")
        return tomllib.loads(f"profile = {profile}")["profile"]["filesystem"]

    assert real not in filesystem()
    assert make_index(registry.configured_directory(os.environ))
    assert filesystem()[real] == "deny"


def test_the_grok_profile_check_requires_the_index(short_runtime: Path) -> None:
    """Review of 7ff033f, G5. A Grok profile that does not deny the index is refused
    with the entry to add, and one that does is taken."""
    paths = harbor_paths(short_runtime)
    workspace = paths.root.resolve()
    (workspace / ".dromon").mkdir()
    assert make_index(registry.configured_directory(os.environ))
    protected = protected_dirs(paths, NO_FEDERATION)
    real = os.path.realpath(index())
    home = short_runtime.parent / "home"
    daemon_home = short_runtime.parent / "operator-home"
    (home / grok.HOME_DIR).mkdir(parents=True)
    sandbox = home / grok.HOME_DIR / "sandbox.toml"
    without = tuple(path for path in protected if str(path) != real)
    sandbox.write_text(
        grok.profile_snippet(grok.READER_PROFILE, workspace, without, daemon_home)
    )
    with pytest.raises(EngineRefused) as refused:
        grok.check_profile(
            LineConfig(), workspace, protected, home, home / grok.HOME_DIR, daemon_home
        )
    assert real in str(refused.value)
    sandbox.write_text(
        grok.profile_snippet(grok.READER_PROFILE, workspace, protected, daemon_home)
    )
    assert (
        grok.check_profile(
            LineConfig(), workspace, protected, home, home / grok.HOME_DIR, daemon_home
        )
        == grok.READER_PROFILE
    )


# The commands.


def test_fleet_list_shows_both_daemons(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor, tide = two
    assert cli.main(["fleet", "list", "--operator", "ana"]) == exits.OK
    out = capsys.readouterr().out
    assert (
        f"harbor: running, commit {COMMIT[:12]}, 2 lines, 1 open questions, 3 unread,"
        " 1 open issues\n"
    ) in out
    assert "tide: running" in out and "behind its checkout" in out
    assert str(harbor.root) in out and str(tide.root) in out
    assert found.sockets() == {harbor.socket, tide.socket}


def test_a_stale_entry_is_shown_and_never_asked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = Fleet()
    live = found.add(tmp_path / "harbor", 101)
    stale = found.add(tmp_path / "gone", 103, live=False)
    found.table[103] = LATER
    monkeypatch.setattr(cli, "call", found.call)
    monkeypatch.setattr(cli, "fleet_start_of", found.start_of)
    assert cli.main(["fleet", "list"]) == exits.OK
    assert "gone: stale, pid 103, not asked" in capsys.readouterr().out
    assert found.sockets() == {live.socket}
    assert cli.main(["fleet", "send", "gone:core", "hello"]) == exits.FAILURE
    assert "is stale, so nothing is sent" in capsys.readouterr().err
    assert stale.socket not in found.sockets()


def test_a_daemon_that_does_not_answer_is_reported_and_the_rest_go_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = Fleet()
    found.add(tmp_path / "harbor", 101)
    found.add(tmp_path / "mute", 104, answers=False)
    found.add(tmp_path / "tide", 102)
    monkeypatch.setattr(cli, "call", found.call)
    monkeypatch.setattr(cli, "fleet_start_of", found.start_of)
    assert cli.main(["fleet", "list"]) == exits.FAILURE
    out = capsys.readouterr().out
    assert "mute: not answering" in out
    assert "harbor: running" in out and "tide: running" in out
    assert cli.main(["fleet", "questions", "--operator", "ana"]) == exits.FAILURE
    captured = capsys.readouterr()
    assert "harbor:q-harbor from core to ana" in captured.out
    assert "tide:q-tide from core to ana" in captured.out
    assert "mute: not answering" in captured.err


def test_a_target_is_routed_to_its_project_alone(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor, tide = two
    assert cli.main(["fleet", "answer", "tide:q-1", "yes", "--operator", "ana"]) == 0
    assert found.calls[-1] == (
        tide.socket,
        Operation.ANSWER,
        {"question_id": "q-1", "body": "yes", "operator": "ana"},
    )
    assert cli.main(["fleet", "send", "harbor:core", "hello", "--operator", "ana"]) == 0
    assert found.calls[-1][:2] == (harbor.socket, Operation.SEND)
    assert found.calls[-1][2]["recipient"] == "core"
    assert cli.main(["fleet", "send", f"{tide.root}:@bo", "hi"]) == 0
    assert found.calls[-1][:2] == (tide.socket, Operation.SEND)
    assert found.calls[-1][2]["recipient"] == "@bo"
    assert cli.main(["fleet", "read", "harbor:m-1", "--operator", "ana"]) == 0
    assert found.calls[-1] == (
        harbor.socket,
        Operation.READ,
        {"operator": "ana", "message_id": "m-1"},
    )
    capsys.readouterr()


def test_every_routed_request_is_verified_first_through_the_same_socket(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
) -> None:
    found, harbor, _ = two
    cli.main(["fleet", "answer", "harbor:q-1", "yes"])
    assert [(socket, op) for socket, op, _ in found.calls] == [
        (harbor.socket, Operation.STATUS),
        (harbor.socket, Operation.ANSWER),
    ]


def test_no_operation_reaches_a_daemon_that_is_not_the_one_indexed(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Another process now answers harbor's socket, saying another pid. Nothing but
    the status that tells it apart is sent there."""
    found, harbor, _ = two
    found.handlers[harbor.socket] = daemon(harbor, pid=999)
    assert cli.main(["fleet", "send", "harbor:core", "hello"]) == exits.FAILURE
    assert "is not the one the index names" in capsys.readouterr().err
    assert [op for _, op, _ in found.calls] == [Operation.STATUS]
    assert cli.main(["fleet", "mail"]) == exits.FAILURE
    sent_to_harbor = [op for socket, op, _ in found.calls if socket == harbor.socket]
    assert set(sent_to_harbor) == {Operation.STATUS}


@pytest.mark.parametrize(
    "argv",
    [
        ["fleet", "list"],
        ["fleet", "questions"],
        ["fleet", "mail", "--unread"],
        ["fleet", "issues"],
        ["fleet", "answer", "harbor:q-1", "yes"],
        ["fleet", "send", "tide:core", "hello"],
        ["fleet", "read", "tide:m-1"],
        ["fleet", "status", "harbor"],
    ],
)
def test_every_fleet_command_reaches_only_indexed_operator_sockets(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor, tide = two
    cli.main(argv)
    capsys.readouterr()
    assert found.calls
    assert found.sockets() <= {harbor.socket, tide.socket}
    assert all(socket.name == "dromond.sock" for socket in found.sockets())


def test_fleet_mail_and_issues_name_each_message_by_project(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, _, _ = two
    assert cli.main(["fleet", "mail", "--unread", "--operator", "ana"]) == exits.OK
    out = capsys.readouterr().out
    assert "harbor:m-harbor from core, issue, unread" in out
    assert "tide:m-tide from core, issue, unread" in out
    mail_args = [args for _, op, args in found.calls if op is Operation.MAIL]
    assert all(args.get("unread_only") is True for args in mail_args)
    found.calls.clear()
    assert cli.main(["fleet", "issues"]) == exits.OK
    issue_args = [args for _, op, args in found.calls if op is Operation.MAIL]
    assert issue_args and all(args.get("issues_only") is True for args in issue_args)


def test_fleet_status_and_restart_use_the_single_project_commands(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor, _ = two
    seen: list[Path] = []

    def restarted(
        paths: cli.ProjectPaths, arguments: object, entry: fleet.IndexEntry
    ) -> int:
        del arguments
        assert entry == harbor
        seen.append(paths.socket)
        return exits.OK

    monkeypatch.setattr(cli, "_restart", restarted)
    assert cli.main(["fleet", "restart", "harbor"]) == exits.OK
    assert seen == [harbor.socket]
    assert cli.main(["fleet", "status", "tide"]) == exits.OK
    assert "running, pid 102" in capsys.readouterr().out
    assert found.calls[-1][1] is Operation.STATUS


# Review of 7ff033f, G1 and G2. ``fleet restart`` confirms its successor through the
# index, and its wait is bounded.


def status_of(entry: fleet.IndexEntry, pid: int) -> Response:
    return Response("r", True, result={"pid": pid, "project": str(entry.root)})


@dataclass
class Restarting:
    """``entry``'s daemon, which says ``before`` until it is asked to restart, and
    then whatever ``after`` gives for each status."""

    entry: fleet.IndexEntry
    after: Callable[[int], Response]
    before: int = 0
    restarts: int = 0
    polls: int = 0

    def __post_init__(self) -> None:
        self.before = self.entry.pid

    def __call__(self, operation: Operation, args: dict[str, object]) -> Response:
        del args
        if operation is Operation.RESTART:
            self.restarts += 1
            return Response("r", True, result={"restarting": True, "running": []})
        if self.restarts == 0:
            return status_of(self.entry, self.before)
        self.polls += 1
        return self.after(self.polls)


def no_sleep(seconds: float) -> None:
    del seconds


def preflight_passes(
    argv: object, runtime_dir: Path | None = None, environment: object = None
) -> None:
    """A restart preflight that passes, since these tests start no installed
    daemon. ``tests/test_restart.py`` covers the preflight itself."""
    del argv, runtime_dir, environment


@pytest.fixture
def restarting(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Fleet, fleet.IndexEntry]:
    monkeypatch.setattr(cli, "_preflight", preflight_passes)
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 1000.0)
    monkeypatch.setattr(cli.time, "sleep", no_sleep)
    found, harbor, _ = two
    return found, harbor


def test_fleet_restart_confirms_the_successor_through_its_own_index_entry(
    restarting: tuple[Fleet, fleet.IndexEntry],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor = restarting
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 0.0)
    successor = replace(harbor, pid=201)

    def after(poll: int) -> Response:
        if poll == 1:
            # Another process on the socket, under a pid the index does not list.
            return status_of(harbor, 999)
        if poll == 2:
            fleet.write_index_entry(index(), successor)
            found.table[201] = STARTED
        return status_of(harbor, 201)

    daemon_of = Restarting(harbor, after)
    found.handlers[harbor.socket] = daemon_of
    assert cli.main(["fleet", "restart", "harbor"]) == exits.OK
    assert daemon_of.polls == 2
    assert "restarted, pid 201" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("listed", "live", "says"),
    [
        (None, False, 999),
        (201, True, 999),
        (201, False, 201),
    ],
    ids=["unlisted", "listed-but-another-pid-answers", "listed-but-stale"],
)
def test_fleet_restart_never_takes_another_pid_on_the_socket_for_the_successor(
    restarting: tuple[Fleet, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
    listed: int | None,
    live: bool,
    says: int,
) -> None:
    found, harbor = restarting

    def after(poll: int) -> Response:
        if poll == 1 and listed is not None:
            fleet.write_index_entry(index(), replace(harbor, pid=listed))
            if live:
                found.table[listed] = STARTED
        return status_of(harbor, says)

    daemon_of = Restarting(harbor, after)
    found.handlers[harbor.socket] = daemon_of
    assert cli.main(["fleet", "restart", "harbor", "--wait-seconds", "1"]) == (
        exits.FAILURE
    )
    assert daemon_of.restarts == 1 and daemon_of.polls >= 1
    err = capsys.readouterr().err
    assert "no restarted daemon that the fleet index lists for this root" in err


def test_fleet_restart_asks_nothing_of_a_daemon_that_changed_since_it_was_verified(
    restarting: tuple[Fleet, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor = restarting
    statuses = iter([harbor.pid, 999])

    def changing(operation: Operation, args: dict[str, object]) -> Response:
        del args
        assert operation is Operation.STATUS, "nothing but status may be sent"
        return status_of(harbor, next(statuses))

    found.handlers[harbor.socket] = changing
    assert cli.main(["fleet", "restart", "harbor"]) == exits.FAILURE
    assert "not the one the index names" in capsys.readouterr().err


@pytest.mark.parametrize("fleet_restart", [False, True])
def test_the_wait_for_a_successor_is_bounded_however_long_requests_hang(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fleet_restart: bool,
) -> None:
    """A daemon that accepts and never answers. The clock runs on by each timeout
    the client gives, and the wait ends by its deadline, not by its count of polls."""
    _, harbor, _ = two
    clock = [0.0]
    timeouts: list[float] = []

    def hangs(
        socket_path: Path,
        operation: Operation,
        timeout_seconds: float = 5.0,
        args: dict[str, object] | None = None,
    ) -> Response:
        del socket_path, operation, args
        timeouts.append(timeout_seconds)
        clock[0] += timeout_seconds
        raise TimeoutError("no answer")

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(cli, "call", hangs)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "sleep", sleep)
    wait = 30
    successor = cli._fleet_successor(harbor) if fleet_restart else None  # pyright: ignore[reportPrivateUsage]
    request = (
        cli.FLEET_TIMEOUT_SECONDS if fleet_restart else cli.REQUEST_TIMEOUT_SECONDS
    )
    code = cli._await_successor(  # pyright: ignore[reportPrivateUsage]
        cli._fleet_paths(harbor),  # pyright: ignore[reportPrivateUsage]
        {"pid": harbor.pid},
        wait,
        successor,
        request,
    )
    assert code == exits.FAILURE
    bound = wait + cli.SHUTDOWN_TIMEOUT_SECONDS + cli.STARTUP_TIMEOUT_SECONDS
    assert clock[0] <= bound + cli.RESTART_POLL_SECONDS
    assert timeouts and max(timeouts) <= request
    polls = math.ceil(bound / cli.RESTART_POLL_SECONDS) + 1
    assert len(timeouts) < polls, "the deadline, not the count, ended the wait"
    assert "within" in capsys.readouterr().err


def test_a_shared_name_is_refused_until_the_root_is_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = Fleet()
    first = found.add(tmp_path / "a" / "harbor", 101)
    found.add(tmp_path / "b" / "harbor", 102)
    monkeypatch.setattr(cli, "call", found.call)
    monkeypatch.setattr(cli, "fleet_start_of", found.start_of)
    assert cli.main(["fleet", "send", "harbor:core", "hello"]) == exits.FAILURE
    assert "names more than one daemon" in capsys.readouterr().err
    assert found.calls == []
    assert cli.main(["fleet", "send", f"{first.root}:core", "hello"]) == exits.OK
    assert found.calls[-1][0] == first.socket


@pytest.mark.parametrize("target", ["harbor", ":core", "harbor:"])
def test_a_target_without_a_project_and_an_item_is_refused(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    target: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, _, _ = two
    assert cli.main(["fleet", "read", target]) == exits.FAILURE
    assert "is not PROJECT:MESSAGE" in capsys.readouterr().err
    assert found.calls == []


def test_an_unknown_project_and_a_refusal_are_reported(
    two: tuple[Fleet, fleet.IndexEntry, fleet.IndexEntry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found, harbor, _ = two
    assert cli.main(["fleet", "read", "nowhere:m-1"]) == exits.FAILURE
    assert "no daemon in the fleet index is named nowhere" in capsys.readouterr().err
    status = daemon(harbor)

    def refusing(operation: Operation, args: dict[str, object]) -> Response:
        if operation is Operation.STATUS:
            return status(operation, args)
        return Response("r", False, error_code=ErrorCode.REJECTED, error_message="no")

    found.handlers[harbor.socket] = refusing
    assert cli.main(["fleet", "questions"]) == exits.FAILURE
    assert "harbor: refused questions, no" in capsys.readouterr().err


def test_an_empty_index_lists_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = Fleet()
    monkeypatch.setattr(cli, "call", found.call)
    assert cli.main(["fleet", "list"]) == exits.OK
    assert "no daemon is in the fleet index" in capsys.readouterr().out
    assert found.calls == []


# The hosts of other machines.


def write_hosts(text: str, mode: int = 0o600) -> Path:
    path = fleet.configured_hosts_file(os.environ)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_hosts_are_read_and_hold_no_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write_hosts(
        '[hosts.studio]\ndestination = "studio.example"\n'
        '[hosts.lab]\ndestination = "ana@lab"\nremote_dromon = "/opt/dromon"\n'
    )
    hosts = fleet.read_hosts(path)
    assert hosts == {
        "lab": fleet.Host("lab", "ana@lab", "/opt/dromon"),
        "studio": fleet.Host("studio", "studio.example"),
    }
    assert cli.main(["fleet", "hosts"]) == exits.OK
    out = capsys.readouterr().out
    assert "studio: ssh studio.example, dromon there" in out
    assert "designed and not implemented" in out


def test_a_missing_hosts_file_names_no_host() -> None:
    assert fleet.read_hosts(fleet.configured_hosts_file(os.environ)) == {}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[hosts.s]\ndestination = "s"\npassword = "x"\n', "holds no credential"),
        ('[hosts.s]\ndestination = "s"\nidentity_file = "k"\n', "holds no credential"),
        ('[hosts.s]\ndestination = "-oProxyCommand=x"\n', "does not begin with -"),
        ('[hosts.s]\ndestination = "a b"\n', "without spaces"),
        ("[hosts.s]\n", "destination must be"),
        ('[hosts.S]\ndestination = "s"\n', "lowercase"),
        ('[hosts.s]\ndestination = "s"\nremote_dromon = "x; y"\n', "one command"),
        ('[hosts.s]\ndestination = "s"\nremote_registry = "r"\n', "absolute"),
        ('token = "x"\n', "not a key of the hosts file"),
    ],
)
def test_a_malformed_hosts_file_is_refused(text: str, reason: str) -> None:
    with pytest.raises(fleet.FleetError, match=reason):
        fleet.read_hosts(write_hosts(text))


def test_a_hosts_file_others_may_write_is_refused() -> None:
    path = write_hosts('[hosts.s]\ndestination = "s"\n', 0o620)
    with pytest.raises(fleet.FleetError, match="group or others"):
        fleet.read_hosts(path)
    path.chmod(0o644)
    assert set(fleet.read_hosts(path)) == {"s"}


def test_a_relative_hosts_override_is_refused() -> None:
    with pytest.raises(fleet.FleetError, match=fleet.HOSTS_FILE_ENV):
        fleet.configured_hosts_file({fleet.HOSTS_FILE_ENV: "hosts.toml"})
