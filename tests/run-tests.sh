#!/bin/sh
# Lightweight test runner. Doesn't require a special test framework — just
# stdlib unittest plus optional shellcheck/pyflakes. Run from the repo root.

set -e

cd "$(dirname "$0")/.."

echo '==> python -m unittest tests/test_config.py'
PYTHONPATH=src python3 -m unittest tests.test_config -v

if command -v shellcheck >/dev/null 2>&1; then
    echo '==> shellcheck scripts/play-beep.sh initcpio/install-hook'
    shellcheck scripts/play-beep.sh initcpio/install-hook
else
    echo '(shellcheck not installed; skipping)'
fi

if command -v pyflakes >/dev/null 2>&1; then
    echo '==> pyflakes src/cryptsetup_beep'
    pyflakes src/cryptsetup_beep
else
    echo '(pyflakes not installed; skipping)'
fi

echo '==> all checks passed'
