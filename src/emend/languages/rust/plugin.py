from __future__ import annotations
from emend.language_plugins import (
    DocCommentHandler,
    LanguagePlugin,
    TreeSitterImportHandler,
    TreeSitterPatternCompiler,
)

def create_plugin() -> LanguagePlugin:
    """Return a LanguagePlugin for Rust."""
    return LanguagePlugin(
        import_handler=TreeSitterImportHandler(
            language="rust",
            extensions=["rs"],
            import_keywords=("use",),
        ),
        comment_handler=DocCommentHandler(doc_style="line", language="rust"),
        pattern_compiler=TreeSitterPatternCompiler("rust"),
    )
