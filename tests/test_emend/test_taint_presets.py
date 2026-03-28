"""Tests for framework-specific taint presets."""

import pytest

from emend.taint import TaintConfig, run_taint_analysis
from emend.taint_presets import get_preset, list_presets, merge_configs


class TestPresetLoading:
    def test_list_presets(self):
        presets = list_presets()
        assert "flask" in presets
        assert "django" in presets
        assert "sqlalchemy" in presets
        assert "fastapi" in presets
        assert "all" in presets

    def test_get_flask_preset(self):
        config = get_preset("flask")
        assert "user_input" in config.labels
        assert len(config.sources) >= 5
        assert len(config.sinks) >= 5
        assert len(config.sanitizers) >= 3

    def test_get_django_preset(self):
        config = get_preset("django")
        assert "user_input" in config.labels
        assert any("request.GET" in s.pattern for s in config.sources)
        assert any("mark_safe" in s.pattern for s in config.sinks)

    def test_get_sqlalchemy_preset(self):
        config = get_preset("sqlalchemy")
        assert any("text($X)" in s.pattern for s in config.sinks)
        assert any("session.execute" in s.pattern for s in config.sinks)

    def test_get_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            get_preset("nonexistent")

    def test_merge_configs(self):
        c1 = TaintConfig(labels=["a"], sources=[], sinks=[], sanitizers=[])
        c2 = TaintConfig(labels=["b", "a"], sources=[], sinks=[], sanitizers=[])
        merged = merge_configs(c1, c2)
        assert sorted(merged.labels) == ["a", "b"]

    def test_get_all_preset(self):
        config = get_preset("all")
        # "all" should include sources from multiple frameworks
        patterns = [s.pattern for s in config.sources]
        assert any("request.args" in p for p in patterns)  # Flask
        assert any("request.GET" in p for p in patterns)  # Django

    def test_get_fastapi_preset(self):
        config = get_preset("fastapi")
        assert "user_input" in config.labels
        assert len(config.sinks) >= 5
        assert len(config.sanitizers) >= 3

    def test_merge_configs_sources_combined(self):
        flask = get_preset("flask")
        sqlalchemy = get_preset("sqlalchemy")
        merged = merge_configs(flask, sqlalchemy)
        # Flask sources + SQLAlchemy sinks
        flask_source_count = len(flask.sources)
        assert len(merged.sources) >= flask_source_count
        # Sinks should include both Flask and SQLAlchemy sinks
        sa_sink_patterns = {s.pattern for s in sqlalchemy.sinks}
        flask_sink_patterns = {s.pattern for s in flask.sinks}
        merged_patterns = {s.pattern for s in merged.sinks}
        assert sa_sink_patterns.issubset(merged_patterns)
        assert flask_sink_patterns.issubset(merged_patterns)

    def test_merge_configs_sanitizers_combined(self):
        c1 = TaintConfig(
            labels=["x"],
            sources=[],
            sinks=[],
            sanitizers=[],
        )
        c2 = TaintConfig(
            labels=["x"],
            sources=[],
            sinks=[],
            sanitizers=[],
        )
        merged = merge_configs(c1, c2)
        assert merged.labels == ["x"]

    def test_django_sources_count(self):
        config = get_preset("django")
        assert len(config.sources) >= 5

    def test_django_sanitizers_count(self):
        config = get_preset("django")
        assert len(config.sanitizers) >= 3

    def test_sqlalchemy_has_no_sources(self):
        config = get_preset("sqlalchemy")
        # SQLAlchemy preset has no sources (meant to be composed)
        assert len(config.sources) == 0

    def test_sqlalchemy_has_bindparam_sanitizer(self):
        config = get_preset("sqlalchemy")
        assert any("bindparam" in s.pattern for s in config.sanitizers)


class TestPresetIntegration:
    def test_flask_sql_injection(self, tmp_path):
        """Flask preset detects SQL injection."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )
        config = get_preset("flask")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_django_mark_safe_xss(self, tmp_path):
        """Django preset detects mark_safe XSS."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def view(request):\n"
            "    name = request.GET.get('name')\n"
            "    html = mark_safe(name)\n"
        )
        config = get_preset("django")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_sqlalchemy_text_injection(self, tmp_path):
        """SQLAlchemy preset detects text() injection when composed with Flask."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def query(request, session):\n"
            "    name = request.args.get('name')\n"
            "    stmt = text(name)\n"
        )
        config = merge_configs(get_preset("flask"), get_preset("sqlalchemy"))
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_flask_sanitizer_blocks(self, tmp_path):
        """Flask sanitizer (escape) blocks taint propagation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    name = escape(name)\n"
            "    cursor.execute(name)\n"
        )
        config = get_preset("flask")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) == 0

    def test_flask_command_injection(self, tmp_path):
        """Flask preset detects command injection via os.system."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "import os\n"
            "def run_cmd(request):\n"
            "    cmd = request.args.get('cmd')\n"
            "    os.system(cmd)\n"
        )
        config = get_preset("flask")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_django_cursor_sql_injection(self, tmp_path):
        """Django preset detects raw SQL injection via cursor.execute."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def search(request, cursor):\n"
            "    q = request.POST.get('q')\n"
            "    cursor.execute(q)\n"
        )
        config = get_preset("django")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_no_violations_when_sanitized(self, tmp_path):
        """Django sanitizer (int conversion) blocks taint."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def view(request, cursor):\n"
            "    user_id = request.GET.get('id')\n"
            "    user_id = int(user_id)\n"
            "    cursor.execute(user_id)\n"
        )
        config = get_preset("django")
        violations = run_taint_analysis([str(test_file)], config)
        assert len(violations) == 0
