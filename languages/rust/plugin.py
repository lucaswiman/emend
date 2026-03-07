from __future__ import annotations
from emend.language_plugins import (
    LanguagePlugin,
    NoOpImportHandler,
    RegexCommentHandler,
    TreeSitterPatternCompiler,
)

def create_plugin() -> LanguagePlugin:
    """Return a LanguagePlugin for Rust."""
    return LanguagePlugin(
        import_handler=NoOpImportHandler(),
        comment_handler=RegexCommentHandler("//"),
        pattern_compiler=TreeSitterPatternCompiler("rust"),
    )
