Language Support
================

emend provides multi-language support through a configuration-driven architecture
built on `tree-sitter <https://tree-sitter.github.io>`_.  Adding a new language
requires no changes to the core engine -- only configuration files and an
optional Python plugin module.

Built-in Languages
------------------

emend ships with first-class support for three languages:

.. list-table::
   :header-rows: 1
   :widths: 15 15 70

   * - Language
     - Extensions
     - Notes
   * - Python
     - ``.py``, ``.pyi``
     - Full support: imports via AST, docstrings, patterns, scope resolution,
       type oracle integration, dead-code analysis.
   * - TypeScript / JavaScript
     - ``.ts``, ``.tsx``, ``.js``, ``.jsx``
     - Symbols, patterns, scope resolution, JSDoc comment handling,
       import extraction, ``node_modules`` environment lookup.
   * - Rust
     - ``.rs``
     - Symbols, patterns, scope resolution, ``///`` doc comment handling,
       ``use`` import extraction, ``target/`` environment lookup.

Architecture Overview
---------------------

Language support is divided into three layers:

1. **Rust core** (``emend_core``): Tree-sitter parsing, scope resolution, pattern
   matching.  Driven entirely by TOML config files embedded at compile time.

2. **Language config** (``languages/<lang>/config.toml``): Declarative rules for
   scoping, bindings, imports, qualified names, symbols, pattern matching, and
   environment lookup.

3. **Python plugin** (``languages/<lang>/plugin.py``): Composable handlers for
   import manipulation, comment/docstring handling, and pattern compilation.

Adding a New Built-in Language
------------------------------

Follow these steps to add support for a new language directly in the emend
repository.

Step 1: Create language directory
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Create ``languages/<lang>/`` with three files:

.. code-block:: text

   languages/
     <lang>/
       config.toml    # Required: language configuration
       plugin.py      # Required: plugin handler composition
       symbols.scm    # Optional: tree-sitter query (documentation only)

Step 2: Write ``config.toml``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is the most important file.  It drives the Rust scope resolver, symbol
extractor, and pattern matcher.  See :ref:`config-reference` below for the full
specification.

Minimal example for a Go-like language:

.. code-block:: toml

   [language]
   name = "go"
   tree_sitter_grammar = "tree-sitter-go"
   file_extensions = ["go"]
   keywords = ["func", "var", "const", "type", "package", "import"]

   [scoping]
   [[scoping.scope_creators]]
   node = "source_file"
   kind = "module"

   [[scoping.scope_creators]]
   node = "function_declaration"
   kind = "function"

   [scoping.rules.function]
   is_closure_boundary = true
   names_visible_to_inner = true

   [scoping.rules.module]
   is_closure_boundary = false
   names_visible_to_inner = true

   [bindings]
   [[bindings.assignment]]
   node = "short_var_declaration"
   target = "left"

   [bindings.parameters]
   param_nodes = ["parameter_declaration"]
   name_field = "name"

   [bindings.definitions]
   function_def = "function_declaration"
   class_def = "type_declaration"
   name_field = "name"

   [imports]
   import_statement = "import_declaration"
   module_field = "path"
   name_field = "name"
   resolution = "go"
   dotted_name = "identifier"
   aliased_import = "import_spec"
   identifier = "identifier"

   [qualified_names]
   module_separator = "."
   class_member_prefix = true

   [exports]
   public_by_default = true

   [symbols]
   function_node = "function_declaration"
   class_node = "type_declaration"
   name_field = "name"
   parameters_field = "parameter_list"
   return_type_field = "result"
   body_field = "block"

   [pattern_matching]
   function_def = "function_declaration"
   class_def = "type_declaration"
   call = "call_expression"
   attribute = "selector_expression"
   identifier = "identifier"
   # ... map other pattern node types

   [builtins]
   names = ["error", "string", "int", "bool", "nil", "true", "false"]

Step 3: Write ``plugin.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The plugin file composes three handlers into a ``LanguagePlugin``:

.. code-block:: python

   from __future__ import annotations
   from emend.language_plugins import (
       LanguagePlugin,
       TreeSitterImportHandler,
       DocCommentHandler,
       TreeSitterPatternCompiler,
   )

   def create_plugin() -> LanguagePlugin:
       return LanguagePlugin(
           import_handler=TreeSitterImportHandler(
               language="go",
               extensions=["go"],
               import_keywords=("import",),
           ),
           comment_handler=DocCommentHandler("//", doc_style="line"),
           pattern_compiler=TreeSitterPatternCompiler("go"),
       )

Available handler implementations:

- ``TreeSitterImportHandler``: Generic import handler using the scope resolver.
  Works for any language whose ``config.toml`` defines an ``[imports]`` section.
- ``DocCommentHandler``: Extends ``RegexCommentHandler`` with doc comment support.
  ``doc_style="block"`` for ``/** */`` (JSDoc), ``doc_style="line"`` for ``///``.
- ``TreeSitterPatternCompiler``: Universal pattern compiler using tree-sitter.
- ``NoOpImportHandler``: Stub that performs no import operations.
- ``RegexCommentHandler``: Basic ``// noqa:`` handling without doc comment support.

Step 4: Add tree-sitter grammar (Rust core)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add the tree-sitter grammar crate to ``rust/Cargo.toml``:

.. code-block:: toml

   [dependencies]
   tree-sitter-go = "0.23"

Then update the parser dispatch in ``rust/src/pattern.rs``:

.. code-block:: rust

   // In get_parser():
   "go" => tree_sitter_go::LANGUAGE,

   // In parse_by_extension():
   "go" => { let mut p = get_parser("go"); p.parse(source.as_bytes(), None) },

And the config dispatch in ``rust/src/scope.rs``:

.. code-block:: rust

   const GO_CONFIG_TOML: &str = include_str!("../../languages/go/config.toml");

   // In config_for_ext():
   "go" => GO_CONFIG.get_or_init(|| {
       LanguageConfig::from_toml(GO_CONFIG_TOML)
           .expect("Failed to parse Go config")
   }),

Step 5: Add tests
~~~~~~~~~~~~~~~~~

Add tests in ``tests/test_emend/test_multi_language.py`` covering:

- File resolution
- Symbol collection
- Pattern matching
- Scope resolution
- Import handling
- Doc comment handling

.. _config-reference:

Config.toml Reference
---------------------

The language configuration file is divided into the following sections.

``[language]``
~~~~~~~~~~~~~~

.. code-block:: toml

   [language]
   name = "python"                          # Language identifier
   tree_sitter_grammar = "tree-sitter-python"  # Grammar crate name
   file_extensions = ["py", "pyi"]          # File extensions (no leading dot)
   keywords = ["def", "class", "import"]    # Language keywords (for scope resolver)

``[scoping]``
~~~~~~~~~~~~~

Defines which tree-sitter node types create new scopes:

.. code-block:: toml

   [[scoping.scope_creators]]
   node = "function_definition"    # Tree-sitter node type
   kind = "function"               # Scope kind: module, function, class, comprehension, block

   [scoping.rules.function]
   is_closure_boundary = true      # Whether this scope captures enclosing names
   names_visible_to_inner = true   # Whether names leak into nested scopes

``[bindings]``
~~~~~~~~~~~~~~

How variable names are introduced:

.. code-block:: toml

   [[bindings.assignment]]
   node = "assignment"         # AST node for assignments
   target = "left"             # Field containing the bound name

   [[bindings.loop]]
   node = "for_statement"
   target = "left"

   [bindings.parameters]
   param_nodes = ["identifier", "typed_parameter"]
   name_field = "name"

   [bindings.definitions]
   function_def = "function_definition"
   class_def = "class_definition"
   name_field = "name"

``[imports]``
~~~~~~~~~~~~~

Import statement recognition:

.. code-block:: toml

   [imports]
   import_statement = "import_statement"
   module_field = "module_name"
   name_field = "name"
   resolution = "python"          # Resolution strategy: python, node, rust
   dotted_name = "dotted_name"
   aliased_import = "aliased_import"
   identifier = "identifier"

``[qualified_names]``
~~~~~~~~~~~~~~~~~~~~~

How qualified names are constructed:

.. code-block:: toml

   [qualified_names]
   module_separator = "."         # "." for Python, "::" for Rust, "/" for TS
   class_member_prefix = true     # Include class name in member QNs
   nested_function_prefix = false # Include enclosing function name

``[exports]``
~~~~~~~~~~~~~

Visibility rules:

.. code-block:: toml

   [exports]
   public_by_default = true       # Whether symbols are public unless marked private
   private_prefix = "_"           # Name prefix that marks symbols as private

``[symbols]``
~~~~~~~~~~~~~

Symbol extraction configuration:

.. code-block:: toml

   [symbols]
   function_node = "function_definition"
   class_node = "class_definition"
   name_field = "name"
   parameters_field = "parameters"
   return_type_field = "return_type"
   body_field = "body"
   superclasses_field = "argument_list"
   decorator_node = "decorator"
   async_keyword = "async"

   [[symbols.param_types]]
   node = "identifier"
   name_source = "self"           # "self" (node text), "field:X" (field X), "child:N" (Nth child)

   [[symbols.param_separators]]
   node = "/"
   display = "/"

``[pattern_matching]``
~~~~~~~~~~~~~~~~~~~~~~

Maps abstract pattern node types to language-specific tree-sitter node names:

.. code-block:: toml

   [pattern_matching]
   function_def = "function_definition"
   class_def = "class_definition"
   call = "call"
   attribute = "attribute"
   identifier = "identifier"
   assignment = "assignment"
   return_stmt = "return_statement"
   # ... many more node type mappings
   func_field = "function"        # Field names for call expressions
   args_field = "arguments"
   object_field = "object"
   attr_field = "attribute"

``[environment_lookup]``
~~~~~~~~~~~~~~~~~~~~~~~~

Where to find symbols from dependencies:

.. code-block:: toml

   [environment_lookup]
   enabled = true
   paths = [".venv", "venv"]     # Python
   # paths = ["node_modules"]    # TypeScript
   # paths = ["target"]          # Rust

``[builtins]``
~~~~~~~~~~~~~~

Names that should be treated as always-available:

.. code-block:: toml

   [builtins]
   names = ["print", "len", "range", "True", "False", "None"]


Plugin Package System
---------------------

emend supports external language plugins distributed as Python packages.  This
allows adding language support without modifying the emend core.

Package structure
~~~~~~~~~~~~~~~~~

An external language plugin (e.g., ``emend-golang``) has this structure:

.. code-block:: text

   emend-golang/
     pyproject.toml
     src/
       emend_golang/
         __init__.py    # Module entry point
         config.toml    # Language configuration
         plugin.py      # Plugin handlers

Entry points
~~~~~~~~~~~~~

Register the language via the ``emend.languages`` entry point group:

.. code-block:: toml

   # pyproject.toml
   [project]
   name = "emend-golang"
   dependencies = ["emend"]

   [project.entry-points."emend.languages"]
   go = "emend_golang"

The entry point name (``go``) becomes the language identifier.  The value
(``emend_golang``) is the Python module whose directory contains ``config.toml``
and ``plugin.py``.

What the package must provide
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The module directory must contain:

1. ``config.toml`` -- Full language configuration (see :ref:`config-reference`).
2. ``plugin.py`` -- A module with a ``create_plugin()`` function returning a
   ``LanguagePlugin`` instance.
3. ``__init__.py`` -- Standard Python package init (can be empty).

.. note::

   External plugins cannot add new tree-sitter grammars to the Rust core.
   Pattern matching and scope resolution for plugin languages use the
   ``TreeSitterPatternCompiler`` and ``TreeSitterImportHandler`` which
   delegate to ``emend_core``.  The grammar must already be compiled into
   ``emend_core``, or the plugin must provide its own tree-sitter bindings.

Discovery order
~~~~~~~~~~~~~~~

emend discovers languages in this order:

1. **Built-in languages** from ``languages/*/config.toml`` (highest priority)
2. **Entry-point plugins** from installed packages (cannot override built-ins)
3. **Hardcoded fallbacks** for Python, TypeScript, and Rust

Language Plugin API
-------------------

Each language plugin composes three handler objects:

``ImportHandler``
~~~~~~~~~~~~~~~~~

.. code-block:: python

   class ImportHandler(ABC):
       def extract_imports(self, source: str) -> str: ...
       def add_import_text(self, import_str: str, position: int, source_code: str) -> str: ...
       def remove_import(self, source: str, module: str, name: str) -> str: ...

``CommentHandler``
~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class CommentHandler(ABC):
       @property
       def line_comment_prefix(self) -> str: ...
       def find_docstrings(self, source: str, symbol_byte_range: tuple[int, int]) -> list: ...
       def find_noqa_comments(self, source: str) -> dict: ...
       def rename_in_docstrings(self, content: str, old_name: str, new_name: str) -> str | None: ...

``PatternCompiler``
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class PatternCompiler(ABC):
       def compile(self, pattern_str: str) -> dict | None: ...
