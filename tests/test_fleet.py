"""The fleet, round 13. ``docs/architecture/FLEET.md`` is normative.

The tests stand in for the process table and for every daemon's socket, since a
sandbox denies ``ps`` and Unix sockets. Every request a fleet command makes is
recorded with the socket it was sent to, so that a test can show no operation reaches
a daemon except through the socket its own index entry names.
"""

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dromon import cli, exits, fleet, registry
from dromon.daemon import fleet_registration, protected_dirs
from dromon.federation import NO_FEDERATION
from dromon.paths import project_paths
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
        del timeout_seconds
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


def test_a_daemon_registers_whatever_its_federation_mode(tmp_path: Path) -> None:
    (tmp_path / "harbor").mkdir()
    paths = project_paths(tmp_path / "harbor", tmp_path / "run")
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "harbor").mkdir()
    paths = project_paths(tmp_path / "harbor", tmp_path / "run")

    def unreadable(_pid: int) -> str | None:
        raise RuntimeError("ps is denied")

    directory = registry.configured_directory(os.environ)
    assert fleet_registration(paths, directory, unreadable, None) is None
    assert fleet_registration(paths, directory, NO_PROCESS.get, None) is None
    assert "not listed in the fleet index" in capsys.readouterr().err
    assert fleet.read_index(index()) == []


def test_every_line_is_denied_the_index_whatever_the_mode(tmp_path: Path) -> None:
    (tmp_path / "harbor").mkdir()
    paths = project_paths(tmp_path / "harbor", tmp_path / "run")
    denied = protected_dirs(paths, NO_FEDERATION)
    assert Path(os.path.realpath(index())) in denied
    default = fleet.index_directory(registry.default_directory())
    assert Path(os.path.realpath(default)) in denied


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

    def restarted(paths: cli.ProjectPaths, arguments: object) -> int:
        del arguments
        seen.append(paths.socket)
        return exits.OK

    monkeypatch.setattr(cli, "_restart", restarted)
    assert cli.main(["fleet", "restart", "harbor"]) == exits.OK
    assert seen == [harbor.socket]
    assert cli.main(["fleet", "status", "tide"]) == exits.OK
    assert "running, pid 102" in capsys.readouterr().out
    assert found.calls[-1][1] is Operation.STATUS


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
