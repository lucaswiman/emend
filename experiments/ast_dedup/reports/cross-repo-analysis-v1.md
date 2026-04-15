# Cross-repo AST duplicate report (2026-04-15)

Heuristics: repos>=2, lines>=5, nodes>=18, unique_tokens>=5, tokens>=10, named_tokens>=4

Cross-repo candidate clusters before post-filtering: 116
Interesting clusters after post-filtering: 3

## Interesting duplicates

### 1. 2 repos, 2 occurrences, 6 lines, 25 nodes

- `django` — `db/migrations/autodetector.py:26-31` (class_definition)
- `sqlalchemy` — `engine/interfaces.py:79-84` (class_definition)

Normalized shape:

```text
root=class_definition
kinds: class_definition identifier argument_list identifier block expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer
tokens: bound_0 free_0 bound_1 num bound_2 num bound_3 num bound_4 num bound_5 num
```

`django` snippet from `db/migrations/autodetector.py:26-31`:

```python
class Type(Enum):
        CREATE = 0
        REMOVE = 1
        ALTER = 2
        REMOVE_ORDER_WRT = 3
        ALTER_FOO_TOGETHER = 4
```

`sqlalchemy` snippet from `engine/interfaces.py:79-84`:

```python
class CacheStats(Enum):
    CACHE_HIT = 0
    CACHE_MISS = 1
    CACHING_DISABLED = 2
    NO_CACHE_KEY = 3
    NO_DIALECT_SUPPORT = 4
```

### 2. 2 repos, 2 occurrences, 6 lines, 25 nodes

- `django` — `db/migrations/operations/base.py:7-12` (block)
- `sqlalchemy` — `sql/selectable.py:4308-4313` (block)

Normalized shape:

```text
root=block
kinds: block expression_statement assignment identifier string expression_statement assignment identifier string expression_statement assignment identifier string expression_statement assignment identifier string expression_statement assignment identifier string expression_statement assignment identifier string
tokens: bound_0 str bound_1 str bound_2 str bound_3 str bound_4 str bound_5 str
```

`django` snippet from `db/migrations/operations/base.py:7-12`:

```python
ADDITION = "+"
    REMOVAL = "-"
    ALTERATION = "~"
    PYTHON = "p"
    SQL = "s"
    MIXED = "?"
```

`sqlalchemy` snippet from `sql/selectable.py:4308-4313`:

```python
UNION = "UNION"
    UNION_ALL = "UNION ALL"
    EXCEPT = "EXCEPT"
    EXCEPT_ALL = "EXCEPT ALL"
    INTERSECT = "INTERSECT"
    INTERSECT_ALL = "INTERSECT ALL"
```

### 3. 2 repos, 3 occurrences, 5 lines, 21 nodes

- `django` — `db/migrations/autodetector.py:27-31` (block)
- `sqlalchemy` — `engine/interfaces.py:80-84` (block)

Normalized shape:

```text
root=block
kinds: block expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer expression_statement assignment identifier integer
tokens: bound_0 num bound_1 num bound_2 num bound_3 num bound_4 num
```

`django` snippet from `db/migrations/autodetector.py:27-31`:

```python
CREATE = 0
        REMOVE = 1
        ALTER = 2
        REMOVE_ORDER_WRT = 3
        ALTER_FOO_TOGETHER = 4
```

`sqlalchemy` snippet from `engine/interfaces.py:80-84`:

```python
CACHE_HIT = 0
    CACHE_MISS = 1
    CACHING_DISABLED = 2
    NO_CACHE_KEY = 3
    NO_DIALECT_SUPPORT = 4
```

## Rejected top matches

- `too_few_tokens`: 2 repos, 7 lines at `django/template/backends/django.py:84-90`
- `too_short_lines`: 4 repos, 72 lines at `django/contrib/admin/options.py:1270-1273`
- `too_short_lines`: 4 repos, 4 lines at `django/contrib/auth/models.py:564-567`
- `too_short_lines`: 4 repos, 3 lines at `django/contrib/gis/db/models/sql/conversion.py:20-22`
- `too_short_lines`: 3 repos, 89 lines at `emend/cli_base.py:105-106`
- `too_short_lines`: 3 repos, 4 lines at `django/contrib/gis/serializers/geojson.py:76-77`
- `too_short_lines`: 3 repos, 4 lines at `django/contrib/auth/models.py:554-557`
- `too_short_lines`: 3 repos, 4 lines at `django/contrib/admin/filters.py:78-81`
- `too_short_lines`: 3 repos, 3 lines at `django/contrib/gis/gdal/field.py:211-212`
- `too_short_lines`: 3 repos, 3 lines at `emend/trace_presets.py:774-776`
