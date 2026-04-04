# emend.vim

A Vim/Neovim plugin for searching and navigating code with [emend](https://github.com/lucaswiman/emend).

## Features

- **Interactive search**: symbols, code patterns (`$X`), and selectors (`file.py::Class`)
- **Split-pane UI**: navigable result list + file preview with syntax highlighting
- **Definition jumps**: `:EmendGoto` resolves locals, imports, and mapped cross-project symbols
- **Async RPC**: non-blocking communication with `emend editor-server` over stdio
- **Auto cache warming**: builds the index on first use with progress display
- **Works in Vim 8+ and Neovim**

## Installation

### 1. Install emend

```bash
# Recommended: install as a uv tool
uv tool install emend

# Or in a virtualenv
pip install emend
```

### 2. Install the plugin

**vim-plug:**
```vim
Plug 'lucaswiman/emend', { 'rtp': 'vim' }
```

**packer.nvim:**
```lua
use { 'lucaswiman/emend', rtp = 'vim' }
```

**lazy.nvim:**
```lua
{ 'lucaswiman/emend',
  config = function()
    vim.opt.rtp:append(vim.fn.stdpath('data') .. '/lazy/emend/vim')
  end }
```

**Local development checkout:**
```vim
Plug '~/src/emend', { 'rtp': 'vim' }
```

## Usage

```vim
:Emend                    " Open search prompt
:Emend parse              " Search for 'parse' immediately
:EmendSearch print($X)    " Pattern search
:EmendSearch file.py::Cls " Selector search
:EmendOutline             " Symbols in current file
:EmendGoto                " Definition jump for symbol under cursor
:EmendRefs                " References to word under cursor
:EmendStatus              " Index statistics
```

### Goto behavior

`:EmendGoto` prefers a local definition first. If the symbol is imported,
emend resolves the imported definition instead of stopping on the import line.
Cross-file jumps open in a new tab. If the destination buffer is already
loaded, emend reuses that buffer instead of reopening the file. Cross-file
jumps are opened from a tab cloned from the current window so Vim's normal
`<C-o>` / `<C-i>` jump navigation continues to work.

### Search UI navigation

| Key        | Action                    |
|------------|---------------------------|
| `j` / `↓`  | Move down                 |
| `k` / `↑`  | Move up                   |
| `Ctrl-d`   | Page down (10 items)      |
| `Ctrl-u`   | Page up (10 items)        |
| `gg`       | First result              |
| `G`        | Last result               |
| `Enter`    | Open file at match        |
| `q` / `Esc`| Close                     |
| `/`        | New search                |

## Configuration

```vim
" Path to emend executable (auto-detected by default)
let g:emend_command = ''

" Project root (defaults to cwd)
let g:emend_project_root = ''

" Max search results (default: 50)
let g:emend_limit = 50

" UI height as % of editor (default: 60)
let g:emend_preview_height = 60

" Enable default <Leader> mappings (default: 0)
let g:emend_default_mappings = 1
"   <Leader>es → :Emend
"   <Leader>eo → :EmendOutline
"   <Leader>er → :EmendRefs
"   <Leader>eg → :EmendGoto
```

## Architecture

The plugin communicates with `emend editor-server` via newline-delimited
JSON-RPC over stdin/stdout pipes.  The server process stays warm across
searches, keeping the SQLite index + FTS5 trigram table in memory for
sub-5ms symbol lookups.

```
 Vim/Neovim                     emend editor-server
 ┌──────────┐   stdin (JSON)    ┌─────────────────┐
 │ emend.vim │ ───────────────▶ │ EditorSearch     │
 │           │ ◀─────────────── │   Engine         │
 └──────────┘   stdout (JSON)   │ (SQLite + FTS5)  │
                                └─────────────────┘
```

On first use, if no index exists, the plugin runs `emend index -vv` and
displays the output in the preview pane with an elapsed-time ticker.

## Testing

### Vim plugin tests (vader.vim)

```bash
cd vim
./test/run_tests.sh
```

### Python RPC integration tests

```bash
make test TESTS=tests/test_emend/test_vim_rpc.py
```

## License

MPL-2.0
