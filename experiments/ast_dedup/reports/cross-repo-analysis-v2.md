# Cross-repo AST duplicate report (2026-04-15)

Heuristics: repos>=2, lines>=5, nodes>=18, unique_tokens>=5, tokens>=10, named_tokens>=4

Cross-repo candidate clusters before post-filtering: 116
Interesting clusters after post-filtering: 0

## Interesting duplicates

_none_
## Rejected top matches

- `constant_assignment_block`: 2 repos, 6 lines at `django/db/migrations/operations/base.py:7-12`
- `constant_assignment_block`: 2 repos, 5 lines at `django/db/migrations/autodetector.py:27-31`
- `constant_class_body`: 2 repos, 6 lines at `django/db/migrations/autodetector.py:26-31`
- `too_few_tokens`: 2 repos, 7 lines at `django/template/backends/django.py:84-90`
- `too_short_lines`: 4 repos, 72 lines at `django/contrib/admin/options.py:1270-1273`
- `too_short_lines`: 4 repos, 4 lines at `django/contrib/auth/models.py:564-567`
- `too_short_lines`: 4 repos, 3 lines at `django/contrib/gis/db/models/sql/conversion.py:20-22`
- `too_short_lines`: 3 repos, 89 lines at `emend/cli_base.py:105-106`
- `too_short_lines`: 3 repos, 4 lines at `django/contrib/gis/serializers/geojson.py:76-77`
- `too_short_lines`: 3 repos, 4 lines at `django/contrib/auth/models.py:554-557`
