from __future__ import annotations
from emend.language_plugins import (
    DocCommentHandler,
    LanguagePlugin,
    TreeSitterImportHandler,
    TreeSitterPatternCompiler,
)

def create_plugin() -> LanguagePlugin:
    """Return a LanguagePlugin for TypeScript."""
    return LanguagePlugin(
        import_handler=TreeSitterImportHandler(
            language="typescript",
            extensions=["ts", "tsx", "js", "jsx"],
            import_keywords=("import", "require"),
        ),
        comment_handler=DocCommentHandler("//", doc_style="block"),
        pattern_compiler=TreeSitterPatternCompiler("typescript"),
    )
