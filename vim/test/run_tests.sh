#!/usr/bin/env bash
# Run emend.vim tests using vader.vim.
#
# Usage:
#   ./test/run_tests.sh              # Run all tests
#   ./test/run_tests.sh test/foo.vader  # Run specific test
#
# Prerequisites:
#   - nvim (or vim 8+) on PATH
#   - vader.vim is auto-downloaded if missing

set -euo pipefail
cd "$(dirname "$0")/.."

VADER_DIR="${VADER_DIR:-test/vader.vim}"

# Auto-download vader.vim if not present.
if [ ! -d "$VADER_DIR" ]; then
  echo "Downloading vader.vim..."
  git clone --depth 1 https://github.com/junegunn/vader.vim.git "$VADER_DIR"
fi

# Determine which editor to use.  Prefer nvim (headless mode is reliable).
if command -v nvim &>/dev/null; then
  VIM_CMD=(nvim --headless)
elif command -v vim &>/dev/null; then
  # Vim requires -es (silent ex mode) for non-interactive use.
  VIM_CMD=(vim -N -es)
else
  echo "error: neither nvim nor vim found on PATH" >&2
  exit 1
fi

# Build a minimal vimrc that loads our plugin and vader.
VIMRC="$(mktemp)"
trap 'rm -f "$VIMRC"' EXIT

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

echo "Running tests with: ${VIM_CMD[*]}"
echo "Tests: $TESTS"

# shellcheck disable=SC2086
"${VIM_CMD[@]}" -u "$VIMRC" -c "Vader! $TESTS"
