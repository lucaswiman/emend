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

" Auto-start the editor-server when a Python file is opened.
let g:emend_auto_start = get(g:, 'emend_auto_start', 1)

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

" Search with cross-project module mappings included.
command! -nargs=1 EmendSearchMap call emend#ui#search(<q-args>, {'include_map': v:true})

" Show symbols in the current file.
command! -nargs=0 EmendOutline call emend#file_symbols(expand('%:p'))

" Find references to the symbol under cursor (requires indexed project).
command! -nargs=? EmendRefs call emend#references(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Go to the cross-service mapping target for the symbol under cursor.
command! -nargs=? EmendGoto call emend#goto(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Rename the symbol under cursor across the project.
command! -nargs=? EmendRename call emend#rename(<q-args>)

" Resolve a module to its external repo / local path.
command! -nargs=1 EmendResolve call emend#module_resolve(<q-args>)

" Add a module mapping for an import on the current line.
command! -nargs=? EmendModuleMap call emend#module_map(<q-args>)

" Server lifecycle.
command! -nargs=0 EmendStart call emend#start()
command! -nargs=0 EmendStop call emend#stop()

" Index status and reindex.
command! -nargs=0 EmendStatus call emend#status()
command! -nargs=0 EmendReindex call emend#reindex()

" Pattern-based find and replace across the project.
command! -nargs=0 EmendReplace call emend#replace_prompt()

" Move symbol under cursor to another file.
command! -nargs=? EmendMove call emend#move(<q-args>)

" Find call sites for the symbol under cursor.
command! -nargs=? EmendCallers call emend#callers(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Find functions called by the symbol under cursor.
command! -nargs=? EmendCallees call emend#callees(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Show type information for the symbol under cursor.
command! -nargs=0 EmendTypeHover call emend#type_hover()

" Show symbols in the current file with interactive filtering.
command! -nargs=0 EmendOutlineFilter call emend#outline_filter()

" Show transitive impact of changing the symbol under cursor.
command! -nargs=? EmendImpact call emend#impact(<q-args> ==# '' ? expand('<cword>') : <q-args>)

" Check whether the current buffer duplicates code elsewhere in the project.
command! -nargs=0 EmendCheckDupes call emend#check_duplicates()

" Hot buffer protocol — keep the server in sync with unsaved buffer contents.
command! -nargs=0 EmendHotBuffersEnable call emend#enable_hot_buffers()
command! -nargs=0 EmendHotBuffersDisable call emend#disable_hot_buffers()

" ---------------------------------------------------------------------------
" Auto-start server (opt-out via g:emend_auto_start = 0)
" ---------------------------------------------------------------------------

if g:emend_auto_start
  augroup emend_auto_start
    autocmd!
    " Start the server the first time a Python file is opened.
    autocmd FileType python ++once call emend#start()
  augroup END
endif

" ---------------------------------------------------------------------------
" Default mappings (opt-in via g:emend_default_mappings)
" ---------------------------------------------------------------------------

" Global C-Space completion via emend (works in any buffer).
inoremap <silent> <C-Space> <Cmd>call emend#complete_at_cursor()<CR>
inoremap <silent> <C-@> <Cmd>call emend#complete_at_cursor()<CR>

if get(g:, 'emend_default_mappings', 0)
  call emend#map_if_free('n', '<Leader>es', '<Cmd>Emend<CR>')
  call emend#map_if_free('n', '<Leader>eo', '<Cmd>EmendOutline<CR>')
  call emend#map_if_free('n', '<Leader>er', '<Cmd>EmendRefs<CR>')
  call emend#map_if_free('n', '<Leader>eg', '<Cmd>EmendGoto<CR>')
  call emend#map_if_free('n', '<Leader>eR', '<Cmd>EmendReplace<CR>')
  call emend#map_if_free('n', '<Leader>em', '<Cmd>EmendMove<CR>')
  call emend#map_if_free('n', '<Leader>ec', '<Cmd>EmendCallers<CR>')
  call emend#map_if_free('n', '<Leader>eC', '<Cmd>EmendCallees<CR>')
  call emend#map_if_free('n', '<Leader>et', '<Cmd>EmendTypeHover<CR>')
  call emend#map_if_free('n', '<Leader>eO', '<Cmd>EmendOutlineFilter<CR>')
endif
