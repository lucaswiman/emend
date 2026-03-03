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
let s:list_win = -1
let s:preview_win = -1
let s:results = []        " list of result dicts
let s:selected = 0        " currently highlighted index
let s:query = ''
let s:last_result = {}    " full result dict (mode, elapsed_ms, etc.)

" Kind abbreviations (hoisted to avoid per-call allocation).
let s:KIND_ICONS = {
      \ 'class': 'C',
      \ 'function': 'f',
      \ 'method': 'm',
      \ 'async_function': 'af',
      \ 'async_method': 'am',
      \ 'variable': 'v',
      \ }

" ---------------------------------------------------------------------------
" Public entry — prompt and search
" ---------------------------------------------------------------------------

function! emend#ui#prompt(...) abort
  let l:initial = a:0 > 0 ? a:1 : ''
  let l:query = input('emend> ', l:initial)
  if l:query ==# ''
    return
  endif
  call emend#ui#search(l:query)
endfunction

function! emend#ui#search(query) abort
  let s:query = a:query
  if !emend#is_running()
    call emend#start()
  endif
  if !emend#is_ready()
    call s:show_cache_warming(a:query)
    return
  endif
  call emend#search(a:query)
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

  if empty(s:results)
    echo 'emend: no results for "' . s:query . '"'
          \ . ' [' . get(a:result, 'elapsed_ms', 0) . 'ms]'
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

function! s:open_ui() abort
  call s:close_ui_silent()

  let l:total_h = &lines
  let l:total_w = &columns
  let l:height = max([10, l:total_h * g:emend_preview_height / 100])
  let l:list_w = max([30, l:total_w * 35 / 100])
  let l:preview_w = l:total_w - l:list_w - 3

  let s:list_buf = s:create_scratch_buf('emend://results')
  let s:preview_buf = s:create_scratch_buf('emend://preview')

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

  let s:list_win = nvim_open_win(s:list_buf, v:true, {
        \ 'relative': 'editor',
        \ 'row': l:row,
        \ 'col': l:col,
        \ 'width': a:list_w,
        \ 'height': a:height,
        \ 'style': 'minimal',
        \ 'border': 'rounded',
        \ 'title': ' emend search ',
        \ 'title_pos': 'center',
        \ })

  let s:preview_win = nvim_open_win(s:preview_buf, v:false, {
        \ 'relative': 'editor',
        \ 'row': l:row,
        \ 'col': l:col + a:list_w + 2,
        \ 'width': a:preview_w,
        \ 'height': a:height,
        \ 'style': 'minimal',
        \ 'border': 'rounded',
        \ 'title': ' preview ',
        \ 'title_pos': 'center',
        \ })

  call nvim_win_set_option(s:list_win, 'cursorline', v:true)
  call nvim_win_set_option(s:list_win, 'number', v:false)
  call nvim_win_set_option(s:preview_win, 'number', v:true)
  call nvim_win_set_option(s:preview_win, 'wrap', v:false)
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
endfunction

function! s:close_ui_silent() abort
  if s:cache_timer >= 0
    call timer_stop(s:cache_timer)
    let s:cache_timer = -1
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

  let s:list_buf = -1
  let s:preview_buf = -1
endfunction

" ---------------------------------------------------------------------------
" Keymaps
" ---------------------------------------------------------------------------

function! s:setup_keymaps() abort
  let l:buf = s:list_buf

  call s:map_buf(l:buf, 'n', '<CR>',      '<Cmd>call emend#ui#accept()<CR>')
  call s:map_buf(l:buf, 'n', '<Esc>',     '<Cmd>call emend#ui#close()<CR>')
  call s:map_buf(l:buf, 'n', 'q',         '<Cmd>call emend#ui#close()<CR>')
  call s:map_buf(l:buf, 'n', 'j',         '<Cmd>call emend#ui#move(1)<CR>')
  call s:map_buf(l:buf, 'n', 'k',         '<Cmd>call emend#ui#move(-1)<CR>')
  call s:map_buf(l:buf, 'n', '<Down>',    '<Cmd>call emend#ui#move(1)<CR>')
  call s:map_buf(l:buf, 'n', '<Up>',      '<Cmd>call emend#ui#move(-1)<CR>')
  call s:map_buf(l:buf, 'n', '<C-d>',     '<Cmd>call emend#ui#move(10)<CR>')
  call s:map_buf(l:buf, 'n', '<C-u>',     '<Cmd>call emend#ui#move(-10)<CR>')
  call s:map_buf(l:buf, 'n', 'gg',        '<Cmd>call emend#ui#goto_first()<CR>')
  call s:map_buf(l:buf, 'n', 'G',         '<Cmd>call emend#ui#goto_last()<CR>')
  call s:map_buf(l:buf, 'n', '/',         '<Cmd>call emend#ui#new_search()<CR>')
endfunction

function! s:map_buf(buf, mode, lhs, rhs) abort
  if has('nvim')
    call nvim_buf_set_keymap(a:buf, a:mode, a:lhs, a:rhs,
          \ {'noremap': v:true, 'silent': v:true, 'nowait': v:true})
  else
    execute 'nnoremap <buffer=' . a:buf . '> <silent> <nowait> ' . a:lhs . ' ' . a:rhs
  endif
endfunction

" ---------------------------------------------------------------------------
" Navigation
" ---------------------------------------------------------------------------

function! emend#ui#move(delta) abort
  let l:new = max([0, min([s:selected + a:delta, len(s:results) - 1])])
  if l:new == s:selected
    return
  endif
  let s:selected = l:new
  call s:highlight_selected()
  call s:render_preview()
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
    execute 'edit ' . fnameescape(l:file)
    execute l:line
    normal! zz
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

" ---------------------------------------------------------------------------
" Rendering
" ---------------------------------------------------------------------------

function! s:render_list() abort
  let l:lines = []
  let l:elapsed = get(s:last_result, 'elapsed_ms', 0)
  let l:mode = get(s:last_result, 'mode', '?')

  let l:header = '  ' . len(s:results) . ' results'
  if l:elapsed > 0
    let l:header .= ' [' . l:elapsed . 'ms]'
  endif
  let l:header .= '  (' . l:mode . ')'
  call add(l:lines, l:header)
  call add(l:lines, repeat('─', 40))

  " Compute cwd prefix once for all items.
  let l:cwd = getcwd() . '/'

  for l:i in range(len(s:results))
    call add(l:lines, s:format_result_line(s:results[l:i], l:i, l:cwd))
  endfor

  call s:set_buf_lines(s:list_buf, l:lines)
  call s:highlight_selected()
endfunction

function! s:format_result_line(item, index, cwd) abort
  let l:name = get(a:item, 'name', get(a:item, 'matched_text', '?'))
  let l:kind = get(a:item, 'kind', '')
  let l:file = get(a:item, 'file_path', '')
  let l:line = get(a:item, 'line', '')
  let l:end_line = get(a:item, 'end_line', '')

  let l:prefix = a:index == s:selected ? ' > ' : '   '
  let l:text = l:prefix

  if l:kind !=# ''
    let l:text .= get(s:KIND_ICONS, l:kind, '·') . ' '
  endif

  let l:text .= l:name

  if l:file !=# ''
    " Shorten path relative to cwd.
    let l:short = l:file
    if stridx(l:short, a:cwd) == 0
      let l:short = strpart(l:short, len(a:cwd))
    endif
    let l:loc = l:short
    if l:line !=# '' && l:line isnot v:null
      let l:loc .= ':' . l:line
      if l:end_line !=# '' && l:end_line isnot v:null && l:end_line != l:line
        let l:loc .= '-' . l:end_line
      endif
    endif
    let l:text .= '  ' . l:loc
  endif

  return l:text
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
  if s:preview_buf < 0 || empty(s:results) || s:selected >= len(s:results)
    return
  endif

  let l:item = s:results[s:selected]
  let l:file = get(l:item, 'file_path', '')
  let l:start_line = get(l:item, 'line', 1)
  let l:end_line = get(l:item, 'end_line', l:start_line)
  let l:matched_text = get(l:item, 'matched_text', '')

  " Pattern mode: show matched_text directly.
  if l:matched_text !=# ''
    let l:cwd = getcwd() . '/'
    let l:short = l:file
    if stridx(l:short, l:cwd) == 0
      let l:short = strpart(l:short, len(l:cwd))
    endif
    let l:lines = ['  File: ' . l:short, '  Lines: ' . l:start_line . '-' . l:end_line, '']
    let l:lines += split(l:matched_text, "\n")
    call s:set_buf_lines(s:preview_buf, l:lines)
    call s:set_preview_ft('python')
    return
  endif

  if l:file ==# '' || !filereadable(l:file)
    call s:set_buf_lines(s:preview_buf, ['  (file not available)'])
    return
  endif

  " Read only as many lines as needed (avoid reading entire large files).
  let l:ctx = 5
  let l:max_line = l:end_line + l:ctx
  let l:all_lines = readfile(l:file, '', l:max_line)
  if empty(l:all_lines)
    call s:set_buf_lines(s:preview_buf, ['  (empty file)'])
    return
  endif

  let l:from = max([0, l:start_line - 1 - l:ctx])
  let l:to = min([len(l:all_lines), l:max_line])
  let l:preview_lines = l:all_lines[l:from : l:to - 1]

  call s:set_buf_lines(s:preview_buf, l:preview_lines)

  " Set filetype only when it changes (avoids re-triggering autocommands).
  let l:ext = fnamemodify(l:file, ':e')
  let l:ft = l:ext ==# 'py' ? 'python' : l:ext
  call s:set_preview_ft(l:ft)

  " Scroll to the match.
  let l:preview_target = l:start_line - l:from
  try
    if has('nvim')
      if nvim_win_is_valid(s:preview_win)
        call nvim_win_set_cursor(s:preview_win, [max([1, l:preview_target]), 0])
      endif
    else
      call win_execute(s:preview_win, 'call cursor(' . max([1, l:preview_target]) . ', 1)')
    endif
  catch
  endtry
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
