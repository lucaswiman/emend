#!/usr/bin/env bash
# Run emend.vim tests.
#
# Usage:
#   ./test/run_tests.sh              # Run all tests
#   ./test/run_tests.sh test/foo.vader  # Run specific test
#
# Prerequisites:
#   - vim or nvim on PATH
#   - vader.vim installed (auto-downloaded if missing)

set -euo pipefail
cd "$(dirname "$0")/.."

VADER_DIR="${VADER_DIR:-test/vader.vim}"

# Auto-download vader.vim if not present.
if [ ! -d "$VADER_DIR" ]; then
  echo "Downloading vader.vim..."
  git clone --depth 1 https://github.com/junegunn/vader.vim.git "$VADER_DIR"
fi

# Determine which editor to use.
if command -v nvim &>/dev/null; then
  VIM="nvim --headless"
elif command -v vim &>/dev/null; then
  VIM="vim -N"
else
  echo "error: neither nvim nor vim found on PATH" >&2
  exit 1
fi

# Build a minimal vimrc that loads our plugin and vader.
VIMRC="$(mktemp)"
cat > "$VIMRC" <<VIMRC
set nocompatible
filetype off
set rtp+=$(pwd)
set rtp+=$VADER_DIR
filetype plugin indent on
syntax enable
VIMRC

# Default to all .vader files.
TESTS="${*:-test/*.vader}"

echo "Running tests with: $VIM"
echo "Tests: $TESTS"

# shellcheck disable=SC2086
$VIM -u "$VIMRC" -c "Vader! $TESTS" 2>&1

STATUS=$?
rm -f "$VIMRC"
exit $STATUS
