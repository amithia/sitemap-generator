#!/usr/bin/env python3
"""Backward-compatible entry point for running from a repo checkout.

The actual implementation now lives in the `sitemap_generator` package
(see sitemap_generator/cli.py) so it can be installed via pip/pipx. This
shim keeps `python3 crawl_sitemap.py <url>` working for anyone running
straight from a git clone.
"""

import sys

from sitemap_generator.cli import main

if __name__ == "__main__":
    sys.exit(main())
