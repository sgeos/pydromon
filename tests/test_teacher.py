"""The local teacher: its project table, and bringing it up and down against a fake
Ollama, so no test starts a server, loads a model or signals a real process."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from dromon import cli, exits
from dromon.daemon import release_teacher, start_teacher
from dromon.projectconfig import ConfigFileError, parse
from dromon.teacher import (
    PID_FILE,
    Teacher,
    TeacherConfig,
    TeacherError,
    parse_teacher,
)

MODEL = "qwen3:30b-a3b-instruct-2507-q4_K_M"


class FakeOllama:
    """Ollama's HTTP interface and the processes around it, in memory."""

    def __init__(
        self, *, running: bool = True, pulled: bool = True, starts: bool = True
    ) -> None:
        self.running = running
        self.pulled = pulled
        self.starts = starts
        self.loaded: dict[str, object] | None = None
        self.requests: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.started: list[tuple[list[str], dict[str, str]]] = []
        self.processes: dict[int, str] = {}
        self.signals: list[tuple[int, int]] = []

    def transport(
        self, method: str, url: str, body: Mapping[str, object] | None, timeout: float
    ) -> object:
        del timeout
        self.requests.append((method, url, body))
        if not self.running:
            raise ConnectionRefusedError("refused")
        path = url
        if path.endswith("/api/version"):
            return {"version": "0.40.10"}
        if path.endswith("/api/tags"):
            return {"models": [{"name": MODEL, "model": MODEL}] if self.pulled else []}
        if path.endswith("/api/ps"):
            return {"models": [self.loaded] if self.loaded else []}
        if path.endswith("/api/generate"):
            assert body is not None
            if body.get("keep_alive") == 0:
                if self.loaded is None:
                    raise OSError("HTTP Error 404: Not Found")
                self.loaded = None
            else:
                options = body.get("options")
                context = (
                    cast(dict[str, object], options).get("num_ctx")
                    if isinstance(options, dict)
                    else 0
                )
                self.loaded = {
                    "name": MODEL,
                    "size_vram": 18_900_000_000,
                    "context_length": context,
                }
            return {}
        raise AssertionError(f"unexpected request {method} {url}")

    def starter(self, command: list[str], env: Mapping[str, str], log: Path) -> int:
        del log
        self.started.append((command, dict(env)))
        if self.starts:
            self.running = True
            self.processes[4242] = "ollama"
        return 4242

    def command_of(self, pid: int) -> str | None:
        return self.processes.get(pid)

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        self.processes.pop(pid, None)
        self.running = False


def teacher(fake: FakeOllama, runtime: Path, **table: object) -> Teacher:
    config = parse_teacher({"model": MODEL, **table})
    ticks = iter(range(100_000))
    return Teacher(
        config,
        runtime,
        transport=fake.transport,
        starter=fake.starter,
        environment={"PATH": "/bin"},
        clock=lambda: float(next(ticks)),
        sleep=lambda seconds: None,
        command_of=fake.command_of,
        kill=fake.kill,
    )


# The table.


def test_the_table_takes_defaults_and_every_key() -> None:
    assert parse_teacher({"model": MODEL}) == TeacherConfig(MODEL)
    full = parse_teacher(
        {
            "model": MODEL,
            "endpoint": "http://localhost:11500",
            "context": 8192,
            "keep_alive": "2h",
            "autostart": True,
        }
    )
    assert (full.endpoint, full.context, full.keep_alive, full.autostart) == (
        "http://localhost:11500",
        8192,
        "2h",
        True,
    )


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ([], "must be a table"),
        ({}, "teacher.model must be"),
        ({"model": "bad name"}, "teacher.model must be"),
        ({"model": MODEL, "host": "x"}, "teacher.host is not a known key"),
        ({"model": MODEL, "endpoint": "https://127.0.0.1:11434"}, "must be http on"),
        ({"model": MODEL, "endpoint": "http://10.0.0.5:11434"}, "must be http on"),
        ({"model": MODEL, "endpoint": "http://127.0.0.1"}, "with a port"),
        ({"model": MODEL, "endpoint": "http://127.0.0.1:0"}, "with a port"),
        ({"model": MODEL, "endpoint": "http://127.0.0.1:99999"}, "with a port"),
        ({"model": MODEL, "context": 100}, "teacher.context must be"),
        ({"model": MODEL, "context": True}, "teacher.context must be"),
        ({"model": MODEL, "keep_alive": "forever"}, "teacher.keep_alive must be"),
        ({"model": MODEL, "keep_alive": 30}, "teacher.keep_alive must be"),
        ({"model": MODEL, "autostart": "yes"}, "teacher.autostart must be"),
    ],
)
def test_a_bad_table_is_refused(table: object, message: str) -> None:
    with pytest.raises(TeacherError, match=message):
        parse_teacher(table)


def test_the_project_file_holds_the_teacher_with_its_location() -> None:
    config = parse(f"version = 1\n[teacher]\nmodel = '{MODEL}'\ncontext = 4096\n")
    assert config.teacher == TeacherConfig(MODEL, context=4096)
    assert parse("version = 1\n").teacher is None
    with pytest.raises(ConfigFileError) as raised:
        parse("version = 1\n\n[teacher]\nmodel = 'x'\nendpoint = 'http://evil:1'\n")
    assert "teacher: teacher.endpoint must be http" in str(raised.value)
    assert raised.value.line == 3


# Up, status and down.


def test_up_loads_the_model_with_the_declared_context(tmp_path: Path) -> None:
    fake = FakeOllama()
    found = teacher(fake, tmp_path, context=4096, keep_alive="10m").up()
    assert found.loaded and found.context == 4096
    assert not found.started_by_dromon
    assert fake.started == []
    load = next(body for _, url, body in fake.requests if url.endswith("generate"))
    assert load == {
        "model": MODEL,
        "prompt": "",
        "stream": False,
        "keep_alive": "10m",
        "options": {"num_ctx": 4096},
    }
    assert "18.9 GB resident" in found.summary()


def test_up_starts_a_server_only_when_none_answers(tmp_path: Path) -> None:
    fake = FakeOllama(running=False)
    found = teacher(fake, tmp_path / "run", endpoint="http://127.0.0.1:11500").up()
    assert found.loaded and found.started_by_dromon
    command, environment = fake.started[0]
    assert command == ["ollama", "serve"]
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11500"
    assert (tmp_path / "run" / PID_FILE).read_text() == "4242\n"
    assert (tmp_path / "run").stat().st_mode & 0o777 == 0o700


def test_up_refuses_to_pull_and_stops_a_server_it_started(tmp_path: Path) -> None:
    fake = FakeOllama(running=False, pulled=False)
    with pytest.raises(TeacherError, match="not pulled. Pull it with `ollama pull"):
        teacher(fake, tmp_path).up()
    assert fake.signals and fake.signals[0][0] == 4242
    assert not (tmp_path / PID_FILE).exists()
    assert not any(url.endswith("/api/pull") for _, url, _ in fake.requests)


def test_up_refuses_to_pull_and_leaves_a_server_it_did_not_start(
    tmp_path: Path,
) -> None:
    fake = FakeOllama(pulled=False)
    with pytest.raises(TeacherError, match="not pulled"):
        teacher(fake, tmp_path).up()
    assert fake.signals == []
    assert fake.running


def test_a_server_that_never_answers_or_exits_is_reported(tmp_path: Path) -> None:
    fake = FakeOllama(running=False, starts=False)
    with pytest.raises(TeacherError, match="exited before answering"):
        teacher(fake, tmp_path).up()


def test_down_unloads_and_stops_only_its_own_server(tmp_path: Path) -> None:
    fake = FakeOllama()
    target = teacher(fake, tmp_path)
    target.up()
    released = target.down()
    assert not released.loaded and fake.running
    assert fake.signals == []
    own = FakeOllama(running=False)
    mine = teacher(own, tmp_path / "own")
    mine.up()
    mine.down()
    assert own.signals == [(4242, 15)]
    assert not (tmp_path / "own" / PID_FILE).exists()


def test_down_without_a_loaded_model_still_stops_its_server(tmp_path: Path) -> None:
    fake = FakeOllama(running=False)
    target = teacher(fake, tmp_path)
    target.up()
    fake.loaded = None
    target.down()
    assert fake.signals == [(4242, 15)]


def test_a_reused_process_identifier_is_never_signalled(tmp_path: Path) -> None:
    fake = FakeOllama()
    (tmp_path / PID_FILE).write_text("4242\n")
    fake.processes[4242] = "zsh"
    target = teacher(fake, tmp_path)
    assert not target.status().started_by_dromon
    target.down()
    assert fake.signals == []
    assert not (tmp_path / PID_FILE).exists()


def test_status_reports_an_unreachable_server(tmp_path: Path) -> None:
    found = teacher(FakeOllama(running=False), tmp_path).status()
    assert not found.reachable
    assert found.summary() == "server not reachable"


# The daemon and the client.


def test_the_daemon_brings_up_only_an_autostart_teacher(
    tmp_path: Path, short_runtime: Path
) -> None:
    from dromon.paths import project_paths

    paths = project_paths(tmp_path, short_runtime)
    assert start_teacher(parse("version = 1\n"), paths) is None
    manual = parse(f"version = 1\n[teacher]\nmodel = '{MODEL}'\n")
    assert start_teacher(manual, paths) is None


def test_a_server_that_will_not_stop_is_reported_by_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeOllama(running=False)
    target = teacher(fake, tmp_path)
    target.up()
    fake.kill = lambda pid, sig: None  # the server ignores the signal
    target._kill = fake.kill  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TeacherError, match="did not stop"):
        target.down()
    release_teacher(target)
    assert "could not release the teacher" in capsys.readouterr().err


def test_dromon_teacher_needs_a_teacher_table(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "teacher"]
    assert cli.main(argv) == exits.FAILURE
    assert "declares no [teacher]" in capsys.readouterr().err


def test_dromon_config_reports_the_teacher(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(
        f"version = 1\n[teacher]\nmodel = '{MODEL}'\nautostart = true\n"
    )
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.OK
    out = capsys.readouterr().out
    assert (
        f"teacher: {MODEL} at http://127.0.0.1:11434, context 4096, keep alive 30m,"
        " autostart" in out
    )
