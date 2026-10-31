"""Exact disclosure acceptance boundaries and configuration validation."""

from hashlib import sha256

import pytest

from dromon import disclosure
from dromon.projectconfig import ConfigFileError, parse


def test_acceptance_is_exact_and_file_scoped() -> None:
    text = "a private token"
    digest = sha256(text.encode()).hexdigest()
    config = parse(
        f'version = 1\n[daemon.disclosure_accepted]\n"docs/a.md" = ["{digest}"]\n'
    )
    hashes = config.disclosure_accepted["docs/a.md"]
    assert disclosure.matching_lines(("private",), text, hashes) == 0
    assert disclosure.matching_lines(("private",), text + " changed", hashes) == 1
    assert disclosure.matching_lines(("private",), text) == 1
    assert (
        disclosure.matching_lines(
            ("private",), text, config.disclosure_accepted.get("docs/b.md", ())
        )
        == 1
    )


@pytest.mark.parametrize(
    "path,hashes",
    [
        ("../escape", '["' + "a" * 64 + '"]'),
        ("/absolute", "[]"),
        ("ok", '["bad"]'),
        ("ok", '"bad"'),
    ],
)
def test_invalid_acceptance(path: str, hashes: str) -> None:
    with pytest.raises(ConfigFileError, match="disclosure_accepted"):
        parse(f'version = 1\n[daemon.disclosure_accepted]\n"{path}" = {hashes}\n')


def test_empty_acceptance_is_valid() -> None:
    assert parse("version = 1").disclosure_accepted == {}
