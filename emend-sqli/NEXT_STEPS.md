# Next Steps: Other Detectable Bug Classes in Python Library Misuse

This report identifies classes of bugs in common Python libraries that could
be reliably detected using emend's existing tooling: pattern matching, taint
tracking (intraprocedural + interprocedural), flow rules, CFG analysis,
structural rules, and DSL-aware linting.

Each class is rated by **detection confidence** (how reliably emend can find
real bugs with low false-positive rates) and **CVE density** (how many real
vulnerabilities exist in the wild to validate against).

---

## Tier 1: High confidence, high CVE density

### 1. Command Injection (os.system, subprocess, etc.)

**Detection strategy:** Flow rules (taint tracking from user input to shell sinks)

**Why it works well:** The pattern is structurally identical to SQL injection —
tainted user input flows to a dangerous function. emend's existing presets
already cover `os.system`, `subprocess.call`, `subprocess.run`.

**What to build:**
- Dataset of CVEs in Python web apps with command injection
- Rules for `os.popen`, `commands.getoutput` (legacy), `asyncio.create_subprocess_shell`
- Sanitizer patterns: `shlex.quote()`, `shlex.split()`, allowlist validation
- Structural rules for `shell=True` in subprocess calls with non-literal args

**Known CVE examples:**
- CVE-2022-36359 (Django FileResponse content-disposition)
- Numerous CVEs in Flask/FastAPI apps passing user input to shell commands

**Estimated effort:** Low — mostly reuses SQLi infrastructure with different sinks.

---

### 2. Server-Side Template Injection (SSTI)

**Detection strategy:** Flow rules + structural patterns

**Why it works well:** SSTI follows the same source→sink taint model. The sinks
are template rendering functions that accept raw strings.

**What to build:**
- Sinks: `Template(user_input).render()`, `render_template_string(user_input)`,
  `jinja2.from_string(user_input)`, `Markup(user_input)`
- Sources: standard web framework request objects
- Structural rules for `Template()` constructed from non-literal strings

**Known CVE examples:**
- CVE-2019-8341 (Jinja2 sandbox escape via `from_string`)
- Numerous Flask/Django SSTI bugs

**Estimated effort:** Low.

---

### 3. Path Traversal / Arbitrary File Access

**Detection strategy:** Flow rules + structural patterns

**Why it works well:** Another source→sink pattern. User input flows to
filesystem operations without sanitization.

**What to build:**
- Sinks: `open()`, `pathlib.Path()`, `os.path.join()`, `shutil.copy()`,
  `send_file()`, `send_from_directory()`, `FileResponse()`
- Sanitizers: `os.path.basename()`, `secure_filename()`, path prefix validation
- Structural rules for `os.path.join(base, user_input)` without `..` checking

**Known CVE examples:**
- CVE-2023-40590 (GitPython arbitrary command on Windows)
- CVE-2024-21503 (python-docx XML entity injection)
- Numerous file upload/download vulnerabilities

**Estimated effort:** Low.

---

## Tier 2: High confidence, moderate CVE density

### 4. Insecure Deserialization (pickle, yaml, marshal)

**Detection strategy:** Structural patterns + flow rules

**Why it works well:** The dangerous functions are well-known and the safe
alternatives are clear. Pattern matching alone catches most cases.

**What to build:**
- Structural rules:
  - `pickle.loads(user_input)` — always dangerous with untrusted data
  - `pickle.load(user_file)` — dangerous with untrusted files
  - `yaml.load(data)` without `Loader=SafeLoader` (pre-5.1 default was unsafe)
  - `yaml.unsafe_load(data)`, `yaml.full_load(data)`
  - `marshal.loads(data)`, `shelve.open(user_path)`
  - `jsonpickle.decode(data)` — deserializes arbitrary objects
- Flow rules: user input → deserialization function
- Sanitizer: `yaml.safe_load()`, `json.loads()` as safe alternatives

**Known CVE examples:**
- CVE-2020-1747 (PyYAML full_load arbitrary code execution)
- CVE-2017-18342 (PyYAML load() default Loader)
- Numerous pickle-based RCE in ML/AI pipelines (model loading)

**Estimated effort:** Low — mostly structural patterns.

---

### 5. XML External Entity (XXE) Injection

**Detection strategy:** Structural patterns

**Why it works well:** Python's XML libraries have well-documented unsafe
defaults. The fix is always the same: use `defusedxml` or disable features.

**What to build:**
- Structural rules for unsafe parsers:
  - `xml.etree.ElementTree.parse(user_input)` — allows entity expansion
  - `xml.sax.parse(user_input)` — allows external entities by default
  - `lxml.etree.parse(user_input)` without `resolve_entities=False`
  - `xml.dom.minidom.parse(user_input)`
  - `xmlrpc.client` with untrusted servers
- Safe alternatives: `defusedxml.parse()`, explicit feature disabling

**Known CVE examples:**
- CVE-2022-40303 / CVE-2022-40304 (libxml2 via lxml)
- CVE-2021-28957 (lxml.html.clean deanonymization)

**Estimated effort:** Low — pattern-only, no taint tracking needed.

---

### 6. Regular Expression Denial of Service (ReDoS)

**Detection strategy:** Structural patterns + emend's regex named group analysis

**Why it works well:** emend already has `extract_regex_named_groups()` and
regex analysis infrastructure in `dsl.py`. Extending this to detect
catastrophic backtracking patterns is feasible.

**What to build:**
- Structural rules for `re.compile(user_input)` / `re.match(user_input, data)`
  (user-controlled regex = guaranteed ReDoS)
- Flow rules: user input → regex compilation
- Static analysis of regex literals for known-bad patterns (nested quantifiers,
  overlapping alternatives)

**Known CVE examples:**
- CVE-2022-40897 (setuptools ReDoS in package_index)
- CVE-2021-32839 (aiohttp ReDoS in header parsing)
- CVE-2022-42969 (py library ReDoS)

**Estimated effort:** Medium — regex analysis is an extension of existing DSL work.

---

## Tier 3: Medium confidence, high value

### 7. Cryptographic Misuse

**Detection strategy:** Structural patterns

**Why it works well:** The patterns are well-known and the fixes are mechanical.
Low false positive rate because the dangerous APIs are unambiguous.

**What to build:**
- Rules for:
  - `hashlib.md5()` / `hashlib.sha1()` for password hashing (not HMAC)
  - `DES`, `Blowfish`, `RC4` cipher usage
  - `AES` with `ECB` mode
  - `random.random()` / `random.randint()` for security-sensitive values
    (tokens, passwords, nonces) — should use `secrets` module
  - Hard-coded encryption keys: `AES.new(b"hardcoded_key", ...)`
  - `ssl._create_unverified_context()` / `verify=False` in requests
- Flow rules: secret/password values flowing to weak hash functions

**Known CVE examples:**
- Widespread in Python packages (hard-coded secrets, weak hashing)
- CVE-2023-43804 (urllib3 Cookie header leak on redirect)

**Estimated effort:** Low for structural rules, medium for flow rules.

---

### 8. SSRF (Server-Side Request Forgery)

**Detection strategy:** Flow rules

**Why it works well:** Same source→sink model as SQLi. User input flows to
HTTP request functions.

**What to build:**
- Sinks: `requests.get(url)`, `urllib.request.urlopen(url)`,
  `httpx.get(url)`, `aiohttp.ClientSession().get(url)`
- Sources: standard web framework request objects
- Sanitizers: URL allowlist validation, domain checking

**Known CVE examples:**
- CVE-2023-36053 (Django URL validator bypass)
- Numerous SSRF in webhook/integration features

**Estimated effort:** Low.

---

### 9. TOCTOU (Time-of-Check-Time-of-Use) Race Conditions

**Detection strategy:** Effect-based sinks + scope sanitizers (emend's Phase 2-3
trace-CFG features)

**Why it works well:** emend already has `writes($X)` / `reads($X)` effect
predicates and `TraceScopeSanitizer` for scope boundaries. This is exactly
what TOCTOU detection needs.

**What to build:**
- Sources: check operations (`os.path.exists()`, `os.access()`,
  `stat()`, database reads)
- Sinks: `writes($X)` on the same resource (file open, database update)
- Scope sanitizers: atomic operations, locks, transactions
- File-level TOCTOU: `os.path.exists(path)` → `open(path)` without locking
- Database-level: `SELECT` → `UPDATE` without row locking

**Known CVE examples:**
- CVE-2022-24302 (Paramiko TOCTOU in private key writing)
- Numerous filesystem TOCTOU bugs

**Estimated effort:** Medium — the infrastructure exists, needs rule authoring.

---

### 10. Django-Specific ORM Misuse (N+1 queries, unsafe defaults)

**Detection strategy:** Structural patterns + call graph analysis

**Why it works well:** emend has call graph and callers/callees analysis. Many
Django performance and security bugs are structural.

**What to build:**
- `Model.objects.all()` in a loop without `select_related`/`prefetch_related`
  (using emend's `--inside` constraint with loop detection)
- `@csrf_exempt` on views that accept POST (structural)
- `DEBUG = True` in production settings (structural)
- Missing `ALLOWED_HOSTS` validation (structural)
- `mark_safe()` on anything that isn't a literal (flow rule)

**Estimated effort:** Low for structural, medium for N+1 detection.

---

## Tier 4: Experimental / research-grade

### 11. Resource Leak Detection (unclosed files, connections, sockets)

**Detection strategy:** CFG analysis + pattern matching

**Why emend could do this:** emend builds per-function CFGs and can detect
unreachable blocks. Extending this to track resource acquisition/release
along CFG paths is architecturally natural.

**What to build:**
- Resource acquisition patterns: `open()`, `socket.socket()`, `connect()`
- Resource release patterns: `.close()`, context manager `with` blocks
- CFG paths where acquisition is not followed by release (or `with`)
- Exception paths: resources acquired before try/except without finally

**Estimated effort:** High — requires new CFG analysis, but the CFG infra exists.

---

### 12. Async/Await Misuse

**Detection strategy:** Structural patterns + CFG

**What to build:**
- Calling sync I/O functions inside `async def` (e.g., `requests.get` in async,
  `time.sleep` instead of `asyncio.sleep`)
- Missing `await` on coroutine calls (structural: calling async function
  without await keyword)
- `asyncio.run()` inside an already-running event loop

**Estimated effort:** Medium — structural rules cover most cases.

---

## Recommended Implementation Order

| Priority | Bug Class | Effort | Reuses SQLi Infra |
|----------|-----------|--------|-------------------|
| 1 | Command Injection | Low | Yes |
| 2 | Path Traversal | Low | Yes |
| 3 | SSTI | Low | Yes |
| 4 | Insecure Deserialization | Low | Partially |
| 5 | XXE | Low | No (structural) |
| 6 | Cryptographic Misuse | Low | No (structural) |
| 7 | SSRF | Low | Yes |
| 8 | TOCTOU | Medium | Yes (Phase 2-3) |
| 9 | ReDoS | Medium | Partially (DSL) |
| 10 | Django ORM Misuse | Medium | Partially |
| 11 | Resource Leaks | High | No (new analysis) |
| 12 | Async Misuse | Medium | No (structural) |

The first seven items (Command Injection through SSRF) can all be implemented
as additional `emend-<name>/` dataset directories following the same structure
as `emend-sqli/`, with rules.yaml files that compose with the existing
framework presets. Items 1-3 are essentially just SQL injection with different
sinks and would take minimal effort.

The highest-value next step is **Command Injection** — it shares the exact same
taint-tracking infrastructure, has abundant CVEs for validation, and catches a
class of bug that is as dangerous as SQL injection but less often scanned for.
