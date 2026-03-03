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
let s:mode = ''
let s:elapsed_ms = 0
let s:project_root = ''

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
  " Check if the server is running; start + warm cache if needed.
  if !emend#is_running()
    call emend#start()
  endif
  if !emend#is_ready()
    " Server still starting — show cache-warming UI.
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
    " Check if this is a cache miss / index not ready.
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

  let s:results = get(a:result, 'items', [])
  let s:mode = get(a:result, 'mode', '?')
  let s:elapsed_ms = get(a:result, 'elapsed_ms', 0)
  let s:selected = 0

  if empty(s:results)
    echo 'emend: no results for "' . s:query . '" [' . s:elapsed_ms . 'ms]'
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
  " Open a simple buffer showing that we're warming the cache.
  call s:open_ui()

  " Fill the list side with a status message.
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

  " Start `emend index -vv` and capture output.
  let l:emend = s:find_emend_or_default()
  let l:root = g:emend_project_root !=# '' ? g:emend_project_root : getcwd()

  if has('nvim')
    let s:cache_job = jobstart([l:emend, 'index', l:root, '-vv'], {
          \ 'on_stdout': function('s:on_cache_stdout'),
          \ 'on_stderr': function('s:on_cache_stderr'),
          \ 'on_exit':   function('s:on_cache_exit', [a:query]),
          \ 'stdout_buffered': v:false,
          \ })
  else
    let s:cache_job = job_start([l:emend, 'index', l:root, '-vv'], {
          \ 'out_cb':  function('s:on_cache_output'),
          \ 'err_cb':  function('s:on_cache_output'),
          \ 'exit_cb': function('s:on_cache_done', [a:query]),
          \ })
  endif

  " Start a timer to update elapsed time display.
  let s:cache_timer = timer_start(500, function('s:update_cache_ticker'), {'repeat': -1})
endfunction

function! s:find_emend_or_default() abort
  " Reuse the detection from the main module.
  let l:cmd = g:emend_command
  if l:cmd !=# ''
    return l:cmd
  endif
  if executable('emend')
    return 'emend'
  endif
  return 'emend'
endfunction

function! s:on_cache_stdout(job_id, data, ...) abort
  if s:preview_buf < 0 || !bufexists(s:preview_buf)
    return
  endif
  let l:lines = type(a:data) == v:t_list ? a:data : split(a:data, "\n")
  call s:append_buf_lines(s:preview_buf, l:lines)
endfunction

function! s:on_cache_stderr(job_id, data, ...) abort
  call s:on_cache_stdout(a:job_id, a:data)
endfunction

function! s:on_cache_output(channel, msg) abort
  if s:preview_buf < 0 || !bufexists(s:preview_buf)
    return
  endif
  let l:lines = split(a:msg, "\n")
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

function! s:on_cache_done(query, job, status) abort
  call s:on_cache_exit(a:query, a:job, a:status)
endfunction

function! s:retry_search(query) abort
  call emend#start()
  " Wait for ready, then search.
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
  " Update the first line with elapsed time.
  call s:set_buf_line(s:list_buf, 0,
        \ '  emend — Warming cache' . l:ticks . ' (' . l:secs . 's)')
endfunction

" ---------------------------------------------------------------------------
" Window / buffer management
" ---------------------------------------------------------------------------

function! s:open_ui() abort
  " Close any existing emend UI windows first.
  call s:close_ui_silent()

  " Calculate dimensions.
  let l:total_h = &lines
  let l:total_w = &columns
  let l:height = max([10, l:total_h * g:emend_preview_height / 100])
  let l:list_w = max([30, l:total_w * 35 / 100])
  let l:preview_w = l:total_w - l:list_w - 3  " 3 for separator + borders

  " Create list buffer.
  let s:list_buf = s:create_scratch_buf('emend://results')

  " Create preview buffer.
  let s:preview_buf = s:create_scratch_buf('emend://preview')

  if has('nvim')
    call s:open_nvim_float(l:height, l:list_w, l:preview_w)
  else
    call s:open_vim_split(l:height, l:list_w)
  endif

  " Populate the result list.
  if !empty(s:results)
    call s:render_list()
    call s:render_preview()
  endif

  " Set up keybindings in the list window.
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

  " List window (left pane).
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

  " Preview window (right pane).
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

  " Style.
  call nvim_win_set_option(s:list_win, 'cursorline', v:true)
  call nvim_win_set_option(s:list_win, 'number', v:false)
  call nvim_win_set_option(s:preview_win, 'number', v:true)
  call nvim_win_set_option(s:preview_win, 'wrap', v:false)
endfunction

function! s:open_vim_split(height, list_w) abort
  " In classic Vim, use horizontal + vertical splits at the bottom.
  execute 'botright ' . a:height . 'new'
  let s:list_win = win_getid()
  execute 'buffer ' . s:list_buf
  setlocal cursorline nonumber norelativenumber nowrap

  execute 'vertical rightbelow new'
  let s:preview_win = win_getid()
  execute 'buffer ' . s:preview_buf
  setlocal number nowrap

  " Resize the list pane.
  call win_gotoid(s:list_win)
  execute 'vertical resize ' . a:list_w
endfunction

function! s:close_ui() abort
  call s:close_ui_silent()
endfunction

function! s:close_ui_silent() abort
  " Stop any cache warming timer.
  if s:cache_timer >= 0
    call timer_stop(s:cache_timer)
    let s:cache_timer = -1
  endif

  " Close windows safely.
  if s:preview_win >= 0
    try
      if has('nvim')
        if nvim_win_is_valid(s:preview_win)
          call nvim_win_close(s:preview_win, v:true)
        endif
      else
        let l:winnr = win_id2win(s:preview_win)
        if l:winnr > 0
          execute l:winnr . 'wincmd c'
        endif
      endif
    catch
    endtry
    let s:preview_win = -1
  endif

  if s:list_win >= 0
    try
      if has('nvim')
        if nvim_win_is_valid(s:list_win)
          call nvim_win_close(s:list_win, v:true)
        endif
      else
        let l:winnr = win_id2win(s:list_win)
        if l:winnr > 0
          execute l:winnr . 'wincmd c'
        endif
      endif
    catch
    endtry
    let s:list_win = -1
  endif

  let s:list_buf = -1
  let s:preview_buf = -1
endfunction

" ---------------------------------------------------------------------------
" Keymaps
" ---------------------------------------------------------------------------

function! s:setup_keymaps() abort
  " All mappings are buffer-local to the list buffer.
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
  let l:new = s:selected + a:delta
  let l:new = max([0, min([l:new, len(s:results) - 1])])
  if l:new == s:selected
    return
  endif
  let s:selected = l:new
  call s:highlight_selected()
  call s:render_preview()
endfunction

function! emend#ui#goto_first() abort
  let s:selected = 0
  call s:highlight_selected()
  call s:render_preview()
endfunction

function! emend#ui#goto_last() abort
  let s:selected = max([0, len(s:results) - 1])
  call s:highlight_selected()
  call s:render_preview()
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

  " Header.
  let l:header = '  ' . len(s:results) . ' results'
  if get(s:, 'elapsed_ms', 0) > 0
    let l:header .= ' [' . s:elapsed_ms . 'ms]'
  endif
  let l:header .= '  (' . s:mode . ')'
  call add(l:lines, l:header)
  call add(l:lines, repeat('─', 40))

  " Items.
  for l:i in range(len(s:results))
    let l:item = s:results[l:i]
    let l:line = s:format_result_line(l:item, l:i)
    call add(l:lines, l:line)
  endfor

  call s:set_buf_lines(s:list_buf, l:lines)
  call s:highlight_selected()
endfunction

function! s:format_result_line(item, index) abort
  let l:name = get(a:item, 'name', get(a:item, 'matched_text', '?'))
  let l:kind = get(a:item, 'kind', '')
  let l:file = get(a:item, 'file_path', '')
  let l:line = get(a:item, 'line', '')
  let l:end_line = get(a:item, 'end_line', '')

  " Shorten file path.
  let l:short_file = s:shorten_path(l:file)

  " Build display line.
  let l:prefix = a:index == s:selected ? ' > ' : '   '
  let l:text = l:prefix

  if l:kind !=# ''
    let l:kind_icon = s:kind_icon(l:kind)
    let l:text .= l:kind_icon . ' '
  endif

  let l:text .= l:name

  if l:short_file !=# ''
    let l:loc = l:short_file
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

function! s:kind_icon(kind) abort
  let l:icons = {
        \ 'class': 'C',
        \ 'function': 'f',
        \ 'method': 'm',
        \ 'async_function': 'af',
        \ 'async_method': 'am',
        \ 'variable': 'v',
        \ }
  return get(l:icons, a:kind, '·')
endfunction

function! s:shorten_path(path) abort
  if a:path ==# ''
    return ''
  endif
  " Make relative to cwd.
  let l:cwd = getcwd() . '/'
  let l:p = a:path
  if stridx(l:p, l:cwd) == 0
    let l:p = strpart(l:p, len(l:cwd))
  endif
  return l:p
endfunction

function! s:highlight_selected() abort
  if s:list_win < 0
    return
  endif

  " The result items start at line index 2 (after header + separator).
  let l:target_line = s:selected + 3  " 1-indexed, +2 for header lines

  try
    if has('nvim')
      if nvim_win_is_valid(s:list_win)
        call nvim_win_set_cursor(s:list_win, [l:target_line, 0])
      endif
    else
      let l:winnr = win_id2win(s:list_win)
      if l:winnr > 0
        call win_execute(s:list_win, 'call cursor(' . l:target_line . ', 1)')
      endif
    endif
  catch
  endtry
endfunction

function! s:render_preview() abort
  if s:preview_buf < 0 || empty(s:results) || s:selected >= len(s:results)
    return
  endif

  let l:item = s:results[s:selected]
  let l:file = get(l:item, 'file_path', '')
  let l:start_line = get(l:item, 'line', 1)
  let l:end_line = get(l:item, 'end_line', l:start_line)
  let l:matched_text = get(l:item, 'matched_text', '')

  " If we have matched_text (pattern mode), show it with context.
  if l:matched_text !=# ''
    let l:lines = ['  File: ' . s:shorten_path(l:file), '  Lines: ' . l:start_line . '-' . l:end_line, '']
    let l:lines += split(l:matched_text, "\n")
    call s:set_buf_lines(s:preview_buf, l:lines)
    " Try to set filetype for syntax highlighting.
    call setbufvar(s:preview_buf, '&filetype', 'python')
    return
  endif

  " Otherwise read the file and show relevant lines.
  if l:file ==# '' || !filereadable(l:file)
    call s:set_buf_lines(s:preview_buf, ['  (file not available)'])
    return
  endif

  let l:all_lines = readfile(l:file)
  if empty(l:all_lines)
    call s:set_buf_lines(s:preview_buf, ['  (empty file)'])
    return
  endif

  " Show context around the match.
  let l:ctx = 5
  let l:from = max([0, l:start_line - 1 - l:ctx])
  let l:to = min([len(l:all_lines), l:end_line + l:ctx])
  let l:preview_lines = l:all_lines[l:from : l:to - 1]

  call s:set_buf_lines(s:preview_buf, l:preview_lines)

  " Set filetype for syntax highlighting.
  let l:ext = fnamemodify(l:file, ':e')
  if l:ext ==# 'py'
    call setbufvar(s:preview_buf, '&filetype', 'python')
  elseif l:ext !=# ''
    call setbufvar(s:preview_buf, '&filetype', l:ext)
  endif

  " Scroll preview window to center on the match.
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
    " Classic Vim: delete all, then append.
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
