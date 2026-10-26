# Contributing

Contributions are welcome, including fixes, documentation and tests.

**The project is licensed under 0BSD.** By contributing you agree that your contribution
is released under the same licence. Do not contribute material that you did not write,
or that carries terms of its own.

Participation in the project's spaces follows the
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Contributions are judged on their technical
merits.

## Setting up

The build needs Python 3.12 or newer, [uv](https://github.com/astral-sh/uv), and a Rust
toolchain, because the `pykeleusma` dependency is built from its repository.

```sh
uv sync
./scripts/check.sh
```

## The gate

`./scripts/check.sh` runs the repository hygiene checks, then Ruff, pyright in strict
mode and pytest with warnings treated as errors. Continuous integration runs the same
script. Arguments are passed to pytest, and `--hygiene-only` runs the repository checks
alone. The hygiene checks need `python3` and `shellcheck`, and fail without them.
`git config core.hooksPath scripts/hooks` runs the gate before every push.

- **Socket tests** bind Unix domain sockets. They run by default. Inside a sandbox that
  denies sockets, such as Claude Code's, run `./scripts/check.sh -m "not socket and not
  live"`, and say so in the pull request.
- **Live tests** call the real engines on the operator's subscriptions. They are
  deselected by default, need `DROMON_LIVE_TESTS=1` as well as `-m live`, and are never
  expected from contributors. A change to an engine adapter says whether the live tests
  were run.

## Before opening a pull request

1. Run `./scripts/check.sh` and confirm that it exits zero.
2. Keep each pull request to one change, on a branch named
   `<scope>/<short-description>`, such as `fix/parser-error-recovery`.
3. Write commit messages as conventional commits, such as `feat:`, `fix:`, `docs:`,
   `test:` or `chore:`.
4. Add an entry under `[Unreleased]` in `CHANGELOG.md` for any change a user would notice.
5. Update the specification under [`docs/protocol/`](docs/protocol/README.md) when a
   behaviour it describes changes. It is normative, and the Rust port will be written
   from it.
6. Do not weaken a check to make it pass. If a check is wrong, say so in the pull request
   and fix it deliberately.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow [`SECURITY.md`](SECURITY.md).
