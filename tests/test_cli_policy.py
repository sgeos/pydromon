"""Operator command lifecycle options and configuration visibility."""

from pathlib import Path

import pytest

from dromon import cli


def test_lifecycle_and_access_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "configure",
            "alpha",
            "--workdir-access",
            "read",
            "--keep-session",
            "--keep-session-at-max",
            "keep",
            "--allow-compact",
            "--auto-compact-tokens",
            "100000",
        ]
    )
    document = cli._config_document(args)  # pyright: ignore[reportPrivateUsage]
    assert document is not None
    assert document["workdir_access"] == "read"
    assert document["keep_session"] is True
    assert document["allow_compact"] is True
    assert document["auto_compact_tokens"] == 100000


@pytest.mark.parametrize(
    "option,value",
    [
        ("--workdir-access", "bad"),
        ("--keep-session-at-max", "bad"),
        ("--auto-compact-tokens", "bad"),
    ],
)
def test_bad_option_values(option: str, value: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["create", "alpha", option, value])


def test_config_summary_shows_operator_tables(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir()
    (state / "dromon.toml").write_text(
        'version = 1\nread_only_paths = ["docs"]\n'
        '[runs.commands]\ncheck = ["true"]\n'
        '[relays.tail]\nkind = "file"\npath = "notices.jsonl"\n'
        '[operators.ana]\nrelay = "tail"\n'
        '[federation]\nlocal = "allowlist"\nremote = "allowlist"\n'
        '[profiles.small]\nmodel = "claude-haiku-5-5"\n',
        encoding="utf-8",
    )
    assert (
        cli.main(
            ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
        )
        == 0
    )
    output = capsys.readouterr().out
    for expected in (
        "read_only_paths: docs",
        "run check:",
        "relay tail:",
        "operator ana: relay tail",
        "local allowlist",
        "remote allowlist",
        "profile small:",
    ):
        assert expected in output
