" emend/ui.vim — Split-pane search UI for emend.
"
" Inspired by fzf.vim: a two-pane layout with a navigable result list on the
" left and a file/code preview on the right.  Keyboard driven with arrow keys,
" j/k, Enter to jump, Esc/q to close.

" ---------------------------------------------------------------------------
" State
" ---------------------------------------------------------------------------

let s:list_buf = -1
let s:preview_buf = -1
let s:input_buf = -1
let s:list_win = -1
let s:preview_win = -1
let s:input_win = -1
let s:results = []        " list of result dicts
let s:selected = 0        " currently highlighted index
let s:query = ''
let s:last_result = {}    " full result dict (mode, elapsed_ms, etc.)
let s:all_line_hl = []    " per-line highlight ranges for re-application
let s:is_interactive = 0
let s:search_timer = -1
let s:focus = 'list'

" Namespace for extmark highlights in the result list.
let s:ns_id = -1

function! s:get_ns() abort
  if s:ns_id < 0
    let s:ns_id = nvim_create_namespace('emend_ui')
  endif
  return s:ns_id
endfunction

" Highlight groups for color-coded results.
highlight default EmendKindClass guifg=#ffc600 gui=bold ctermfg=220 cterm=bold
highlight default EmendKindFunc guifg=#3ad900 ctermfg=76
highlight default EmendKindMethod guifg=#80fcff ctermfg=123
highlight default EmendKindAsync guifg=#cc99ff ctermfg=183
highlight default EmendKindVar guifg=#ff9d00 ctermfg=208
highlight default EmendKindFile guifg=#ffffff ctermfg=15
highlight default EmendName guifg=#e1efff gui=bold ctermfg=255 cterm=bold
highlight default EmendFilePath guifg=#7e8a93 ctermfg=102
highlight default EmendSelected guibg=#1d4e7a gui=bold ctermbg=24 cterm=bold
highlight default EmendHeader guifg=#7e8a93 gui=italic ctermfg=102

let s:KIND_HIGHLIGHTS = {
      \ 'class': 'EmendKindClass',
      \ 'function': 'EmendKindFunc',
      \ 'method': 'EmendKindMethod',
      \ 'async_function': 'EmendKindAsync',
      \ 'async_method': 'EmendKindAsync',
      \ 'variable': 'EmendKindVar',
      \ 'file': 'EmendKindFile',
      \ }

" Kind abbreviations (hoisted to avoid per-call allocation).
let s:KIND_ICONS = {
      \ 'class': 'C',
      \ 'function': 'f',
      \ 'method': 'm',
      \ 'async_function': 'af',
      \ 'async_method': 'am',
      \ 'variable': 'v',
      \ 'file': 'F',
      \ }

" ---------------------------------------------------------------------------
" Public entry — prompt and search
" ---------------------------------------------------------------------------

function! emend#ui#prompt(...) abort
  if a:0 > 0 && a:1 !=# ''
    call emend#ui#search(a:1)
  else
    call emend#ui#interactive()
  endif
endfunction

function! emend#ui#interactive() abort
  let s:is_interactive = 1
  let s:query = ''
  let s:results = []
  let s:selected = 0
  
  call s:ensure_ui_open()
  
  " In interactive mode, start focus in the input window
  if s:input_win >= 0
    call win_gotoid(s:input_win)
    startinsert
  endif
endfunction

function! emend#ui#search(query, ...) abort
  let s:query = a:query
  let s:is_interactive = 0
  let l:params = a:0 > 0 ? a:1 : {}
  call emend#search(a:query, l:params)
endfunction

" ---------------------------------------------------------------------------
" Indexing-complete notification handler
" ---------------------------------------------------------------------------

function! emend#ui#on_indexing_complete() abort
  " If the search UI is open and we have a query, auto-refresh results
  " now that the index is fresh.
  if s:ui_is_open() && s:query !=# ''
    call emend#search(s:query)
  endif
endfunction

" ---------------------------------------------------------------------------
" Callback from emend#search
" ---------------------------------------------------------------------------

function! emend#ui#on_search_result(result) abort
  if has_key(a:result, 'error')
    let l:msg = get(get(a:result, 'error', {}), 'message', '')
    if l:msg =~# 'no such table\|symbol_index\|unable to open'
      call s:show_cache_warming(s:query)
      return
    endif
    echohl ErrorMsg
    echom 'emend: ' . l:msg
    echohl None
    return
  endif

  let s:last_result = a:result
  let s:results = get(a:result, 'items', [])
  let s:selected = 0
  
  if s:query !=# ''
    call s:save_history(s:query)
  endif

  if s:ui_is_open()
    call s:render_list()
    call s:render_preview()
    return
  endif

  if empty(s:results) && !s:is_interactive
    echo printf('emend: no results for "%s" [%gms]',
          \ s:query, get(a:result, 'elapsed_ms', 0))
    return
  endif

  call s:open_ui()
endfunction

" ---------------------------------------------------------------------------
" Cache warming display
" ---------------------------------------------------------------------------

let s:cache_timer = -1
let s:cache_start = 0
let s:cache_job = v:null

function! s:show_cache_warming(query) abort
  call s:open_ui()

  call s:set_buf_lines(s:list_buf, [
        \ '  emend — Warming cache...',
        \ '',
        \ '  The index has not been built yet.',
        \ '  Running: emend index -vv',
        \ '',
        \ '  This only needs to happen once.',
        \ '  Subsequent searches will be fast.',
        \ '',
        \ ])

  call s:set_buf_lines(s:preview_buf, ['  Waiting for index to complete...'])

  let s:cache_start = reltime()

  let l:emend = emend#find_executable()
  if l:emend ==# ''
    let l:emend = 'emend'
  endif
  let l:root = g:emend_project_root !=# '' ? g:emend_project_root : getcwd()

  if has('nvim')
    let s:cache_job = jobstart([l:emend, 'index', l:root, '-vv'], {
          \ 'on_stdout': {id, data, ev -> s:append_cache_output(data)},
          \ 'on_stderr': {id, data, ev -> s:append_cache_output(data)},
          \ 'on_exit':   function('s:on_cache_exit', [a:query]),
          \ 'stdout_buffered': v:false,
          \ })
  else
    let s:cache_job = job_start([l:emend, 'index', l:root, '-vv'], {
          \ 'out_cb':  {ch, msg -> s:append_cache_output(msg)},
          \ 'err_cb':  {ch, msg -> s:append_cache_output(msg)},
          \ 'exit_cb': {job, status -> s:on_cache_exit(a:query, job, status)},
          \ })
  endif

  let s:cache_timer = timer_start(500, function('s:update_cache_ticker'), {'repeat': -1})
endfunction

function! s:append_cache_output(data) abort
  if s:preview_buf < 0 || !bufexists(s:preview_buf)
    return
  endif
  let l:lines = type(a:data) == v:t_list ? a:data : split(a:data, "\n")
  call s:append_buf_lines(s:preview_buf, l:lines)
endfunction

function! s:on_cache_exit(query, job_id, exit_code, ...) abort
  if s:cache_timer >= 0
    call timer_stop(s:cache_timer)
    let s:cache_timer = -1
  endif
  let s:cache_job = v:null

  if s:list_buf >= 0 && bufexists(s:list_buf)
    call s:set_buf_lines(s:list_buf, [
          \ '  emend — Cache ready!',
          \ '',
          \ '  Searching for: ' . a:query,
          \ ])
  endif

  " Restart the server (it may not have been running or the old one had no index).
  call emend#stop()
  call timer_start(300, {_ -> s:retry_search(a:query)})
endfunction

function! s:retry_search(query) abort
  call emend#start()
  call timer_start(500, {_ -> s:wait_and_search(a:query, 0)})
endfunction

function! s:wait_and_search(query, attempt) abort
  if emend#is_ready()
    call emend#search(a:query)
    return
  endif
  if a:attempt < 20
    call timer_start(250, {_ -> s:wait_and_search(a:query, a:attempt + 1)})
  else
    if s:list_buf >= 0 && bufexists(s:list_buf)
      call s:set_buf_lines(s:list_buf, [
            \ '  emend — Server did not become ready.',
            \ '  Try :EmendStart and search again.',
            \ ])
    endif
  endif
endfunction

function! s:update_cache_ticker(timer) abort
  if s:list_buf < 0 || !bufexists(s:list_buf)
    call timer_stop(a:timer)
    return
  endif
  let l:elapsed = reltimefloat(reltime(s:cache_start))
  let l:secs = float2nr(l:elapsed)
  let l:ticks = repeat('.', (l:secs % 3) + 1)
  call s:set_buf_line(s:list_buf, 0,
        \ '  emend — Warming cache' . l:ticks . ' (' . l:secs . 's)')
endfunction

" ---------------------------------------------------------------------------
" Window / buffer management
" ---------------------------------------------------------------------------

function! s:ui_is_open() abort
  if s:list_win < 0 || s:preview_win < 0
    return 0
  endif
  if has('nvim')
    return nvim_win_is_valid(s:list_win) && nvim_win_is_valid(s:preview_win)
  else
    return win_id2win(s:list_win) > 0 && win_id2win(s:preview_win) > 0
  endif
endfunction

function! s:ensure_ui_open() abort
  if s:ui_is_open()
    return
  endif
  call s:open_ui()
endfunction

function! s:open_ui() abort
  let l:interactive = s:is_interactive
  call s:close_ui_silent()
  let s:is_interactive = l:interactive

  let l:total_h = &lines
  let l:total_w = &columns
  let l:height = max([10, l:total_h * g:emend_preview_height / 100])
  let l:list_w = max([30, l:total_w * 35 / 100])
  let l:preview_w = l:total_w - l:list_w - 3

  let s:list_buf = s:create_scratch_buf('emend://results')
  let s:preview_buf = s:create_scratch_buf('emend://preview')
  if s:is_interactive
    let s:input_buf = s:create_scratch_buf('emend://input')
  endif

  if has('nvim')
    call s:open_nvim_float(l:height, l:list_w, l:preview_w)
  else
    call s:open_vim_split(l:height, l:list_w)
  endif

  if !empty(s:results)
    call s:render_list()
    call s:render_preview()
  endif

  call s:setup_keymaps()
endfunction

function! s:create_scratch_buf(name) abort
  let l:buf = bufnr(a:name, 1)
  call setbufvar(l:buf, '&buftype', 'nofile')
  call setbufvar(l:buf, '&bufhidden', 'wipe')
  call setbufvar(l:buf, '&buflisted', 0)
  call setbufvar(l:buf, '&swapfile', 0)
  call setbufvar(l:buf, '&modifiable', 1)
  return l:buf
endfunction

function! s:open_nvim_float(height, list_w, preview_w) abort
  let l:row = max([1, (&lines - a:height) / 2])
  let l:col = max([1, (&columns - a:list_w - a:preview_w - 3) / 2])
  
  " If interactive, shift down slightly to make room for input box at top
  if s:is_interactive
    let l:row = max([4, l:row]) 
  endif

  let l:opts = {
        \ 'relative': 'editor',
        \ 'row': l:row,
        \ 'col': l:col,
        \ 'width': a:list_w,
        \ 'height': a:height,
        \ 'style': 'minimal',
        \ 'border': 'rounded',
        \ }
  if has('nvim-0.9')
    let l:opts.title = ' results '
    let l:opts.title_pos = 'center'
  endif

  let s:list_win = nvim_open_win(s:list_buf, v:true, l:opts)
  call nvim_win_set_option(s:list_win, 'winhighlight', 'Normal:Normal,FloatBorder:FloatBorder,CursorLine:EmendSelected')

  let l:p_opts = {
        \ 'relative': 'editor',
        \ 'row': l:row,
        \ 'col': l:col + a:list_w + 2,
        \ 'width': a:preview_w,
        \ 'height': a:height,
        \ 'style': 'minimal',
        \ 'border': 'rounded',
        \ }
  if has('nvim-0.9')
    let l:p_opts.title = ' preview '
    let l:p_opts.title_pos = 'center'
  endif

  let s:preview_win = nvim_open_win(s:preview_buf, v:false, l:p_opts)
  call nvim_win_set_option(s:preview_win, 'winhighlight', 'Normal:Normal,FloatBorder:FloatBorder')

  call nvim_win_set_option(s:list_win, 'cursorline', v:true)
  call nvim_win_set_option(s:list_win, 'number', v:false)
  call nvim_win_set_option(s:preview_win, 'number', v:true)
  call nvim_win_set_option(s:preview_win, 'wrap', v:false)
  
  if s:is_interactive
    let l:i_opts = {
          \ 'relative': 'editor',
          \ 'row': l:row - 3,
          \ 'col': l:col,
          \ 'width': a:list_w + a:preview_w + 2,
          \ 'height': 1,
          \ 'style': 'minimal',
          \ 'border': 'rounded',
          \ }
    if has('nvim-0.9')
      let l:i_opts.title = ' emend search '
      let l:i_opts.title_pos = 'left'
    endif
    let s:input_win = nvim_open_win(s:input_buf, v:true, l:i_opts)
    call nvim_win_set_option(s:input_win, 'winhighlight', 'Normal:Normal,FloatBorder:FloatBorder')
    call nvim_win_set_option(s:input_win, 'number', v:false)
  endif
endfunction

function! s:open_vim_split(height, list_w) abort
  execute 'botright ' . a:height . 'new'
  let s:list_win = win_getid()
  execute 'buffer ' . s:list_buf
  setlocal cursorline nonumber norelativenumber nowrap

  execute 'vertical rightbelow new'
  let s:preview_win = win_getid()
  execute 'buffer ' . s:preview_buf
  setlocal number nowrap
  
  if s:is_interactive
    " In Vim split mode, input is tricky, for now just use a split above list
    call win_gotoid(s:list_win)
    leftabove 1new
    let s:input_win = win_getid()
    execute 'buffer ' . s:input_buf
    setlocal nonumber norelativenumber nowrap
  endif

  call win_gotoid(s:list_win)
  execute 'vertical resize ' . a:list_w
endfunction

" Close a single window (Vim or Neovim).
function! s:close_win(win_id) abort
  if a:win_id < 0
    return
  endif
  try
    if has('nvim')
      if nvim_win_is_valid(a:win_id)
        call nvim_win_close(a:win_id, v:true)
      endif
    else
      let l:winnr = win_id2win(a:win_id)
      if l:winnr > 0
        execute l:winnr . 'wincmd c'
      endif
    endif
  catch
  endtry
endfunction

function! s:close_ui() abort
  call s:close_ui_silent()
  let s:is_interactive = 0
endfunction

function! s:close_ui_silent() abort
  if s:cache_timer >= 0
    call timer_stop(s:cache_timer)
    let s:cache_timer = -1
  endif
  
  if s:search_timer >= 0
    call timer_stop(s:search_timer)
    let s:search_timer = -1
  endif

  " Kill any running cache-warming job.
  if s:cache_job isnot v:null
    try
      if has('nvim')
        call jobstop(s:cache_job)
      else
        call job_stop(s:cache_job, 'kill')
      endif
    catch
    endtry
    let s:cache_job = v:null
  endif

  call s:close_win(s:preview_win)
  let s:preview_win = -1

  call s:close_win(s:list_win)
  let s:list_win = -1
  
  call s:close_win(s:input_win)
  let s:input_win = -1

  let s:list_buf = -1
  let s:preview_buf = -1
  let s:input_buf = -1
endfunction

" ---------------------------------------------------------------------------
" Keymaps & Events
" ---------------------------------------------------------------------------

function! s:setup_keymaps() abort
  let l:orig_win = win_getid()

  " List buffer maps
  if s:list_win >= 0
    call win_gotoid(s:list_win)
    call s:map_current_buf('n', '<CR>',      '<Cmd>call emend#ui#accept()<CR>')
    call s:map_current_buf('n', '<Esc>',     '<Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', 'q',         '<Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', 'j',         '<Cmd>call emend#ui#move(1)<CR>')
    call s:map_current_buf('n', 'k',         '<Cmd>call emend#ui#move(-1)<CR>')
    call s:map_current_buf('n', '<Down>',    '<Cmd>call emend#ui#move(1)<CR>')
    call s:map_current_buf('n', '<Up>',      '<Cmd>call emend#ui#move(-1)<CR>')
    call s:map_current_buf('n', '<C-d>',     '<Cmd>call emend#ui#move(10)<CR>')
    call s:map_current_buf('n', '<C-u>',     '<Cmd>call emend#ui#move(-10)<CR>')
    call s:map_current_buf('n', 'gg',        '<Cmd>call emend#ui#goto_first()<CR>')
    call s:map_current_buf('n', 'G',         '<Cmd>call emend#ui#goto_last()<CR>')
    call s:map_current_buf('n', '/',         '<Cmd>call emend#ui#new_search()<CR>')
    call s:map_current_buf('n', '<Tab>',     '<Cmd>call emend#ui#toggle_focus()<CR>')
    call s:map_current_buf('n', '<C-q>',     '<Cmd>call emend#ui#send_to_quickfix()<CR>')

    " Result actions
    call s:map_current_buf('n', 'r',         '<Cmd>call emend#ui#action_rename()<CR>')
    call s:map_current_buf('n', 'R',         '<Cmd>call emend#ui#action_refs()<CR>')
    call s:map_current_buf('n', 'c',         '<Cmd>call emend#ui#action_callers()<CR>')
    call s:map_current_buf('n', 'C',         '<Cmd>call emend#ui#action_callees()<CR>')
    call s:map_current_buf('n', 't',         '<Cmd>call emend#ui#action_type()<CR>')
    call s:map_current_buf('n', 'm',         '<Cmd>call emend#ui#action_move()<CR>')
  endif

  " Preview buffer maps
  if s:preview_win >= 0
    call win_gotoid(s:preview_win)
    call s:map_current_buf('n', '<Tab>',     '<Cmd>call emend#ui#toggle_focus()<CR>')
    call s:map_current_buf('n', '<Esc>',     '<Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', 'q',         '<Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', '<CR>',      '<Cmd>call emend#ui#accept()<CR>')
  endif

  " Input buffer maps (if interactive)
  if s:input_win >= 0
    call win_gotoid(s:input_win)
    call s:map_current_buf('i', '<CR>',      '<Esc><Cmd>call emend#ui#accept()<CR>')
    call s:map_current_buf('n', '<CR>',      '<Cmd>call emend#ui#accept()<CR>')
    call s:map_current_buf('i', '<Esc>',     '<Esc><Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', '<Esc>',     '<Cmd>call emend#ui#close()<CR>')
    call s:map_current_buf('n', 'q',         '<Cmd>call emend#ui#close()<CR>')
    
    " Navigation from input buffer
    call s:map_current_buf('i', '<C-n>',     '<Cmd>call emend#ui#move(1)<CR>')
    call s:map_current_buf('i', '<C-p>',     '<Cmd>call emend#ui#move(-1)<CR>')
    call s:map_current_buf('i', '<Down>',    '<Cmd>call emend#ui#move(1)<CR>')
    call s:map_current_buf('i', '<Up>',      '<Cmd>call emend#ui#move(-1)<CR>')
    call s:map_current_buf('i', '<Tab>',     '<Esc><Cmd>call emend#ui#toggle_focus()<CR>')
    call s:map_current_buf('n', '<Tab>',     '<Cmd>call emend#ui#toggle_focus()<CR>')
    call s:map_current_buf('i', '<C-q>',     '<Esc><Cmd>call emend#ui#send_to_quickfix()<CR>')
    call s:map_current_buf('i', '<C-r>',     '<Cmd>call emend#ui#history(1)<CR>')
    call s:map_current_buf('i', '<C-f>',     '<Cmd>call emend#ui#history(-1)<CR>')
    call s:map_current_buf('i', '<C-Space>', '<Cmd>call emend#ui#complete()<CR>')

    " Trigger search on change
    augroup emend_input
      autocmd! * <buffer>
      autocmd TextChangedI <buffer> call emend#ui#on_input_change()
      autocmd TextChanged  <buffer> call emend#ui#on_input_change()
    augroup END
  endif

  call win_gotoid(l:orig_win)
endfunction

function! emend#ui#on_input_change() abort
  if s:input_buf < 0 || !bufexists(s:input_buf)
    return
  endif
  let l:lines = getbufline(s:input_buf, 1)
  if empty(l:lines)
    return
  endif
  let l:query = l:lines[0]
  if l:query ==# s:query
    return
  endif
  
  let s:query = l:query
  
  if s:search_timer >= 0
    call timer_stop(s:search_timer)
    let s:search_timer = -1
  endif
  
  if l:query ==# ''
    let s:results = []
    call s:render_list()
    call s:render_preview()
    return
  endif
  
  let s:search_timer = timer_start(100, {t -> s:trigger_search()})
endfunction

function! s:trigger_search() abort
  let s:search_timer = -1
  if s:is_interactive && s:query !=# ''
    call emend#search(s:query)
  endif
endfunction

function! s:map_current_buf(mode, lhs, rhs) abort
  if has('nvim')
    let l:buf = nvim_get_current_buf()
    call nvim_buf_set_keymap(l:buf, a:mode, a:lhs, a:rhs,
          \ {'noremap': v:true, 'silent': v:true, 'nowait': v:true})
  else
    execute a:mode . 'noremap <buffer> <silent> <nowait> ' . a:lhs . ' ' . a:rhs
  endif
endfunction

" ---------------------------------------------------------------------------
" Navigation
" ---------------------------------------------------------------------------

function! emend#ui#move(delta) abort
  let l:old = s:selected
  let l:new = max([0, min([s:selected + a:delta, len(s:results) - 1])])
  if l:new == l:old
    return
  endif
  let s:selected = l:new
  call s:update_caret(l:old, l:new)
  call s:highlight_selected()
  call s:render_preview()
endfunction

function! s:update_caret(old_idx, new_idx) abort
  " Swap ' > ' / '   ' prefix on the two affected lines.
  " Result items start at line 3 (1-indexed), so 0-indexed buf lines are +2.
  let l:old_lnum = a:old_idx + 2
  let l:new_lnum = a:new_idx + 2

  let l:old_line = getbufline(s:list_buf, a:old_idx + 3)[0]
  let l:new_line = getbufline(s:list_buf, a:new_idx + 3)[0]

  if empty(l:old_line) || empty(l:new_line)
    return
  endif

  let l:old_text = '   ' . strpart(l:old_line, 3)
  let l:new_text = ' > ' . strpart(l:new_line, 3)

  call s:set_buf_line(s:list_buf, l:old_lnum, l:old_text)
  call s:set_buf_line(s:list_buf, l:new_lnum, l:new_text)

  " Re-apply extmark highlights for the two modified lines
  " (nvim_buf_set_lines destroys extmarks on replaced lines).
  if has('nvim') && !empty(s:all_line_hl)
    let l:ns = s:get_ns()
    for [l:idx, l:row] in [[a:old_idx, l:old_lnum], [a:new_idx, l:new_lnum]]
      if l:idx >= 0 && l:idx < len(s:all_line_hl)
        for [l:start, l:end, l:group] in s:all_line_hl[l:idx]
          call nvim_buf_set_extmark(s:list_buf, l:ns, l:row, l:start, {
                \ 'end_col': l:end,
                \ 'hl_group': l:group,
                \ })
        endfor
      endif
    endfor
  endif
endfunction

function! emend#ui#goto_first() abort
  call emend#ui#move(-len(s:results))
endfunction

function! emend#ui#goto_last() abort
  call emend#ui#move(len(s:results))
endfunction

function! emend#ui#accept() abort
  if empty(s:results) || s:selected >= len(s:results)
    return
  endif
  let l:item = s:results[s:selected]
  let l:file = get(l:item, 'file_path', '')
  let l:line = get(l:item, 'line', 1)

  call s:close_ui()

  if l:file !=# '' && filereadable(l:file)
    call emend#jump_to(l:file, l:line)
  elseif l:file !=# ''
    echohl WarningMsg
    echom 'emend: file not readable: ' . l:file
    echohl None
  endif
endfunction

function! emend#ui#close() abort
  call s:close_ui()
endfunction

function! emend#ui#new_search() abort
  call s:close_ui()
  call emend#ui#prompt()
endfunction

function! emend#ui#toggle_focus() abort
  if s:focus ==# 'list' && s:preview_win >= 0
    let s:focus = 'preview'
    call win_gotoid(s:preview_win)
  else
    let s:focus = 'list'
    if s:is_interactive && s:input_win >= 0
      call win_gotoid(s:input_win)
    elseif s:list_win >= 0
      call win_gotoid(s:list_win)
    endif
  endif
endfunction

" ---------------------------------------------------------------------------
" Rendering
" ---------------------------------------------------------------------------

function! s:render_list() abort
  if s:list_buf < 0 || !bufexists(s:list_buf)
    return
  endif
  let l:lines = []
  let l:elapsed = get(s:last_result, 'elapsed_ms', 0)
  let l:mode = get(s:last_result, 'mode', '?')
  let l:indexing = get(s:last_result, 'indexing', 0)

  let l:header = '  ' . len(s:results) . ' results'
  if l:elapsed > 0
    let l:header .= printf(' [%gms]', l:elapsed)
  endif
  let l:header .= '  (' . l:mode . ')'
  if l:indexing || emend#is_indexing()
    let l:header .= '  [indexing...]'
  endif
  call add(l:lines, l:header)
  call add(l:lines, repeat('─', 40))

  " Compute cwd prefix once for all items.
  let l:cwd = getcwd() . '/'

  let s:all_line_hl = []
  for l:i in range(len(s:results))
    let [l:text, l:hl] = s:format_result_line(s:results[l:i], l:i, l:cwd)
    call add(l:lines, l:text)
    call add(s:all_line_hl, l:hl)
  endfor

  call s:set_buf_lines(s:list_buf, l:lines)
  call s:apply_list_highlights(s:all_line_hl)
  call s:highlight_selected()

  " Update window title to reflect indexing state (Neovim ≥0.9).
  if has('nvim-0.9') && s:list_win >= 0 && nvim_win_is_valid(s:list_win)
    let l:title = (l:indexing || emend#is_indexing())
          \ ? ' results (indexing...) '
          \ : ' results '
    call nvim_win_set_config(s:list_win, {'title': l:title, 'title_pos': 'center'})
  endif
endfunction

function! s:format_result_line(item, index, cwd) abort
  let l:hl = []
  let l:name = get(a:item, 'name', get(a:item, 'matched_text', '?'))
  let l:kind = get(a:item, 'kind', '')
  let l:file = get(a:item, 'file_path', '')
  let l:line_no = get(a:item, 'line', '')
  let l:end_line = get(a:item, 'end_line', '')

  let l:prefix = a:index == s:selected ? ' > ' : '   '
  let l:text = l:prefix
  let l:col = len(l:prefix)

  if l:kind !=# ''
    let l:icon = get(s:KIND_ICONS, l:kind, '·') . ' '
    let l:hl_group = get(s:KIND_HIGHLIGHTS, l:kind, 'EmendKindFunc')
    call add(l:hl, [l:col, l:col + len(l:icon) - 1, l:hl_group])
    let l:text .= l:icon
    let l:col += len(l:icon)
  endif

  " Symbol name
  let l:name_start = l:col
  let l:text .= l:name
  let l:col += len(l:name)
  call add(l:hl, [l:name_start, l:col, 'EmendName'])

  if l:file !=# ''
    let l:short = l:file
    if stridx(l:short, a:cwd) == 0
      let l:short = strpart(l:short, len(a:cwd))
    endif
    let l:sep = '  '
    let l:text .= l:sep
    let l:col += len(l:sep)
    let l:loc = l:short
    if l:line_no !=# '' && l:line_no isnot v:null
      let l:loc .= ':' . l:line_no
      if l:end_line !=# '' && l:end_line isnot v:null && l:end_line != l:line_no
        let l:loc .= '-' . l:end_line
      endif
    endif
    call add(l:hl, [l:col, l:col + len(l:loc), 'EmendFilePath'])
    let l:text .= l:loc
  endif

  return [l:text, l:hl]
endfunction

function! s:apply_list_highlights(all_hl) abort
  if !has('nvim') || s:list_buf < 0 || !bufexists(s:list_buf)
    return
  endif
  let l:ns = s:get_ns()
  call nvim_buf_clear_namespace(s:list_buf, l:ns, 0, -1)

  " Header line highlight
  let l:hdr = getbufline(s:list_buf, 1)
  if !empty(l:hdr)
    call nvim_buf_set_extmark(s:list_buf, l:ns, 0, 0, {
          \ 'end_col': len(l:hdr[0]),
          \ 'hl_group': 'EmendHeader',
          \ })
  endif

  " Result line highlights (offset by 2 for header + separator)
  for l:i in range(len(a:all_hl))
    let l:row = l:i + 2
    for [l:start, l:end, l:group] in a:all_hl[l:i]
      call nvim_buf_set_extmark(s:list_buf, l:ns, l:row, l:start, {
            \ 'end_col': l:end,
            \ 'hl_group': l:group,
            \ })
    endfor
  endfor
endfunction

function! s:highlight_selected() abort
  if s:list_win < 0
    return
  endif

  " Result items start at line 3 (1-indexed, after header + separator).
  let l:target_line = s:selected + 3

  try
    if has('nvim')
      if nvim_win_is_valid(s:list_win)
        call nvim_win_set_cursor(s:list_win, [l:target_line, 0])
      endif
    else
      call win_execute(s:list_win, 'call cursor(' . l:target_line . ', 1)')
    endif
  catch
  endtry
endfunction

let s:preview_ft = ''

function! s:render_preview() abort
  if s:preview_buf < 0 || !bufexists(s:preview_buf)
    return
  endif

  if empty(s:results) || s:selected >= len(s:results)
    call s:set_buf_lines(s:preview_buf, [])
    call s:set_preview_title(' preview ')
    return
  endif

  let l:item = s:results[s:selected]
  let l:file = get(l:item, 'file_path', '')
  let l:start_line = get(l:item, 'line', 1)
  let l:end_line = get(l:item, 'end_line', l:start_line)
  let l:matched_text = get(l:item, 'matched_text', '')

  " Compute short file path for title.
  let l:cwd = getcwd() . '/'
  let l:short = l:file
  if stridx(l:short, l:cwd) == 0
    let l:short = strpart(l:short, len(l:cwd))
  endif

  " Pattern mode: show matched_text directly.
  if l:matched_text !=# ''
    let l:lines = ['  File: ' . l:short, '  Lines: ' . l:start_line . '-' . l:end_line, '']
    let l:lines += split(l:matched_text, "\n")
    call s:set_buf_lines(s:preview_buf, l:lines)
    call s:set_preview_ft('python')
    call s:set_preview_title(' ' . l:short . ' ')
    return
  endif

  if l:file ==# '' || !filereadable(l:file)
    call s:set_buf_lines(s:preview_buf, ['  (file not available)'])
    call s:set_preview_title(' preview ')
    return
  endif

  " Read full file for proper syntax highlighting context.
  let l:all_lines = readfile(l:file, '', 10000)
  if empty(l:all_lines)
    call s:set_buf_lines(s:preview_buf, ['  (empty file)'])
    return
  endif

  call s:set_buf_lines(s:preview_buf, l:all_lines)

  " Set filetype for syntax highlighting.
  let l:ext = fnamemodify(l:file, ':e')
  let l:ft = l:ext ==# 'py' ? 'python' : l:ext
  call s:set_preview_ft(l:ft)

  " Update preview title with file name.
  call s:set_preview_title(' ' . l:short . ' ')

  " Scroll to the match line, centered.
  try
    if has('nvim')
      if nvim_win_is_valid(s:preview_win)
        call nvim_win_set_cursor(s:preview_win, [max([1, l:start_line]), 0])
      endif
    else
      call win_execute(s:preview_win, 'call cursor(' . max([1, l:start_line]) . ', 1)')
    endif
    call win_execute(s:preview_win, 'normal! zz')
  catch
  endtry
endfunction

function! s:set_preview_title(title) abort
  if has('nvim-0.9') && s:preview_win >= 0 && nvim_win_is_valid(s:preview_win)
    call nvim_win_set_config(s:preview_win, {'title': a:title, 'title_pos': 'center'})
  endif
endfunction

function! s:set_preview_ft(ft) abort
  if a:ft !=# '' && a:ft !=# s:preview_ft
    let s:preview_ft = a:ft
    call setbufvar(s:preview_buf, '&filetype', a:ft)
  endif
endfunction

" ---------------------------------------------------------------------------
" Buffer helpers
" ---------------------------------------------------------------------------

function! s:set_buf_lines(buf, lines) abort
  if a:buf < 0 || !bufexists(a:buf)
    return
  endif
  call setbufvar(a:buf, '&modifiable', 1)
  if has('nvim')
    call nvim_buf_set_lines(a:buf, 0, -1, v:false, a:lines)
  else
    silent call deletebufline(a:buf, 1, '$')
    call setbufline(a:buf, 1, a:lines)
  endif
  call setbufvar(a:buf, '&modifiable', 0)
endfunction

function! s:set_buf_line(buf, lnum, text) abort
  if a:buf < 0 || !bufexists(a:buf)
    return
  endif
  call setbufvar(a:buf, '&modifiable', 1)
  if has('nvim')
    call nvim_buf_set_lines(a:buf, a:lnum, a:lnum + 1, v:false, [a:text])
  else
    call setbufline(a:buf, a:lnum + 1, a:text)
  endif
  call setbufvar(a:buf, '&modifiable', 0)
endfunction

function! s:append_buf_lines(buf, lines) abort
  if a:buf < 0 || !bufexists(a:buf)
    return
  endif
  let l:filtered = filter(copy(a:lines), 'v:val !=# ""')
  if empty(l:filtered)
    return
  endif
  call setbufvar(a:buf, '&modifiable', 1)
  if has('nvim')
    call nvim_buf_set_lines(a:buf, -1, -1, v:false, l:filtered)
  else
    call appendbufline(a:buf, '$', l:filtered)
  endif
  call setbufvar(a:buf, '&modifiable', 0)

  " Scroll to bottom.
  try
    if has('nvim')
      if nvim_win_is_valid(s:preview_win)
        let l:lc = nvim_buf_line_count(a:buf)
        call nvim_win_set_cursor(s:preview_win, [l:lc, 0])
      endif
    else
      call win_execute(s:preview_win, 'normal! G')
    endif
  catch
  endtry
endfunction

" ---------------------------------------------------------------------------
" Rename UI
" ---------------------------------------------------------------------------

let s:rename_state = {}

function! emend#ui#show_rename_preview(result, qn, new_name, file) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echohl ErrorMsg | echom 'emend: no changes for rename' | echohl None
    return
  endif

  let s:rename_state = {
        \ 'qn': a:qn,
        \ 'new_name': a:new_name,
        \ 'file': a:file,
        \ }

  let l:lines = [
        \ '  RENAME PREVIEW',
        \ '  Target: ' . a:qn,
        \ '  New name: ' . a:new_name,
        \ '  Press <CR> in the results list to apply, <Esc> to cancel.',
        \ '',
        \ ]

  for l:item in l:items
    let l:lines += ['--- ' . l:item.file_path, '']
    let l:lines += split(l:item.diff, "\n")
    call add(l:lines, '')
  endfor

  call s:open_ui()
  call s:set_buf_lines(s:list_buf, ['  Rename Preview', '  ' . len(l:items) . ' files changed', '', '  <CR> Apply', '  <Esc> Cancel'])
  call s:set_buf_lines(s:preview_buf, l:lines)
  call setbufvar(s:preview_buf, '&filetype', 'diff')
  
  " Map <CR> in list window to confirm rename
  call win_gotoid(s:list_win)
  nnoremap <buffer> <silent> <CR> <Cmd>call emend#ui#rename_confirm()<CR>
endfunction

function! emend#ui#rename_confirm() abort
  let l:state = s:rename_state
  call s:close_ui()
  echom 'emend: applying rename...'
  call emend#send('rename_apply', {
        \ 'qualified_name': l:state.qn,
        \ 'new_name': l:state.new_name,
        \ 'file': l:state.file,
        \ }, {res -> s:on_rename_apply(res)})
endfunction

function! s:on_rename_apply(result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:count = len(get(a:result, 'items', []))
  echom 'emend: renamed symbol in ' . l:count . ' files. Reloading...'
  checktime
endfunction

" ---------------------------------------------------------------------------
" Replace Preview UI
" ---------------------------------------------------------------------------

let s:replace_state = {}

function! emend#ui#show_replace_preview(result, pattern, replacement) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: no matches found for pattern'
    return
  endif

  let s:replace_state = {
        \ 'pattern': a:pattern,
        \ 'replacement': a:replacement,
        \ }

  let l:total_count = 0
  let l:lines = [
        \ '  REPLACE PREVIEW',
        \ '  Pattern: ' . a:pattern,
        \ '  Replace: ' . a:replacement,
        \ '  Press <CR> to apply, <Esc> to cancel.',
        \ '',
        \ ]

  for l:item in l:items
    let l:count = get(l:item, 'count', 0)
    let l:total_count += l:count
    let l:lines += ['--- ' . l:item.file_path . ' (' . l:count . ' replacements)', '']
    let l:lines += split(l:item.diff, "\n")
    call add(l:lines, '')
  endfor

  call s:open_ui()
  call s:set_buf_lines(s:list_buf, [
        \ '  Replace Preview',
        \ '  ' . l:total_count . ' replacements in ' . len(l:items) . ' files',
        \ '',
        \ '  <CR> Apply',
        \ '  <Esc> Cancel',
        \ ])
  call s:set_buf_lines(s:preview_buf, l:lines)
  call setbufvar(s:preview_buf, '&filetype', 'diff')

  " Map <CR> in list window to confirm replace
  call win_gotoid(s:list_win)
  nnoremap <buffer> <silent> <CR> <Cmd>call emend#ui#replace_confirm()<CR>
endfunction

function! emend#ui#replace_confirm() abort
  let l:state = s:replace_state
  call s:close_ui()
  echom 'emend: applying replacements...'
  call emend#send('replace_apply', {
        \ 'pattern': l:state.pattern,
        \ 'replacement': l:state.replacement,
        \ }, {res -> s:on_replace_apply(res)})
endfunction

function! s:on_replace_apply(result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  let l:total = 0
  for l:item in l:items
    let l:total += get(l:item, 'count', 0)
  endfor
  echom 'emend: replaced ' . l:total . ' matches in ' . len(l:items) . ' files. Reloading...'
  checktime
endfunction

" ---------------------------------------------------------------------------
" Move Preview UI
" ---------------------------------------------------------------------------

let s:move_state = {}

function! emend#ui#show_move_preview(result, qn, dest_file, file) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echohl ErrorMsg | echom 'emend: no changes for move' | echohl None
    return
  endif

  let s:move_state = {
        \ 'qn': a:qn,
        \ 'dest_file': a:dest_file,
        \ 'file': a:file,
        \ }

  let l:lines = [
        \ '  MOVE PREVIEW',
        \ '  Symbol: ' . a:qn,
        \ '  Destination: ' . a:dest_file,
        \ '  Press <CR> to apply, <Esc> to cancel.',
        \ '',
        \ ]

  for l:item in l:items
    let l:lines += ['--- ' . l:item.file_path, '']
    let l:lines += split(l:item.diff, "\n")
    call add(l:lines, '')
  endfor

  call s:open_ui()
  call s:set_buf_lines(s:list_buf, [
        \ '  Move Preview',
        \ '  ' . len(l:items) . ' files changed',
        \ '',
        \ '  <CR> Apply',
        \ '  <Esc> Cancel',
        \ ])
  call s:set_buf_lines(s:preview_buf, l:lines)
  call setbufvar(s:preview_buf, '&filetype', 'diff')

  " Map <CR> in list window to confirm move
  call win_gotoid(s:list_win)
  nnoremap <buffer> <silent> <CR> <Cmd>call emend#ui#move_confirm()<CR>
endfunction

function! emend#ui#move_confirm() abort
  let l:state = s:move_state
  call s:close_ui()
  echom 'emend: applying move...'
  call emend#send('move_apply', {
        \ 'qualified_name': l:state.qn,
        \ 'dest_file': l:state.dest_file,
        \ 'file': l:state.file,
        \ }, {res -> s:on_move_apply(res)})
endfunction

function! s:on_move_apply(result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:count = len(get(a:result, 'items', []))
  echom 'emend: moved symbol, updated ' . l:count . ' files. Reloading...'
  checktime
endfunction

" ---------------------------------------------------------------------------
" Search Result Actions
" ---------------------------------------------------------------------------

function! s:get_selected_item() abort
  if empty(s:results) || s:selected >= len(s:results)
    return {}
  endif
  return s:results[s:selected]
endfunction

function! emend#ui#action_rename() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:name = get(l:item, 'name', '')
  let l:file = get(l:item, 'file_path', '')
  let l:qn = get(l:item, 'qualified_name', l:name)
  if empty(l:name) | return | endif

  let l:new_name = input('Rename ' . l:name . ' to: ', l:name)
  if empty(l:new_name) || l:new_name ==# l:name
    return
  endif

  call emend#send('rename_preview', {
        \ 'qualified_name': l:qn,
        \ 'new_name': l:new_name,
        \ 'file': l:file,
        \ }, {res -> emend#ui#show_rename_preview(res, l:qn, l:new_name, l:file)})
endfunction

function! emend#ui#action_refs() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  if empty(l:qn) | return | endif
  call s:close_ui()
  call emend#references(l:qn)
endfunction

function! emend#ui#action_callers() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  let l:file = get(l:item, 'file_path', '')
  if empty(l:qn) | return | endif
  call s:close_ui()
  call emend#send('callers', {
        \ 'qualified_name': l:qn,
        \ 'file': l:file,
        \ }, function('emend#ui#on_search_result'))
endfunction

function! emend#ui#action_callees() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  let l:file = get(l:item, 'file_path', '')
  if empty(l:qn) | return | endif
  call s:close_ui()
  call emend#send('callees', {
        \ 'qualified_name': l:qn,
        \ 'file': l:file,
        \ }, function('emend#ui#on_search_result'))
endfunction

function! emend#ui#action_type() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:file = get(l:item, 'file_path', '')
  let l:line = get(l:item, 'line', 0)
  if empty(l:file) | return | endif
  call emend#send('types_at_cursor', {
        \ 'file': l:file,
        \ 'line': l:line,
        \ 'col': 0,
        \ }, function('s:on_action_type'))
endfunction

function! s:on_action_type(result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: no type information available'
    return
  endif
  " Show all types for the line in the echo area
  let l:parts = []
  for l:item in l:items
    call add(l:parts, get(l:item, 'name', '?') . ': ' . get(l:item, 'type', '?'))
  endfor
  echo join(l:parts, '  |  ')
endfunction

function! emend#ui#action_move() abort
  let l:item = s:get_selected_item()
  if empty(l:item) | return | endif
  let l:name = get(l:item, 'name', '')
  let l:file = get(l:item, 'file_path', '')
  let l:qn = get(l:item, 'qualified_name', l:name)
  if empty(l:name) | return | endif

  let l:dest = input('Move ' . l:name . ' to: ', '', 'file')
  if empty(l:dest)
    return
  endif

  call emend#send('move_preview', {
        \ 'qualified_name': l:qn,
        \ 'dest_file': l:dest,
        \ 'file': l:file,
        \ }, {res -> emend#ui#show_move_preview(res, l:qn, l:dest, l:file)})
endfunction

" ---------------------------------------------------------------------------
" Quickfix
" ---------------------------------------------------------------------------

function! emend#ui#send_to_quickfix() abort
  if empty(s:results)
    return
  endif
  let l:qf = []
  for l:item in s:results
    call add(l:qf, {
          \ 'filename': get(l:item, 'file_path', ''),
          \ 'lnum': get(l:item, 'line', 1),
          \ 'text': get(l:item, 'name', '') . ' [' . get(l:item, 'kind', '') . ']',
          \ })
  endfor
  call setqflist(l:qf, 'r')
  call s:close_ui()
  copen
endfunction

" ---------------------------------------------------------------------------
" Search History
" ---------------------------------------------------------------------------

let s:search_history = []
let s:history_idx = -1

function! s:save_history(query) abort
  if empty(a:query) || a:query =~# '^\s*$' | return | endif
  " Remove if already exists to move to top
  let l:idx = index(s:search_history, a:query)
  if l:idx >= 0
    call remove(s:search_history, l:idx)
  endif
  call insert(s:search_history, a:query, 0)
  if len(s:search_history) > 100
    call remove(s:search_history, 100, -1)
  endif
  let s:history_idx = -1
endfunction

function! emend#ui#history(delta) abort
  if empty(s:search_history) | return | endif
  let s:history_idx = max([0, min([s:history_idx + a:delta, len(s:search_history) - 1])])
  let l:query = s:search_history[s:history_idx]
  
  if s:input_buf >= 0 && bufexists(s:input_buf)
    call s:set_buf_lines(s:input_buf, [l:query])
    
    " Move cursor to end of line in input window
    if s:input_win >= 0 && nvim_win_is_valid(s:input_win)
      call win_gotoid(s:input_win)
      call cursor(1, len(l:query) + 1)
    endif

    " Trigger search
    let s:query = l:query
    call s:trigger_search()
  endif
endfunction

function! emend#ui#complete() abort
  if s:input_buf < 0 | return | endif
  let l:line = getbufline(s:input_buf, 1)[0]
  let l:col = col('.')
  " Prefix is everything before cursor
  let l:prefix = strpart(l:line, 0, l:col - 1)
  " Extract last word
  let l:prefix = matchstr(l:prefix, '\k*$')
  
  if empty(l:prefix) | return | endif
  
  call emend#send('complete', {'prefix': l:prefix}, {res -> s:on_complete(res, l:prefix)})
endfunction

function! s:on_complete(result, prefix) abort
  let l:items = get(a:result, 'items', [])
  if empty(l:items) | return | endif
  
  " Format for complete()
  let l:start_col = col('.') - len(a:prefix)
  call complete(l:start_col, l:items)
endfunction
