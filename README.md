# sitemap-generator

Map any website and turn it into a real sitemap: an ASCII tree, a Markdown
outline, or an interactive flowchart you can explore, reorganise, and export.
Python standard library only — no `pip install`, just Python 3.10+.

```bash
python3 crawl_sitemap.py https://example.com
```

```
https://example.com
├── apply
│   ├── domestic
│   └── international
└── programs
    ├── postgraduate
    │   └── master-of-computing
    └── undergraduate
        └── bachelor-of-science
```

## Why this exists

Most "sitemap generator" tools just read `sitemap.xml` and trust it. In
practice, XML sitemaps drift: pages get added without ever being listed,
old listings point at pages that no longer exist, and nobody notices
until someone asks "does this actually cover the whole site?" This tool
crawls the real, live site — following actual `<a href>` links, the way a
visitor (or a search engine) would — and can cross-reference that against
the XML sitemap to show you exactly where the two disagree.

## Quick start

```bash
# Map a site (tries sitemap.xml first, falls back to crawling)
python3 crawl_sitemap.py https://example.com

# Recommended for a full run: parse every page instead of trusting the
# sitemap, checkpoint progress, save results
python3 crawl_sitemap.py https://example.com --mode hybrid \
    --state crawl-state.json --delay 1.5 --max-pages 5000 \
    --user-agent "my-crawler/1.0 (you@example.com)" \
    --json sitemap.json --markdown sitemap.md

# Crawl only, ignore sitemap.xml entirely
python3 crawl_sitemap.py https://example.com --mode crawl --max-pages 500 --max-depth 4

# Verify coverage: did the crawl actually reach everything reachable?
python3 crawl_sitemap.py https://example.com --mode hybrid --state crawl-state.json --verify

# Interactive map: write it as a file, or serve it locally with a
# working "Re-crawl site" button that re-scans and refreshes the page
python3 crawl_sitemap.py https://example.com --mode hybrid --html map.html
python3 crawl_sitemap.py https://example.com --mode hybrid --state s.json --serve 8600
```

## The interactive map

`--html` / `--serve` render a flowchart-style map (template in
`templates/chart.html`): top-level sections fan out horizontally, pages
cascade vertically underneath, and standalone top-level pages (nothing
beneath them) sit in a panel at the end of the horizontal scroll so they
don't clutter the tree. Badges: a gold dot marks a page missing from the
XML sitemap; 🔒 marks a page carrying a `noindex` meta tag.

**Click any page's `🔗 N` badge** to open a side panel listing every link
found on that page — grouped into links staying on the site and links
leaving it (shown in a different colour with a trailing ↗), so you can
see exactly what a page points to without opening dev tools.

**Edit mode** turns the map into a planning tool: drag any card onto
another card to move it (and its subtree) under that page, drag onto a
sibling's edge to reorder without re-parenting, or onto the Standalone
panel to detach it. Use **+ New page** to sketch pages that don't exist
yet — they render in a distinct colour so planned work is never confused
with what's actually live. Edits apply to a draft copy; the original map
is untouched. The draft can be downloaded as a self-contained, still
further-editable HTML file, or as a Markdown outline. **Re-crawl** (served
mode only) re-runs the scan with cached validators and updates the map in
place.

## Verifying crawl coverage

`--verify` answers "did this actually reach everything?" with evidence,
not a guess. After crawling, it cross-checks every internal link found on
every crawled page against three explanations: the link was itself
crawled, it was excluded by `robots.txt` (tracked explicitly — e.g.
language-mirror paths like `/fr/`, `/de/`), or it failed to fetch (also
tracked, with the HTTP status). Anything left over — a link nobody can
account for — is reported as an **unexplained gap**, meaning `--max-depth`
or `--max-pages` cut the crawl off, or something else needs investigating.

```bash
python3 crawl_sitemap.py https://example.com --mode hybrid --state crawl-state.json --verify
```

The report (also embedded in `--json` output under `"verify"`) shows
whether the BFS queue was fully exhausted — reaching zero means every
link within reach was followed; a non-empty queue at `--max-pages` means
real content may still be out there. Combine with `--fresh` for the most
complete picture, since `robots.txt`-skip and failure tracking only
records what happens during the run that produces the report.

For an independent check, `hybrid` mode's `not in sitemap` / `sitemap
only` diff cross-references against the site's own `sitemap.xml` — two
different sources of truth agreeing is stronger evidence than either
alone.

## Modes

- `auto` (default): use sitemap.xml if it exists, otherwise crawl.
- `sitemap`: sitemap.xml only — fastest, but only shows what the site
  chooses to list.
- `crawl`: breadth-first crawl following `<a href>` links on every page.
  Finds pages regardless of whether they're in the sitemap.
- `hybrid`: crawl, but also seed the queue with every sitemap URL, then
  report `not in sitemap` (pages the crawl found that the sitemap omits)
  and `sitemap only` (listed pages no crawled page links to). This is the
  mode to use when you suspect the sitemap is incomplete.

## Crawling a live site responsibly

The crawler does most of this automatically, but the knobs matter:

- **Identify yourself**: pass `--user-agent` with a contact email so an
  admin who notices the traffic can email you rather than block you.
- **Go slow**: `--delay` is the base gap between request starts, site-wide
  (jitter is added; robots.txt `Crawl-Delay` is honoured if larger). 1–2s
  is a polite rate.
- **Use workers to go faster without hitting harder**: `--workers` (default
  4) fetches pages concurrently while the `--delay` rate limit is shared
  across all threads — the site sees the same requests-per-second, but you
  stop paying each request's network latency serially. Crawl time becomes
  roughly `pages × delay` instead of `pages × (delay + latency)`.
- **Cap the blast radius**: `--max-pages` and `--max-depth` stop the crawl
  from wandering into calendar pages, faceted listings, and other URL
  traps. Query strings are stripped for the same reason.
- **Checkpoint with `--state`**: progress is saved as you go, so a dropped
  connection or Ctrl-C resumes where it left off instead of re-hitting
  every page from scratch.
- **Cheap re-crawls with `--fresh`**: pages' ETag/Last-Modified validators
  are cached in the state file. `--state s.json --fresh` re-crawls the
  whole site but unchanged pages answer with an empty 304 instead of a
  full download. Connections are also kept alive and gzip-compressed, so
  each request skips the TCP/TLS handshake and transfers less.
- **Back off, don't push through**: 429/5xx responses are retried with
  exponential backoff honouring `Retry-After`, and the crawl aborts after
  ~15 consecutive failures — that pattern means you're being rate-limited
  or blocked, and the right response is to stop, wait, raise `--delay`,
  and resume later, not to work around the block.
- robots.txt `Disallow` rules are always respected; only GET requests are
  made and only HTML pages are fetched.

## How it works

1. **Sitemap discovery.** Reads `robots.txt` for `Sitemap:` entries and
   tries common locations (`/sitemap.xml`, `/sitemap_index.xml`). Sitemap
   indexes are followed recursively.
2. **Polite, concurrent BFS crawl.** Breadth-first crawls same-host HTML
   pages, extracting `<a href>` links with the stdlib HTML parser.
   Multiple worker threads fetch concurrently over persistent, keep-alive
   connections while sharing one rate limiter, so a crawl finishes faster
   without hitting the site any harder. Query strings, fragments, and
   asset files (PDF, images, CSS, JS, …) are skipped; redirects are
   followed and canonicalised so aliases collapse into one page instead
   of appearing as duplicates.
3. **Per-page link capture.** Every page's outbound links (internal and
   external) are recorded, deduped, and capped per page — this powers the
   interactive map's link-inspection panel.
4. **Tree rendering.** Discovered URLs are folded into a tree by path
   segment and printed like the Unix `tree` command; `--json`,
   `--markdown`, and `--html` write the same structure to files.

## Roadmap

- Generate a corrected `sitemap.xml` from crawl results
- Diff two crawls to show what changed over time
- Optional headless-browser rendering for JavaScript-heavy sites

## License

MIT — see [LICENSE](LICENSE).
