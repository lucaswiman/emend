" emend.vim — Autoload core for the emend Vim/Neovim plugin.
"
" Manages the emend editor-server process (JSON-RPC over stdio) and
" provides the public API used by commands in plugin/emend.vim.

" ---------------------------------------------------------------------------
" Configuration
" ---------------------------------------------------------------------------

" Path to the emend executable.  When empty the plugin auto-detects:
"   1. g:emend_command  (user override)
"   2. uv tool path     (uv tool install emend)
"   3. $PATH lookup
let g:emend_command = get(g:, 'emend_command', '')

" Project root override. Empty = auto-detect (cwd).
let g:emend_project_root = get(g:, 'emend_project_root', '')

" Maximum results per search.
let g:emend_limit = get(g:, 'emend_limit', 50)

" Preview window height (percentage of editor height).
let g:emend_preview_height = get(g:, 'emend_preview_height', 60)

" ---------------------------------------------------------------------------
" Internal state
" ---------------------------------------------------------------------------

let s:job = v:null
let s:channel = v:null        " Vim-only; Neovim uses s:job as channel
let s:req_id = 0
let s:callbacks = {}          " id → Funcref
let s:ready = 0
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
  call timer_start(500, {_ -> s:force_stop()})
endfunction

function! s:force_stop() abort
  if s:job is v:null
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
  let l:info = a:result.items[0]
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
