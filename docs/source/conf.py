# Configuration file for the Sphinx documentation builder.
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys

sys.path.insert(0, os.path.abspath("../../src"))

project = "forcingkit"
copyright = "2026, Long Horizon Observatory"
author = "Daniel Fry"
release = "0.6.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_rtd_theme",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns: list[str] = []

language = "en"

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
# CNAME for the custom domain forcingkit.docs.lhzn.io, copied to the site root.
html_extra_path = ["_extra"]
