"""An attached client whose daemon is gone, and the curses loop on a dead terminal.

Round 9, item 6, and its review, F14. A client detaches with a message once its
daemon's socket file has been missing for its bound, and once connections have been
refused for a minute, since a live daemon with a full backlog may refuse them. A slow
daemon never detaches it."""

import curses
import socket
from pathlib import Path

import pytest

from dromon import client as client_module
from dromon import exits
from dromon.client import DaemonNotRunning
from dromon.paths import project_paths
from dromon.protocol import Operation, Response
from dromon.ui import link
from dromon.ui import model as ui_model
from dromon.ui import terminal
from dromon.ui.link import SocketDaemon
from dromon.ui.model import Failed, LinesLoaded, Lost, Model, Tick
from dromon.ui.runtime import Runtime, Unreachable, detached_status
from dromon.ui.update import initial, update


class Gone:
    """A daemon port that finds nothing accepting connections, its socket file
    missing or present, or is answered once ``back``."""

    def __init__(self, *, missing: bool = True) -> None:
        self.back = False
        self.missing = missing
        self.calls = 0

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        del operation, args
        self.calls += 1
        if not self.back:
            raise Unreachable(
                "the daemon is not running", gone=True, missing=self.missing
            )
        return Response("r", ok=False, error_message="refused")


def polled(model: Model, now: float, event: Lost | Failed) -> Model:
    model, _ = update(model, Tick(now))
    model, _ = update(model, event)
    return model


def test_a_client_detaches_once_its_socket_has_been_missing_for_its_bound() -> None:
    daemon = Gone()
    client = Runtime(daemon, "alpha", "ana", 1.0)
    now = 0.0
    polls = 0
    while not client.finished and polls < 100:
        client.tick(now)
        polls += 1
        now += 1.0
    assert client.finished
    # Both bounds hold: at least LOST_POLLS polls and at least LOST_SECONDS.
    assert polls >= ui_model.LOST_POLLS
    assert now - 1.0 >= ui_model.LOST_SECONDS
    assert now - 1.0 < ui_model.REFUSED_SECONDS
    message = client.exit_message
    assert message is not None and "the daemon is not running" in message
    assert "so the client detached" in message


def test_refused_connections_detach_only_after_a_minute() -> None:
    """Round 9 review, F14. With the socket file present, a refusal may come from a
    live daemon whose backlog is full, so the client keeps polling past the bound
    for a missing socket, and detaches only once refusals pass ``REFUSED_SECONDS``."""
    model = initial("alpha", "ana", 1.0)
    second = 0
    refused = Lost("the daemon refused the connection", missing=False)
    while second < ui_model.REFUSED_SECONDS:
        model = polled(model, float(second), refused)
        assert not model.finished, second
        second += 1
    model = polled(model, float(second), refused)
    assert model.finished
    assert model.exit_message is not None
    assert "refused the connection" in model.exit_message


def test_a_missing_socket_after_refusals_starts_its_own_bound() -> None:
    """A socket file that disappears after refusals, as when a daemon that stalled
    then stopped, detaches the client ``LOST_SECONDS`` after it went missing."""
    model = initial("alpha", "ana", 1.0)
    for second in range(20):
        model = polled(model, float(second), Lost("refused", missing=False))
    assert not model.finished
    missing = Lost("gone", missing=True)
    second = 20
    while second < 20 + ui_model.LOST_SECONDS:
        model = polled(model, float(second), missing)
        assert not model.finished, second
        second += 1
    model = polled(model, float(second), missing)
    assert model.finished


def test_a_refusal_resets_the_bound_for_a_missing_socket() -> None:
    model = initial("alpha", "ana", 1.0)
    for second in range(8):
        model = polled(model, float(second), Lost("gone", missing=True))
    model = polled(model, 8.0, Lost("refused", missing=False))
    assert model.missing_since is None
    for second in range(9, 15):
        model = polled(model, float(second), Lost("gone", missing=True))
    assert not model.finished


def test_one_poll_counts_once_and_a_short_interval_waits_out_the_seconds() -> None:
    model = initial("alpha", "ana", 0.1)
    model, _ = update(model, Tick(0.0))
    for _ in range(10):
        model, _ = update(model, Lost("gone", missing=True))
    assert model.lost_polls == 1
    now = 0.0
    for _ in range(ui_model.LOST_POLLS * 2):
        now += 0.25
        model = polled(model, now, Lost("gone", missing=True))
    assert model.lost_polls > ui_model.LOST_POLLS and not model.finished
    model = polled(model, ui_model.LOST_SECONDS + 0.5, Lost("gone", missing=True))
    assert model.finished


def test_a_daemon_that_answers_again_resets_the_count() -> None:
    model = initial("alpha", "ana", 1.0)
    for second in range(ui_model.LOST_POLLS - 1):
        model = polled(model, float(second), Lost("gone", missing=True))
    assert model.lost_polls == ui_model.LOST_POLLS - 1
    model, _ = update(model, LinesLoaded(()))
    assert (model.lost_since, model.missing_since, model.lost_polls) == (None, None, 0)


def test_a_slow_daemon_never_detaches_the_client() -> None:
    model = initial("alpha", "ana", 1.0)
    for second in range(200):
        model = polled(
            model, float(second), Failed("the daemon did not answer: timed out")
        )
    assert not model.finished and model.lost_polls == 0


def test_the_socket_port_says_missing_when_there_is_no_socket(
    tmp_path: Path, short_runtime: Path
) -> None:
    """The runtime directory does not exist, as after a project was deleted, so the
    socket file is missing."""
    paths = project_paths(tmp_path, short_runtime)
    with pytest.raises(Unreachable) as raised:
        SocketDaemon(paths).call(Operation.STATUS, {})
    assert (raised.value.gone, raised.value.missing) == (True, True)
    assert (Unreachable("slow").gone, Unreachable("slow").missing) == (False, False)


class Refusing:
    """A socket whose connect fails as ``error`` says."""

    def __init__(self, error: OSError) -> None:
        self.error = error

    def __enter__(self) -> "Refusing":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def settimeout(self, value: float) -> None:
        del value

    def connect(self, address: str) -> None:
        raise self.error


@pytest.mark.parametrize(
    ("error", "missing"),
    [(FileNotFoundError(2, "absent"), True), (ConnectionRefusedError(61, "no"), False)],
)
def test_the_client_says_whether_the_socket_was_missing_or_refused(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    missing: bool,
) -> None:
    def fake(family: int, kind: int) -> Refusing:
        assert (family, kind) == (socket.AF_UNIX, socket.SOCK_STREAM)
        return Refusing(error)

    monkeypatch.setattr(client_module.socket, "socket", fake)
    with pytest.raises(DaemonNotRunning) as raised:
        client_module.call(tmp_path / "s", Operation.STATUS)
    assert raised.value.missing is missing
    paths = project_paths(tmp_path, short_runtime)
    with pytest.raises(Unreachable) as port:
        SocketDaemon(paths).call(Operation.STATUS, {})
    assert (port.value.gone, port.value.missing) == (True, missing)


def test_the_port_maps_a_refusal_to_gone_but_not_missing(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object) -> Response:
        raise DaemonNotRunning("no daemon listens", missing=False)

    monkeypatch.setattr(link, "call", refused)
    with pytest.raises(Unreachable) as raised:
        SocketDaemon(project_paths(tmp_path, short_runtime)).call(Operation.STATUS, {})
    assert (raised.value.gone, raised.value.missing) == (True, False)
    assert "refused" in str(raised.value)


def test_the_detached_client_exits_with_its_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert detached_status(None, 0) == 0
    client = Runtime(Gone(), "alpha", "ana", 1.0)
    assert detached_status(client, 0) == 0
    for second in range(30):
        client.tick(float(second))
    assert detached_status(client, 0) == exits.NOT_RUNNING
    assert capsys.readouterr().err.startswith("dromon attach: the daemon is not")


class DeadTerminal:
    """A screen whose reads fail at once, as curses does once the terminal is gone."""

    def __init__(self) -> None:
        self.reads = 0

    def getmaxyx(self) -> tuple[int, int]:
        return (8, 60)

    def erase(self) -> None:
        pass

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None:
        pass

    def move(self, y: int, x: int, /) -> None:
        pass

    def refresh(self) -> None:
        pass

    def timeout(self, delay: int, /) -> None:
        pass

    def get_wch(self) -> str | int:
        self.reads += 1
        raise curses.error("no input")


def test_the_curses_loop_paces_dead_reads_and_ends_when_the_daemon_is_gone() -> None:
    """Every read that returns at once waits out the input timeout, and the loop
    ends by itself, where before it spun without end."""
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    screen = DeadTerminal()
    client = Runtime(Gone(), "alpha", "ana", 1.0)
    assert (
        terminal.loop(
            screen, client, terminal.palette(False), clock=lambda: now[0], sleep=sleep
        )
        == 0
    )
    assert client.finished and client.exit_message is not None
    assert slept and all(
        seconds == pytest.approx(terminal.INPUT_TIMEOUT_MS / 1000) for seconds in slept
    )
    assert screen.reads == len(slept)
    # The loop ran for the bound in seconds, so its passes are bounded by it.
    assert len(slept) <= (ui_model.LOST_SECONDS + 2) * 1000 / terminal.INPUT_TIMEOUT_MS
