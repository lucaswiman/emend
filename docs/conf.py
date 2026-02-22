# Configuration file for the Sphinx documentation builder.

project = 'emend'
copyright = '2024, emend contributors'
author = 'emend contributors'
release = '0.0.1'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'myst_parser',
]

# MyST settings
myst_enable_extensions = [
    'colon_fence',
    'deflist',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'alabaster'

# Source suffix - support both rst and md
source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}
