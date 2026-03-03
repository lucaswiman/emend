" emend.vim — Plugin entry point.
"
" Provides :Emend* commands for searching Python code via the emend
" editor-server (JSON-RPC over stdio).
"
" Install with vim-plug:
"   Plug 'lucaswiman/emend', { 'rtp': 'vim' }
"
" Or point to a local checkout:
"   Plug '~/src/emend', { 'rtp': 'vim' }

if exists('g:loaded_emend')
  finish
endif
let g:loaded_emend = 1

" ---------------------------------------------------------------------------
" Configuration (must live here, not in autoload, so it's set before any call)
" ---------------------------------------------------------------------------

" Path to the emend executable.  When empty the plugin auto-detects:
"   1. uv tool path     (uv tool install emend)
"   2. $PATH lookup
let g:emend_command = get(g:, 'emend_command', '')

" Project root override. Empty = auto-detect (cwd).
let g:emend_project_root = get(g:, 'emend_project_root', '')

" Maximum results per search.
let g:emend_limit = get(g:, 'emend_limit', 50)

" Preview window height (percentage of editor height).
let g:emend_preview_height = get(g:, 'emend_preview_height', 60)

" ---------------------------------------------------------------------------
" Commands
" ---------------------------------------------------------------------------

" Interactive search prompt (or search a given query directly).
command! -nargs=? Emend call emend#ui#prompt(<q-args>)

" Search with explicit query (no prompt).
command! -nargs=1 EmendSearch call emend#ui#search(<q-args>)

" Show symbols in the current file.
command! -nargs=0 EmendOutline call emend#file_symbols(expand('%:p'))

" Find references to the symbol under cursor (requires indexed project).
command! -nargs=? EmendRefs call emend#references(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Server lifecycle.
command! -nargs=0 EmendStart call emend#start()
command! -nargs=0 EmendStop call emend#stop()

" Index status and reindex.
command! -nargs=0 EmendStatus call emend#status()
command! -nargs=0 EmendReindex call emend#reindex()

" ---------------------------------------------------------------------------
" Default mappings (opt-in via g:emend_default_mappings)
" ---------------------------------------------------------------------------

if get(g:, 'emend_default_mappings', 0)
  nnoremap <silent> <Leader>es <Cmd>Emend<CR>
  nnoremap <silent> <Leader>eo <Cmd>EmendOutline<CR>
  nnoremap <silent> <Leader>er <Cmd>EmendRefs<CR>
endif
