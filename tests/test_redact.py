"""What Dromon records of a command line or a task's description, with secrets
masked. **Review N9 and N10 of the stop policy's re-review.**"""

import pytest

from dromon.redact import MASK, MAX_SUMMARY_CHARACTERS, SECRET_WORDS, mask, summary

CASES = (
    (
        'curl -H "Authorization: Bearer abc.def" https://x/y',
        ("abc.def",),
        "curl",
    ),
    (
        "curl -u x https://h -H 'Authorization: Basic dXNlcjpwdw=='",
        ("dXNlcjpw",),
        "curl",
    ),
    ("git clone https://ana:pa55@github.com/o/r.git", ("ana:pa55", "pa55"), "git"),
    ("/usr/bin/mysql --password=hunter2 --user root", ("hunter2",), "mysql"),
    ("tool --api-key sk-123 --verbose", ("sk-123",), "tool"),
    ("tool --client_secret=s3 --x", ("s3 ",), "tool"),
    (
        "TOKEN=abc /usr/local/bin/curl https://h/p?access_token=zzz&x=1",
        ("abc", "zzz"),
        "TOKEN=***",
    ),
    ('curl -H "X-Api-Key: k1k1" -b "Cookie: s=1"', ("k1k1", "s=1"), "curl"),
    ("deploy --GitHub-Token TOK --dry-run", ("TOK",), "deploy"),
)


@pytest.mark.parametrize(("command", "secrets", "name"), CASES)
def test_a_secret_is_masked_and_the_executable_named(
    command: str, secrets: tuple[str, ...], name: str
) -> None:
    found = summary(command)
    assert found.split(" ", 1)[0] == name
    assert MASK in found
    for secret in secrets:
        assert secret not in found
        assert secret not in mask(command)


def test_a_command_without_a_secret_keeps_its_arguments() -> None:
    assert summary("/bin/sleep 613") == "sleep 613"
    assert summary("/bin/bash -c make watch") == "bash -c make watch"
    assert mask("make gate") == "make gate"
    # The next argument is an option, so the flag takes no value to mask.
    assert mask("tool --token -v") == "tool --token -v"


def test_every_secret_word_masks_an_option_s_value() -> None:
    for word in SECRET_WORDS:
        assert mask(f"x --{word} v4lue") == f"x --{word} {MASK}"
        assert mask(f"x {word.upper()}=v4lue") == f"x {word.upper()}={MASK}"


def test_a_summary_is_cut_after_masking() -> None:
    long = "tool " + "a" * (2 * MAX_SUMMARY_CHARACTERS) + " --token secretvalue"
    found = summary(long)
    assert len(found) == MAX_SUMMARY_CHARACTERS
    assert "secretvalue" not in found
    assert summary("") == ""
    assert summary("   ") == ""
