"""The tests start children through ``spawn``. **Round 18.**

A direct ``subprocess.Popen`` in a test hung a gate for five minutes in the read of the
exec status pipe on 2026-11-18, which no bound of ``spawn`` covers, since the test did
not use it. A test that does not test ``subprocess`` itself starts its children with
``spawn.start`` or ``spawn.run``, whose starts are bounded, sampled and recorded. The
tests that must start a child in another way are named below, each with its reason, and
this module fails when one is added without a name or a name outlives its use.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parent

CALLS: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        {
            "Popen",
            "run",
            "call",
            "check_call",
            "check_output",
            "getoutput",
            "getstatusoutput",
        }
    ),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "os": frozenset({"posix_spawn", "posix_spawnp", "system", "popen", "fork"}),
}

FORK = (
    "a test of the fork lock or the fork timing, which must make the start that the"
    " library makes"
)
EXEC = "the exec wait of a direct Popen is what is tested"


def named(file: str, reason: str, *functions: str) -> dict[str, str]:
    return {f"{file}::{function}": reason for function in functions}


ALLOWED: dict[str, str] = {
    **named(
        "test_bounded_process.py",
        "the control that shows the defect of asyncio's own start",
        "test_asyncios_own_start_stops_the_loop_for_as_long_as_the_fork_stalls",
    ),
    **named(
        "test_exec_wait.py",
        EXEC,
        "test_a_child_stuck_between_fork_and_exec_is_sampled_and_recorded",
        "test_a_start_that_executes_in_time_leaves_no_entry_and_no_sample",
        "test_forks_past_the_watch_limit_are_not_queued",
        "test_the_report_is_made_while_the_start_is_still_waiting",
        "_stuck_start",
    ),
    **named(
        "test_spawn.py",
        FORK,
        "test_a_fork_call_under_the_threshold_is_not_recorded",
        "test_a_plain_start_beside_a_start_on_the_loop_does_not_run_its_sweep",
        "test_a_slow_fork_call_is_recorded_whoever_started_it",
        "test_a_slow_fork_note_does_not_wait_for_a_blocked_standard_error",
        "test_a_start_on_the_event_loop_waits_a_shorter_time_for_the_fork_lock",
        "test_an_asyncio_start_waits_while_another_fork_holds_the_lock",
        "test_no_start_path_nests_a_start",
        "test_posix_spawn_waits_while_another_fork_holds_the_lock",
    ),
    **named(
        "test_spawn_stress.py",
        "the stress test mixes every kind of start",
        "_asyncio_start",
        "_direct_posix_spawn",
    ),
}
"""``file::function`` of each test that may start a child directly, and why."""


def direct_starts(path: Path) -> set[str]:
    """The ``file::function`` of every function in ``path`` that calls a name of
    ``CALLS``, the outermost function for a nested one, and ``file::(module)`` for a
    call outside any."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if owner == "(module)" and isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                name = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.attr in CALLS.get(child.func.value.id, frozenset())
            ):
                found.add(f"{path.name}::{owner}")
            visit(child, name)

    visit(tree, "(module)")
    return found


def all_direct_starts() -> set[str]:
    found: set[str] = set()
    for path in sorted(TESTS.glob("*.py")):
        if path.name != Path(__file__).name:
            found |= direct_starts(path)
    return found


def test_every_direct_start_in_the_tests_is_named_with_a_reason() -> None:
    unnamed = all_direct_starts() - set(ALLOWED)
    assert not unnamed, (
        "start the child with spawn.start or spawn.run, or name the test in ALLOWED"
        f" with its reason: {sorted(unnamed)}"
    )


def test_a_name_in_the_list_still_starts_a_child_directly() -> None:
    assert set(ALLOWED) <= all_direct_starts(), (
        f"remove from ALLOWED: {sorted(set(ALLOWED) - all_direct_starts())}"
    )


def test_every_reason_is_given() -> None:
    assert all(reason.strip() for reason in ALLOWED.values())


def test_the_scan_sees_each_kind_of_direct_start(tmp_path: Path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import asyncio, os, subprocess\n"
        "def test_a():\n    subprocess.Popen(['x'])\n"
        "def test_b():\n    subprocess.run(['x'], check=False)\n"
        "async def test_c():\n    await asyncio.create_subprocess_exec('x')\n"
        "def test_d():\n    os.posix_spawn('x', ['x'], {})\n"
        "def test_e():\n    def inner():\n        subprocess.check_output(['x'])\n"
        "def test_f():\n    spawn.run(['x'], timeout=1)\n"
        "subprocess.call(['x'])\n",
        encoding="utf-8",
    )
    assert direct_starts(sample) == {
        "test_sample.py::test_a",
        "test_sample.py::test_b",
        "test_sample.py::test_c",
        "test_sample.py::test_d",
        "test_sample.py::test_e",
        "test_sample.py::(module)",
    }
