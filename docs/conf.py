# Configuration file for the Sphinx documentation builder.

import os

project = 'emend'
copyright = '2024, emend contributors'
author = 'emend contributors'
release = os.environ.get('EMEND_VERSION', '0.0.0+dev')

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx_sitemap',
    'sphinxext.opengraph',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# Use furo theme for modern, responsive documentation
html_theme = 'furo'
html_title = 'emend: AST-aware multi-language refactoring CLI'

# Autodoc settings
autodoc_member_order = 'bysource'

# Napoleon settings for Google-style docstrings
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

# --- SEO -------------------------------------------------------------------
# Canonical base URL: furo emits <link rel="canonical"> from this, and
# sphinx-sitemap uses it to build sitemap.xml (referenced from
# https://lucaswiman.github.io/robots.txt, which governs this project site).
html_baseurl = "https://lucaswiman.github.io/emend/"
sitemap_url_scheme = "{link}"  # no lang/version prefix; docs are unversioned
sitemap_excludes = ["search.html", "genindex.html", "py-modindex.html"]

# Open Graph / Twitter card tags for link previews.
ogp_site_url = html_baseurl
ogp_site_name = "emend"
ogp_description_length = 200
ogp_type = "website"
ogp_custom_meta_tags = ['<meta name="twitter:card" content="summary">']
ogp_social_cards = {"enable": False}  # avoid an implicit matplotlib dependency
