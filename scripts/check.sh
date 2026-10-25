#!/usr/bin/env bash
# The local gate. Continuous integration runs this same script, so a green run here
# means what a green run there means, apart from operating system and tool versions.
#
# It exits zero only when every check passes. Each check prints one line on success
# and an explanation on failure, and the script stops at the first failure.
#
# Repository hygiene runs first and needs only git. The project's checks follow, in the
# order that fails fastest. Arguments are passed to pytest, so the in-sandbox form is
#
#     ./scripts/check.sh -m "not socket and not live"
#
# because Claude Code's sandbox denies binding a Unix domain socket. Without arguments
# the socket tests run, and live tests stay deselected by pyproject.toml. Live tests
# spend the operator's subscriptions and are never run by this script by default.
# Continuous integration sets UV_PYTHON so that `uv sync` installs for its matrix
# interpreter.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail() {
    printf 'check.sh: FAIL: %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'check.sh: ok: %s\n' "$1"
}

# --- Repository hygiene -------------------------------------------------------------

# secret/ is ignored whole. A tracked file there was added deliberately with
# `git add -f` or before the ignore rule existed, and either way it is a disclosure.
tracked_secret="$(git ls-files -- secret)"
if [[ -n "$tracked_secret" ]]; then
    fail "files under secret/ are tracked and must be removed from the index and history:
$tracked_secret"
fi
pass "nothing under secret/ is tracked"

# tmp/ is scratch. Only its marker is tracked, so the directory exists in a clone.
tracked_tmp="$(git ls-files -- tmp | grep -vx 'tmp/.gitkeep' || true)"
if [[ -n "$tracked_tmp" ]]; then
    fail "files under tmp/ other than tmp/.gitkeep are tracked:
$tracked_tmp"
fi
if [[ -z "$(git ls-files -- tmp/.gitkeep)" ]]; then
    fail "tmp/.gitkeep is not tracked, so tmp/ will not exist in a fresh clone"
fi
pass "only tmp/.gitkeep is tracked under tmp/"

# Unresolved merge markers in tracked text files. Binary files are skipped by -I.
if git grep -nI -E '^(<<<<<<< |>>>>>>> |=======$)' -- . ':!scripts/check.sh' >/dev/null; then
    git grep -nI -E '^(<<<<<<< |>>>>>>> |=======$)' -- . ':!scripts/check.sh' >&2
    fail "unresolved merge markers are present"
fi
pass "no unresolved merge markers"

# --- Project checks -----------------------------------------------------------------

# A stale environment tests old code, so the installed environment must match the lock.
uv sync --locked
pass "the environment matches uv.lock"

uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
pass "ruff lint and format"

uv run --no-sync pyright
pass "pyright strict"

# Warnings are errors. pytest exits non-zero when it collects nothing, so an empty
# selection fails here rather than passing.
uv run --no-sync pytest -W error "$@"
pass "pytest"

# The scripts that gate the work are code too, as the Keleusma repository found. When
# the linter is absent the gap is stated rather than passed over, and continuous
# integration runs it on Linux, where it is required.
if command -v shellcheck >/dev/null; then
    shellcheck scripts/*.sh
    pass "shellcheck on scripts/"
else
    printf 'check.sh: note: shellcheck is not installed, so scripts/ were not linted\n'
fi
