# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

Nothing published yet — everything below has landed on `main` but hasn't been
tagged or released to PyPI.

### Added
- Installable CLI: `pyproject.toml` with a `sitemap-generator` console_scripts
  entry point (`pip install .` / `pipx install .`), instead of requiring a
  git clone. `crawl_sitemap.py` at the repo root remains as a
  backward-compatible shim for anyone still running from a checkout.
- `--version` flag.
- Automated test suite (`tests/test_cli.py`, stdlib `unittest`) and a CI
  workflow (GitHub Actions) running it across Python 3.10–3.12 plus `ruff`
  linting on every push/PR.
- SSRF hardening: every fetch — including each redirect hop — now refuses
  hosts that resolve to a private/loopback/link-local/reserved address
  unless `--allow-private-ips` is passed.
- `--max-duration SECONDS`: an optional wall-clock cap on a crawl, on top of
  the existing `--max-pages`/`--max-depth`.
- `--sitemap-xml FILE`: writes a standards-compliant `sitemap.xml` from crawl
  results (`noindex` pages excluded).
- `--diff-against FILE`: compares this crawl's URLs against a previous
  `--json` snapshot and reports what was added/removed since then.
- `--render-js` (optional `js` extra): fetches pages through a headless
  Chromium instance instead of raw HTTP, so JavaScript-injected links are
  discoverable. `--chromium-path` points it at an existing Chrome/Chromium
  install instead of downloading one.

## [0.1.0] - unreleased

The original crawler: sitemap.xml discovery with BFS-crawl fallback and
cross-checking (`auto`/`sitemap`/`crawl`/`hybrid` modes), politeness controls
(robots.txt, rate limiting, retry/backoff), checkpointed resumable crawls,
concurrent workers over keep-alive connections, ETag/Last-Modified caching
(`--fresh`), `noindex` detection, per-page outbound link capture, coverage
verification (`--verify`), and the interactive HTML map (`--html`/`--serve`)
with edit mode, drag-to-reparent, and export.
