"""The seed's smoke tests. They prove the package installs, imports and is wired."""

import tomllib
from importlib import import_module
from pathlib import Path

import dromon

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_reports_the_installed_version() -> None:
    # Both sides are derived, so a version bump in only one place fails here.
    assert dromon.__version__ == PYPROJECT["project"]["version"]


def test_the_distribution_name_matches_pyproject() -> None:
    assert PYPROJECT["project"]["name"] == dromon.DISTRIBUTION


def test_exactly_the_two_shared_commands_are_declared() -> None:
    assert set(PYPROJECT["project"]["scripts"]) == {"dromon", "dromond"}


def test_each_declared_command_resolves_to_a_callable() -> None:
    for target in PYPROJECT["project"]["scripts"].values():
        module_name, _, attribute = target.partition(":")
        assert callable(getattr(import_module(module_name), attribute))


def test_collection_skips_the_state_directory_a_line_cannot_read() -> None:
    # A Codex line's profile denies even the metadata of the state directory, and
    # pytest stats every entry of the root directory while collecting, so a project
    # that develops Dromon with Dromon must not collect there.
    from dromon.paths import STATE_DIR_NAME

    options = PYPROJECT["tool"]["pytest"]["ini_options"]["addopts"].split()
    assert f"--ignore={STATE_DIR_NAME}" in options
