" emend.vim — Autoload core for the emend Vim/Neovim plugin.
"
" Manages the emend editor-server process (JSON-RPC over stdio) and
" provides the public API used by commands in plugin/emend.vim.

" ---------------------------------------------------------------------------
" Internal state
" ---------------------------------------------------------------------------

let s:job = v:null
let s:channel = v:null        " Vim-only; Neovim uses s:job as channel
let s:req_id = 0
let s:callbacks = {}          " id → Funcref
let s:ready = 0
let s:indexing = 0            " 1 while background reindex is running
let s:buf = ''                " partial read buffer
let s:detected_emend = ''     " cached executable path

" ---------------------------------------------------------------------------
" Executable detection (cached)
" ---------------------------------------------------------------------------

function! s:find_emend() abort
  if g:emend_command !=# ''
    return g:emend_command
  endif

  " Return cached result if available.
  if s:detected_emend !=# ''
    return s:detected_emend
  endif

  " Try uv tool path first (preferred install method).
  let l:uv_path = trim(system('uv tool dir 2>/dev/null'))
  if v:shell_error == 0 && l:uv_path !=# ''
    let l:candidate = l:uv_path . '/emend/bin/emend'
    if executable(l:candidate)
      let s:detected_emend = l:candidate
      return l:candidate
    endif
    " Also check the uv tool bin directory
    let l:uv_bin = trim(system('uv tool run --from emend which emend 2>/dev/null'))
    if v:shell_error == 0 && l:uv_bin !=# '' && executable(l:uv_bin)
      let s:detected_emend = l:uv_bin
      return l:uv_bin
    endif
  endif

  " Fall back to PATH.
  if executable('emend')
    let s:detected_emend = 'emend'
    return 'emend'
  endif

  return ''
endfunction

" Public accessor so other autoload files (e.g. ui.vim) can reuse detection.
function! emend#find_executable() abort
  return s:find_emend()
endfunction

" ---------------------------------------------------------------------------
" Server lifecycle
" ---------------------------------------------------------------------------

function! emend#start(...) abort
  if s:job isnot v:null
    return
  endif

  let l:emend = s:find_emend()
  if l:emend ==# ''
    echohl ErrorMsg
    echom 'emend: executable not found.  Install with `uv tool install emend` or set g:emend_command.'
    echohl None
    return
  endif

  let l:root = g:emend_project_root !=# '' ? g:emend_project_root : getcwd()

  let s:ready = 0
  let s:indexing = 0
  let s:buf = ''
  let s:callbacks = {}
  let s:req_id = 0

  if has('nvim')
    let s:job = jobstart([l:emend, 'editor-server', l:root], {
          \ 'on_stdout': function('s:on_nvim_stdout'),
          \ 'on_stderr': function('s:on_nvim_stderr'),
          \ 'on_exit':   function('s:on_nvim_exit'),
          \ })
    if s:job <= 0
      echohl ErrorMsg
      echom 'emend: failed to start editor-server (jobstart returned ' . s:job . ')'
      echohl None
      let s:job = v:null
      return
    endif
  else
    let s:job = job_start([l:emend, 'editor-server', l:root], {
          \ 'out_cb':  function('s:on_vim_stdout'),
          \ 'err_cb':  function('s:on_vim_stderr'),
          \ 'exit_cb': function('s:on_vim_exit'),
          \ 'out_mode': 'raw',
          \ })
    if job_status(s:job) ==# 'fail'
      echohl ErrorMsg
      echom 'emend: failed to start editor-server'
      echohl None
      let s:job = v:null
      return
    endif
    let s:channel = job_getchannel(s:job)
  endif
endfunction

function! emend#stop() abort
  if s:job is v:null
    return
  endif
  call emend#send('shutdown', {}, {_ -> 0})
  let l:current_job = s:job
  call timer_start(500, {t -> s:force_stop(l:current_job)})
endfunction

function! s:force_stop(job) abort
  if a:job isnot s:job || s:job is v:null
    return
  endif
  if has('nvim')
    call jobstop(s:job)
  else
    call job_stop(s:job, 'kill')
  endif
  let s:job = v:null
  let s:channel = v:null
  let s:ready = 0
  let s:callbacks = {}
endfunction

function! emend#is_running() abort
  return s:job isnot v:null
endfunction

function! emend#is_ready() abort
  return s:ready
endfunction

function! emend#is_indexing() abort
  return s:indexing
endfunction

" ---------------------------------------------------------------------------
" JSON-RPC send
" ---------------------------------------------------------------------------

let s:send_retries = 0

function! emend#send(method, params, Callback) abort
  if s:job is v:null
    call emend#start()
  endif

  " If the server is started but not yet ready, wait with bounded retries.
  if !s:ready
    let s:send_retries += 1
    if s:send_retries > 50
      let s:send_retries = 0
      call a:Callback({'error': {'code': -1, 'message': 'server did not become ready'}})
      return
    endif
    call timer_start(100, {_ -> emend#send(a:method, a:params, a:Callback)})
    return
  endif

  let s:send_retries = 0
  let s:req_id += 1
  let l:id = s:req_id
  let s:callbacks[l:id] = a:Callback

  let l:msg = json_encode({
        \ 'jsonrpc': '2.0',
        \ 'id': l:id,
        \ 'method': a:method,
        \ 'params': a:params,
        \ })

  if has('nvim')
    call chansend(s:job, l:msg . "\n")
  else
    call ch_sendraw(s:channel, l:msg . "\n")
  endif
endfunction

" ---------------------------------------------------------------------------
" Callbacks — Neovim
" ---------------------------------------------------------------------------

function! s:on_nvim_stdout(job_id, data, event) abort
  let s:buf .= join(a:data, "\n")
  call s:process_buf()
endfunction

function! s:on_nvim_stderr(job_id, data, event) abort
endfunction

function! s:on_nvim_exit(job_id, exit_code, event) abort
  call s:on_server_exit()
endfunction

" ---------------------------------------------------------------------------
" Callbacks — Vim
" ---------------------------------------------------------------------------

function! s:on_vim_stdout(channel, msg) abort
  let s:buf .= a:msg
  call s:process_buf()
endfunction

function! s:on_vim_stderr(channel, msg) abort
endfunction

function! s:on_vim_exit(job, status) abort
  call s:on_server_exit()
endfunction

" ---------------------------------------------------------------------------
" Shared exit handler
" ---------------------------------------------------------------------------

function! s:on_server_exit() abort
  let s:job = v:null
  let s:channel = v:null
  let s:ready = 0
  let s:indexing = 0
  " Notify pending callbacks of server exit, then clear.
  for [l:id, l:Cb] in items(s:callbacks)
    try
      call l:Cb({'error': {'code': -1, 'message': 'server exited'}})
    catch
    endtry
  endfor
  let s:callbacks = {}
endfunction

" ---------------------------------------------------------------------------
" Response processing
" ---------------------------------------------------------------------------

function! s:process_buf() abort
  while 1
    let l:nl = stridx(s:buf, "\n")
    if l:nl < 0
      break
    endif
    let l:line = strpart(s:buf, 0, l:nl)
    let s:buf = strpart(s:buf, l:nl + 1)
    if l:line ==# ''
      continue
    endif
    try
      let l:msg = json_decode(l:line)
    catch
      continue
    endtry
    call s:handle_message(l:msg)
  endwhile
endfunction

function! s:handle_message(msg) abort
  " Handle server notifications (no id).
  if !has_key(a:msg, 'id') && has_key(a:msg, 'method')
    if a:msg.method ==# 'ready'
      let s:ready = 1
    elseif a:msg.method ==# 'indexing_started'
      let s:indexing = 1
    elseif a:msg.method ==# 'indexing_complete'
      let s:indexing = 0
      call emend#ui#on_indexing_complete()
    endif
    return
  endif

  " Handle responses.
  let l:id = get(a:msg, 'id', v:null)
  if l:id is v:null
    return
  endif
  let l:key = l:id
  if has_key(s:callbacks, l:key)
    let l:Cb = s:callbacks[l:key]
    call remove(s:callbacks, l:key)
    if has_key(a:msg, 'error')
      call l:Cb({'error': a:msg.error})
    else
      call l:Cb(get(a:msg, 'result', {}))
    endif
  endif
endfunction



function! s:apply_default_mappings() abort
  nnoremap <silent> <Leader>es <Cmd>Emend<CR>
  nnoremap <silent> <Leader>eo <Cmd>EmendOutline<CR>
  nnoremap <silent> <Leader>er <Cmd>EmendRefs<CR>
  nnoremap <silent> <Leader>eg <Cmd>EmendGoto<CR>
  nnoremap <silent> <Leader>ek <Cmd>EmendKB<CR>
  nnoremap <silent> <Leader>eR <Cmd>EmendReplace<CR>
  nnoremap <silent> <Leader>em <Cmd>EmendMove<CR>
  nnoremap <silent> <Leader>ec <Cmd>EmendCallers<CR>
  nnoremap <silent> <Leader>eC <Cmd>EmendCallees<CR>
  nnoremap <silent> <Leader>et <Cmd>EmendTypeHover<CR>
  nnoremap <silent> <Leader>eO <Cmd>EmendOutlineFilter<CR>
endfunction

" ---------------------------------------------------------------------------
" Helpers
" ---------------------------------------------------------------------------

function! s:show_rpc_error(prefix, result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg
    echom a:prefix . get(a:result.error, 'message', 'unknown error')
    echohl None
    return 1
  endif
  return 0
endfunction

" ---------------------------------------------------------------------------
" Public search API
" ---------------------------------------------------------------------------

function! emend#search(query, ...) abort
  let l:params = {'query': a:query, 'limit': g:emend_limit}
  if a:0 > 0
    call extend(l:params, a:1)
  endif
  call emend#send('search', l:params, function('emend#ui#on_search_result'))
endfunction

function! emend#file_symbols(file_path, ...) abort
  let l:Cb = a:0 > 0 ? a:1 : function('emend#ui#on_search_result')
  call emend#send('file_symbols', {'file': a:file_path}, l:Cb)
endfunction

function! emend#references(qualified_name, ...) abort
  let l:Cb = a:0 > 0 ? a:1 : function('emend#ui#on_search_result')
  call emend#send('references', {'qualified_name': a:qualified_name}, l:Cb)
endfunction

function! emend#status(...) abort
  let l:Cb = a:0 > 0 ? a:1 : {r -> s:show_status(r)}
  call emend#send('status', {}, l:Cb)
endfunction

function! s:show_status(result) abort
  if s:show_rpc_error('emend: ', a:result)
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: status unavailable'
    return
  endif
  let l:info = l:items[0]
  echo 'emend index: '
        \ . l:info.symbol_count . ' symbols, '
        \ . l:info.reference_count . ' refs, '
        \ . l:info.file_count . ' files'
        \ . (l:info.fts_count > 0 ? ' (FTS: ' . l:info.fts_count . ')' : '')
        \ . ' [' . a:result.elapsed_ms . 'ms]'
endfunction

function! emend#reindex(...) abort
  let l:Cb = a:0 > 0 ? a:1 : {r -> s:show_reindex(r)}
  call emend#send('reindex', {}, l:Cb)
endfunction

function! s:show_reindex(result) abort
  if s:show_rpc_error('emend reindex: ', a:result)
    return
  endif
  echo 'emend: reindex complete [' . a:result.elapsed_ms . 'ms]'
endfunction

" ---------------------------------------------------------------------------
" Knowledge base / cross-repo goto
" ---------------------------------------------------------------------------

function! emend#goto(identifier, ...) abort
  let l:Cb = a:0 > 0 ? a:1 : function('s:on_goto_result')
  let [l:line_num, l:col] = getpos('.')[1:2]
  call emend#send('mapping_goto', {
        \ 'identifier': a:identifier,
        \ 'file': expand('%:p'),
        \ 'line': l:line_num,
        \ 'col': l:col,
        \ }, l:Cb)
endfunction

function! emend#rename(new_name) abort
  let l:old_name = expand('<cword>')
  let l:new_name = a:new_name
  if empty(l:new_name)
    let l:new_name = input('Rename ' . l:old_name . ' to: ', l:old_name)
  endif
  if empty(l:new_name) || l:new_name ==# l:old_name
    return
  endif

  " Resolve QN first
  let [l:line, l:col] = getpos('.')[1:2]
  call emend#send('goto_definition', {
        \ 'file': expand('%:p'),
        \ 'line': l:line,
        \ 'col': l:col,
        \ }, {res -> s:on_rename_resolve(res, l:new_name)})
endfunction

function! s:on_rename_resolve(result, new_name) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echohl ErrorMsg | echom 'emend: could not resolve symbol at cursor' | echohl None
    return
  endif
  let l:item = l:items[0]
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  let l:file = get(l:item, 'file_path', '')
  
  call emend#send('rename_preview', {
        \ 'qualified_name': l:qn,
        \ 'new_name': a:new_name,
        \ 'file': l:file,
        \ }, {res -> emend#ui#show_rename_preview(res, l:qn, a:new_name, l:file)})
endfunction

" ---------------------------------------------------------------------------
" Pattern Replace
" ---------------------------------------------------------------------------

function! emend#replace_prompt() abort
  let l:pattern = input('Pattern: ')
  if empty(l:pattern)
    return
  endif
  let l:replacement = input('Replace with: ')
  if empty(l:replacement)
    return
  endif

  echo "\n"
  echom 'emend: searching for matches...'
  call emend#send('replace_preview', {
        \ 'pattern': l:pattern,
        \ 'replacement': l:replacement,
        \ }, {res -> emend#ui#show_replace_preview(res, l:pattern, l:replacement)})
endfunction

" ---------------------------------------------------------------------------
" Move Symbol
" ---------------------------------------------------------------------------

function! emend#move(dest_file) abort
  " First resolve the symbol at cursor
  let [l:line, l:col] = getpos('.')[1:2]
  let l:dest = a:dest_file
  if empty(l:dest)
    let l:dest = input('Move to file: ', '', 'file')
  endif
  if empty(l:dest)
    return
  endif

  call emend#send('goto_definition', {
        \ 'file': expand('%:p'),
        \ 'line': l:line,
        \ 'col': l:col,
        \ }, {res -> s:on_move_resolve(res, l:dest)})
endfunction

function! s:on_move_resolve(result, dest_file) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echohl ErrorMsg | echom 'emend: could not resolve symbol at cursor' | echohl None
    return
  endif
  let l:item = l:items[0]
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  let l:file = get(l:item, 'file_path', '')

  echom 'emend: previewing move of ' . l:qn . '...'
  call emend#send('move_preview', {
        \ 'qualified_name': l:qn,
        \ 'dest_file': a:dest_file,
        \ 'file': l:file,
        \ }, {res -> emend#ui#show_move_preview(res, l:qn, a:dest_file, l:file)})
endfunction

" ---------------------------------------------------------------------------
" Callers / Callees
" ---------------------------------------------------------------------------

function! emend#callers(name) abort
  let [l:line, l:col] = getpos('.')[1:2]
  call emend#send('goto_definition', {
        \ 'file': expand('%:p'),
        \ 'line': l:line,
        \ 'col': l:col,
        \ }, {res -> s:on_callers_resolve(res, 'callers')})
endfunction

function! emend#callees(name) abort
  let [l:line, l:col] = getpos('.')[1:2]
  call emend#send('goto_definition', {
        \ 'file': expand('%:p'),
        \ 'line': l:line,
        \ 'col': l:col,
        \ }, {res -> s:on_callers_resolve(res, 'callees')})
endfunction

function! s:on_callers_resolve(result, method) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echohl ErrorMsg | echom 'emend: could not resolve symbol at cursor' | echohl None
    return
  endif
  let l:item = l:items[0]
  let l:qn = get(l:item, 'qualified_name', get(l:item, 'name', ''))
  let l:file = get(l:item, 'file_path', '')

  call emend#send(a:method, {
        \ 'qualified_name': l:qn,
        \ 'file': l:file,
        \ }, function('emend#ui#on_search_result'))
endfunction

" ---------------------------------------------------------------------------
" Type Hover
" ---------------------------------------------------------------------------

function! emend#type_hover() abort
  let [l:line, l:col] = getpos('.')[1:2]
  call emend#send('types_at_cursor', {
        \ 'file': expand('%:p'),
        \ 'line': l:line,
        \ 'col': l:col,
        \ }, function('s:on_type_hover'))
endfunction

function! s:on_type_hover(result) abort
  if has_key(a:result, 'error')
    echohl ErrorMsg | echom 'emend: ' . a:result.error.message | echohl None
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: no type information available'
    return
  endif
  let l:item = l:items[0]
  let l:name = get(l:item, 'name', '?')
  let l:type = get(l:item, 'type', '?')
  echo l:name . ': ' . l:type
endfunction

" ---------------------------------------------------------------------------
" Outline Filter (interactive outline)
" ---------------------------------------------------------------------------

function! emend#outline_filter() abort
  call emend#ui#interactive()
  " Pre-populate with file symbols, then allow filtering
  call emend#file_symbols(expand('%:p'), function('s:on_outline_filter_result'))
endfunction

function! s:on_outline_filter_result(result) abort
  if has_key(a:result, 'error')
    call emend#ui#on_search_result(a:result)
    return
  endif
  " Store original outline items for filtering
  let s:outline_items = get(a:result, 'items', [])
  call emend#ui#on_search_result(a:result)
endfunction

" ---------------------------------------------------------------------------
" Global completion (C-Space in any buffer)
" ---------------------------------------------------------------------------

function! emend#complete_at_cursor() abort
  let l:line_text = getline('.')
  let l:line_num = line('.')
  let l:col = col('.')
  let l:before = strpart(l:line_text, 0, l:col - 1)

  " Check for dotted prefix (e.g. "DocumentRequestConfig.ing")
  let l:dotted = matchstr(l:before, '\k\+\.\k*$')
  if !empty(l:dotted)
    let l:word = l:dotted
    " For dotted completions, only replace the part after the last dot.
    let l:replace_len = len(matchstr(l:dotted, '\.\zs\k*$'))
  else
    let l:word = matchstr(l:before, '\k\+$')
    let l:replace_len = len(l:word)
  endif

  if empty(l:word) | return | endif

  call emend#send('complete', {
        \ 'prefix': l:word,
        \ 'file': expand('%:p'),
        \ 'line': l:line_num,
        \ 'col': l:col,
        \ }, {res -> s:on_complete_result(res, l:replace_len)})
endfunction

function! s:on_complete_result(result, replace_len) abort
  if has_key(a:result, 'error') | return | endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items) | return | endif

  " Don't auto-insert or auto-select; let user browse with arrows.
  let s:save_cot = &completeopt
  set completeopt=menuone,noinsert,noselect

  " Install dot-chaining BEFORE showing the popup, so typing '.' while
  " browsing the menu accepts the current item + triggers attribute completion.
  inoremap <buffer><silent> . <C-y>.<Cmd>call <SID>dot_complete()<CR>

  let l:start_col = col('.') - a:replace_len
  call complete(l:start_col, l:items)

  " Restore completeopt and clean up dot mapping after the popup closes.
  augroup emend_complete_restore
    autocmd!
    autocmd CompleteDone * ++once call s:on_complete_done()
  augroup END
endfunction

function! s:on_complete_done() abort
  let &completeopt = s:save_cot
  " Keep the dot mapping alive briefly for the Enter-then-dot case.
  " It will be cleaned up on InsertLeave or by the next dot_complete call.
  augroup emend_dot_cleanup
    autocmd!
    autocmd InsertLeave * ++once silent! iunmap <buffer> .
  augroup END
endfunction

function! s:dot_complete() abort
  " Remove the one-shot dot mapping.
  silent! iunmap <buffer> .
  augroup emend_dot_cleanup
    autocmd!
  augroup END
  " Trigger completion directly. We're called from <Cmd> within insert mode,
  " so the '.' has already been inserted into the buffer.
  call emend#complete_at_cursor()
endfunction

function! emend#jump_to(file, line) abort
  if empty(a:file) || !filereadable(a:file)
    return
  endif

  let l:target = fnamemodify(a:file, ':p')
  let l:current = expand('%:p')

  " Case 1: Same file — just move the cursor.
  if l:target ==# l:current
    " Mark current position in jumplist so Ctrl-O returns here.
    normal! m'
    execute a:line
    normal! zz
    return
  endif

  " Case 2: File already open in a tab — switch to it.
  " Search all tab pages and windows for a buffer matching the target path.
  for l:tab in range(1, tabpagenr('$'))
    for l:win in range(1, tabpagewinnr(l:tab, '$'))
      let l:bufnr = tabpagebuflist(l:tab)[l:win - 1]
      if fnamemodify(bufname(l:bufnr), ':p') ==# l:target
        " Found it. Switch tab, switch window, go to line.
        execute 'tabnext ' . l:tab
        execute l:win . 'wincmd w'
        " Mark position in jumplist before moving.
        normal! m'
        execute a:line
        normal! zz
        return
      endif
    endfor
  endfor

  " Case 3: File not open anywhere — open it in the current window.
  " normal! m' sets the ' mark so Ctrl-O returns to this position/buffer.
  normal! m'
  execute 'edit ' . fnameescape(l:target)
  execute a:line
  normal! zz
endfunction

function! s:on_goto_result(result) abort
  if s:show_rpc_error('emend goto: ', a:result)
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: no definition found for this symbol'
    return
  endif
  let l:source = get(a:result, 'source', 'kb')

  " Single result — jump directly.
  if len(l:items) == 1
    let l:item = l:items[0]
    " Local result: has file_path + line from the project index.
    if has_key(l:item, 'file_path') && has_key(l:item, 'line')
      call emend#jump_to(l:item.file_path, l:item.line)
      return
    endif
    " KB result: has resolved_path from cross-repo mapping.
    if has_key(l:item, 'resolved_path')
      let l:path = l:item.resolved_path
      if isdirectory(l:path)
        echo 'emend: mapped to directory: ' . l:path
      else
        call emend#jump_to(l:path, get(l:item, 'line', 1))
      endif
      return
    endif
  endif

  " Multiple results: show in the search UI.
  call emend#ui#on_search_result(a:result)
endfunction

function! emend#module_resolve(module_name, ...) abort
  let l:Cb = a:0 > 0 ? a:1 : function('s:on_module_resolve')
  call emend#send('module_resolve', {'module': a:module_name}, l:Cb)
endfunction

function! s:on_module_resolve(result) abort
  if s:show_rpc_error('emend resolve: ', a:result)
    return
  endif
  let l:items = get(a:result, 'items', [])
  if empty(l:items)
    echo 'emend: no module mapping found'
    return
  endif

  let l:item = l:items[0]
  let l:path = get(l:item, 'resolved_path', '')
  if l:path !=# ''
    if isdirectory(l:path)
      echo 'emend: module maps to directory: ' . l:path
    else
      call emend#jump_to(l:path, 1)
    endif
  else
    let l:repo = get(l:item, 'repo', '')
    if l:repo !=# ''
      echo 'emend: module maps to repo ' . l:repo . ' (not yet cloned)'
    else
      echo 'emend: module mapping has no resolved path'
    endif
  endif
endfunction
