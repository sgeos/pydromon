"""Operator migration of generated Grok files without reading sign-in material."""

import os
from pathlib import Path

import pytest

from dromon import cli, exits, grok
from dromon.engine import EngineRefused


def test_preview_is_read_only_and_replacement_keeps_both_originals(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text("old config\n")
    (tmp_path / "requirements.toml").write_text("old requirements\n")
    changes = grok.home_changes(tmp_path)
    assert len(changes) == 2
    assert "-old config" in grok.home_diff(changes)
    assert (tmp_path / "config.toml").read_text() == "old config\n"
    backups = grok.replace_home(tmp_path, changes)
    assert [path.read_text() for path in backups] == [
        "old config\n",
        "old requirements\n",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)
    assert all(".toml." in path.name and path.name.endswith(".bak") for path in backups)
    assert not grok.home_changes(tmp_path)
    assert (tmp_path / "config.toml").read_text() == grok.HOME_CONFIG
    assert (tmp_path / "requirements.toml").read_text() == grok.HOME_REQUIREMENTS


def test_preview_changed_after_confirmation_is_refused(tmp_path: Path) -> None:
    changes = grok.home_changes(tmp_path)
    (tmp_path / "config.toml").write_text("changed")
    with pytest.raises(EngineRefused, match="after preview"):
        grok.replace_home(tmp_path, changes)
    assert (tmp_path / "config.toml").read_text() == "changed"
    assert not list(tmp_path.glob("*.bak"))


def test_linked_generated_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "other"
    target.write_text("private")
    (tmp_path / "config.toml").symlink_to(target)
    with pytest.raises(EngineRefused, match="without links"):
        grok.home_changes(tmp_path)
    assert target.read_text() == "private"


def test_interrupted_second_replacement_recovers_on_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.toml").write_text("old config")
    (tmp_path / "requirements.toml").write_text("old requirements")
    real_replace = os.replace

    def replace(source: Path, destination: Path) -> None:
        if destination.name == "requirements.toml":
            raise OSError("interrupted")
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(grok.os, "replace", replace)
        with pytest.raises(EngineRefused, match="interrupted"):
            grok.replace_home(tmp_path, grok.home_changes(tmp_path))
    assert (tmp_path / "config.toml").read_text() == grok.HOME_CONFIG
    assert (tmp_path / "requirements.toml").read_text() == "old requirements"
    changes = grok.home_changes(tmp_path)
    assert [change.name for change in changes] == ["requirements.toml"]
    grok.replace_home(tmp_path, changes)
    assert not grok.home_changes(tmp_path)
    assert len(list(tmp_path.glob("*.bak"))) == 3
    assert not list(tmp_path.glob(".*.new"))


@pytest.mark.parametrize("answer", ["n", "y"])
def test_cli_previews_before_confirmed_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    arguments = [
        "--project",
        str(tmp_path),
        "--runtime-dir",
        str(short_runtime),
        "grok-home",
    ]
    home = tmp_path / grok.HOME_DIR

    def confirm(prompt: str) -> str:
        assert "Replace" in prompt
        assert "config.toml" in capsys.readouterr().out
        assert not home.exists()
        return answer

    monkeypatch.setattr("builtins.input", confirm)
    assert cli.main(arguments) == exits.OK
    assert not home.exists()
    capsys.readouterr()
    assert cli.main([*arguments, "--apply"]) == exits.OK
    assert home.exists() == (answer == "y")
