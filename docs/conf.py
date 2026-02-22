# Configuration file for the Sphinx documentation builder.

project = 'emend'
copyright = '2024, emend contributors'
author = 'emend contributors'
release = '0.0.1'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# Use furo theme for modern, responsive documentation
html_theme = 'furo'

# Autodoc settings
autodoc_member_order = 'bysource'

# Napoleon settings for Google-style docstrings
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
