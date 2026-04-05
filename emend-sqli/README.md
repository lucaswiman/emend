# emend-sqli: SQL Injection Detection Rules & Dataset

Detection rules and a curated dataset of real-world SQL injection vulnerabilities
in Python libraries, for use with [emend](https://github.com/lucaswiman/emend).

## Dataset

`dataset.yaml` contains verified SQL injection CVEs in Python projects with:
- CVE identifiers
- GitHub repository URLs
- Fix commit hashes (test the parent commit for the vulnerable version)
- Descriptions of the vulnerable pattern
- Framework/library involved

### Covered libraries

| Library     | CVE count | Patterns |
|-------------|-----------|----------|
| Django      | 8+        | `order_by()`, `annotate()`, `aggregate()`, `explain()`, `Trunc()`, `Extract()`, `StringAgg`, `values()`, `extra()`, `RawSQL`, JSONField key injection |
| SQLAlchemy  | varies    | `text()`, `session.execute()`, `engine.execute()` with string formatting |
| psycopg2    | varies    | `cursor.execute()` with `%` formatting or string concatenation |
| Raw DB-API  | general   | Any `cursor.execute()` with unsanitized input |

## Rules

`.emend/rules.yaml` contains two kinds of detection:

### Structural rules (pattern matching)

Detect dangerous code patterns directly, such as:
```python
# Caught by raw-sql-format-string
cursor.execute("SELECT * FROM users WHERE id = %s" % user_id)

# Caught by sqlalchemy-text-fstring
session.execute(text(f"SELECT * FROM users WHERE name = '{name}'"))

# Caught by django-rawsql-format
RawSQL("SELECT * FROM t WHERE id = %s" % pk)
```

### Flow rules (taint tracking)

Track data flow from user input sources to SQL sinks:
```python
# Caught by django-order-by-user-input
sort_field = request.GET.get("sort")  # source: user input
queryset.order_by(sort_field)         # sink: order_by()

# Caught by django-annotate-injection
alias = request.GET["alias"]          # source: user input
queryset.annotate(**{alias: Count("id")})  # sink: annotate()
```

### Trace analysis (taint propagation)

The `trace:` section defines source/sink/sanitizer triples for emend's
Datalog-based taint analysis engine:
```bash
# Run taint analysis on a project
emend trace src/ --config emend-sqli/.emend/rules.yaml --label sqli

# With interprocedural tracking
emend trace src/ --config emend-sqli/.emend/rules.yaml --interprocedural
```

## Usage

```bash
# Lint a project for SQL injection patterns
emend lint <path> --config emend-sqli/.emend/rules.yaml

# Run trace analysis
emend trace <path> --config emend-sqli/.emend/rules.yaml

# Test against a known-vulnerable commit
git clone https://github.com/django/django /tmp/django
cd /tmp/django && git checkout <vulnerable-commit>^
emend trace django/ --config /path/to/emend-sqli/.emend/rules.yaml --label sqli
```

## Validating rules against the dataset

Each entry in `dataset.yaml` includes a `fix_commit`. To test a rule against
the vulnerable version:

```bash
# Clone the repo at the commit before the fix
git clone <repo_url> /tmp/target
cd /tmp/target
git checkout <fix_commit>~1

# Run emend detection
emend lint . --config /path/to/emend-sqli/.emend/rules.yaml
emend trace . --config /path/to/emend-sqli/.emend/rules.yaml --label sqli
```
