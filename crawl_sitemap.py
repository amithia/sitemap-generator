#!/usr/bin/env python3
"""Crawl a website and print its sitemap as a tree.

Works against any website. Uses only the Python standard library — no
pip installs needed.

Modes (--mode):
  auto     try sitemap.xml first; BFS-crawl only if no sitemap found (default)
  sitemap  sitemap.xml only
  crawl    BFS-crawl pages only, ignore sitemap.xml entirely
  hybrid   BFS-crawl AND seed the queue from sitemap.xml, then report which
           crawled pages were missing from the sitemap (and vice versa).
           Use this when you suspect the sitemap is incomplete.

Politeness / safety (important on live sites):
  - honours robots.txt Disallow rules and Crawl-Delay
  - waits --delay seconds (with random jitter) between requests
  - retries 429/5xx with exponential backoff, honouring Retry-After
  - aborts if many consecutive requests fail (you are probably being blocked)
  - --state FILE checkpoints progress so an interrupted crawl resumes
    instead of re-hitting pages

Usage:
  python3 crawl_sitemap.py https://example.com             # auto mode
  python3 crawl_sitemap.py https://example.com --mode hybrid \
      --state crawl-state.json --max-pages 3000 --delay 1.0 \
      --json sitemap.json
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import io
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser

DEFAULT_USER_AGENT = "sitemap-tree-crawler/1.1 (personal study tool; contact: set --user-agent)"
COMMON_SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml"]
SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|ico|css|js|mjs|json|xml|zip|gz|tar|mp[34]|"
    r"avi|mov|docx?|xlsx?|pptx?|woff2?|ttf|eot)$",
    re.IGNORECASE,
)
MAX_CONSECUTIVE_FAILURES = 15  # abort threshold: the site is probably blocking us
FETCH_RETRIES = 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


_conns = threading.local()  # per-thread keep-alive connection pool


def _connect(scheme: str, host: str, timeout: float) -> http.client.HTTPConnection:
    """Open a connection to host, tunnelling through an env-configured proxy."""
    proxy = urllib.request.getproxies().get(scheme)
    if proxy and not urllib.request.proxy_bypass(host):
        pp = urllib.parse.urlsplit(proxy)
        if scheme == "https":
            conn = http.client.HTTPSConnection(pp.hostname, pp.port or 3128, timeout=timeout)
            conn.set_tunnel(host)
        else:
            conn = http.client.HTTPConnection(pp.hostname, pp.port or 3128, timeout=timeout)
            conn._proxy_absolute = True  # plain-HTTP proxies want absolute request URIs
        return conn
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, timeout=timeout)


def _get_conn(scheme: str, host: str, timeout: float) -> http.client.HTTPConnection:
    pool = getattr(_conns, "pool", None)
    if pool is None:
        pool = _conns.pool = {}
    key = (scheme, host)
    if key not in pool:
        pool[key] = _connect(scheme, host, timeout)
    return pool[key]


def _drop_conn(scheme: str, host: str) -> None:
    pool = getattr(_conns, "pool", {})
    conn = pool.pop((scheme, host), None)
    if conn:
        conn.close()


def fetch(url: str, user_agent: str, cond: dict[str, str] | None = None,
          timeout: float = 20.0) -> tuple[bytes | None, int, dict[str, str], str]:
    """GET a URL over a per-thread keep-alive connection.

    Returns (body, http_status, response_headers, final_url); body is None on
    failure and on 304 Not Modified (send validators via `cond` to enable
    304s). final_url is where the request landed after redirects, so callers
    can record canonical addresses instead of alias URLs.
    Follows same-host redirects, retries 429/5xx with backoff honouring
    Retry-After, and reconnects once if a pooled connection has gone stale.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip",
    }
    if cond:
        headers.update(cond)

    for attempt in range(FETCH_RETRIES):
        target = url
        try:
            for _redirect in range(5):
                parts = urllib.parse.urlsplit(target)
                scheme, host = parts.scheme, parts.netloc
                conn = _get_conn(scheme, host, timeout)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                if getattr(conn, "_proxy_absolute", False):
                    path = target
                try:
                    conn.request("GET", path, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()  # must drain to reuse the connection
                except (http.client.HTTPException, ConnectionError, BrokenPipeError):
                    # stale keep-alive connection: reconnect once and retry in place
                    _drop_conn(scheme, host)
                    conn = _get_conn(scheme, host, timeout)
                    conn.request("GET", path, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                rheaders = dict(resp.getheaders())
                if resp.will_close:
                    _drop_conn(scheme, host)
                status = resp.status

                if status in (301, 302, 303, 307, 308):
                    location = rheaders.get("Location")
                    if not location:
                        return None, status, rheaders, target
                    target = urllib.parse.urljoin(target, location)
                    continue
                if status == 304:
                    return None, 304, rheaders, target
                if status in (429, 500, 502, 503, 504) and attempt < FETCH_RETRIES - 1:
                    retry_after = rheaders.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else 2.0 ** (attempt + 1)
                    except ValueError:
                        wait = 2.0 ** (attempt + 1)
                    wait = min(wait, 120.0)
                    log(f"  ! HTTP {status} on {url}, backing off {wait:.0f}s")
                    time.sleep(wait)
                    break  # next attempt
                if status >= 400:
                    log(f"  ! HTTP {status}: {target}")
                    return None, status, rheaders, target
                if rheaders.get("Content-Encoding") == "gzip" or target.endswith(".gz"):
                    try:
                        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
                    except OSError:
                        pass
                return data, status, rheaders, target
            else:
                log(f"  ! too many redirects: {url}")
                return None, 0, {}, url
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            _drop_conn(*urllib.parse.urlsplit(target)[:2])
            if attempt < FETCH_RETRIES - 1:
                time.sleep(2.0 ** (attempt + 1))
                continue
            log(f"  ! fetch failed: {url} ({exc})")
            return None, 0, {}, url
    return None, 0, {}, url


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.anchors: list[tuple[str, str]] = []  # (href, visible text)
        self.noindex = False
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
                self._href = href
                self._text = []
        elif tag == "meta":
            a = dict(attrs)
            if (a.get("name") or "").lower() == "robots" and \
                    "noindex" in (a.get("content") or "").lower():
                self.noindex = True

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.anchors.append((self._href, text))
            self._href = None
            self._text = []


MAX_LINKS_PER_PAGE = 300  # cap so one nav-heavy page can't bloat the JSON


def outbound_links(base_url: str, anchors: list[tuple[str, str]], host: str) -> list[dict]:
    """Resolve a page's <a> tags into a deduped, capped list of
    {url, text, external} for the "links on this page" panel."""
    seen: dict[str, dict] = {}
    for href, text in anchors:
        if href.startswith(("#", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        scheme = urllib.parse.urlsplit(absolute).scheme
        if scheme not in ("http", "https", "mailto", "tel"):
            continue
        if scheme in ("http", "https"):
            absolute = normalize(absolute)
        if absolute in seen:
            if text and not seen[absolute]["text"]:
                seen[absolute]["text"] = text[:120]
            continue
        if len(seen) >= MAX_LINKS_PER_PAGE:
            continue
        seen[absolute] = {
            "url": absolute,
            "text": text[:120],
            "external": scheme in ("mailto", "tel") or not same_host(absolute, host),
        }
    return list(seen.values())


def normalize(url: str) -> str:
    """Strip fragments and query strings, collapse trailing slashes."""
    parts = urllib.parse.urlsplit(url)
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def same_host(url: str, host: str) -> bool:
    return urllib.parse.urlsplit(url).netloc.lower() == host


# --- sitemap.xml discovery ---------------------------------------------------

def sitemap_urls_from_robots(base: str, user_agent: str) -> list[str]:
    data, _, _, _ = fetch(urllib.parse.urljoin(base, "/robots.txt"), user_agent)
    if not data:
        return []
    urls = []
    for line in data.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("sitemap:"):
            urls.append(line.split(":", 1)[1].strip())
    return urls


def parse_sitemap(url: str, seen: set[str], page_urls: set[str], host: str,
                  user_agent: str) -> None:
    """Parse a sitemap or sitemap-index URL, recursing into indexes."""
    if url in seen:
        return
    seen.add(url)
    data, _, _, _ = fetch(url, user_agent)
    if not data:
        return
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        log(f"  ! not valid XML: {url}")
        return
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.endswith("}loc") or el.tag == "loc" if el.text]
    if tag == "sitemapindex":
        log(f"  sitemap index: {url} ({len(locs)} child sitemaps)")
        for loc in locs:
            parse_sitemap(loc, seen, page_urls, host, user_agent)
    else:
        added = 0
        for loc in locs:
            if same_host(loc, host):
                page_urls.add(normalize(loc))
                added += 1
        log(f"  sitemap: {url} (+{added} urls)")


def collect_from_sitemaps(base: str, host: str, user_agent: str) -> set[str]:
    candidates = sitemap_urls_from_robots(base, user_agent)
    candidates += [urllib.parse.urljoin(base, p) for p in COMMON_SITEMAP_PATHS]
    page_urls: set[str] = set()
    seen: set[str] = set()
    for url in candidates:
        parse_sitemap(url, seen, page_urls, host, user_agent)
        if page_urls:
            break  # first working sitemap wins; robots entries were tried first
    return page_urls


# --- BFS crawl ----------------------------------------------------------------

class CrawlState:
    """Crawl progress, checkpointable to a JSON file for resuming."""

    def __init__(self, path: str | None, fresh: bool = False):
        self.path = path
        self.visited: set[str] = set()
        self.queue: deque[tuple[str, int]] = deque()
        self.found: set[str] = set()
        self.cache: dict[str, dict] = {}  # url -> {etag, lastmod, links}
        self.noindex: set[str] = set()
        self.page_links: dict[str, list] = {}  # url -> [{url,text,external}, ...]
        self.failed: dict[str, str] = {}     # url -> last error/status seen
        self.robots_skipped: set[str] = set()  # url -> disallowed by robots.txt
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.cache = data.get("cache", {})
            self.noindex = set(data.get("noindex", []))
            self.page_links = data.get("page_links", {})
            self.failed = data.get("failed", {})
            self.robots_skipped = set(data.get("robots_skipped", []))
            if fresh:
                log(f"Fresh re-crawl: keeping {len(self.cache)} cached validators, "
                    "resetting progress")
            else:
                self.visited = set(data["visited"])
                self.queue = deque((u, d) for u, d in data["queue"])
                self.found = set(data["found"])
                log(f"Resumed from {path}: {len(self.found)} pages done, "
                    f"{len(self.queue)} queued")

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"visited": sorted(self.visited),
                       "queue": list(self.queue),
                       "found": sorted(self.found),
                       "cache": self.cache,
                       "noindex": sorted(self.noindex),
                       "page_links": self.page_links,
                       "failed": self.failed,
                       "robots_skipped": sorted(self.robots_skipped)}, f)
        os.replace(tmp, self.path)


class RateLimiter:
    """Enforces a minimum (jittered) interval between request starts,
    shared across all workers — the site sees the same polite aggregate
    rate no matter how many threads are fetching."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next:
                    # jittered interval: bursts at exact intervals look more bot-like
                    self._next = now + self.delay * random.uniform(0.75, 1.25)
                    return
                pause = self._next - now
            time.sleep(pause)


def crawl(base: str, host: str, seeds: set[str], state: CrawlState,
          max_pages: int, max_depth: int, delay: float, user_agent: str,
          workers: int = 4) -> set[str]:
    robots = urllib.robotparser.RobotFileParser()
    robots.set_url(urllib.parse.urljoin(base, "/robots.txt"))
    try:
        robots.read()
    except Exception:
        pass  # if robots.txt is unreadable, can_fetch defaults to allow

    crawl_delay = robots.crawl_delay(user_agent)
    if crawl_delay and crawl_delay > delay:
        log(f"robots.txt asks for Crawl-Delay {crawl_delay}s — using it")
        delay = float(crawl_delay)

    if not state.queue and not state.found:
        start = normalize(base)
        state.queue.append((start, 0))
        state.visited.add(start)
        for seed in sorted(seeds):
            if seed not in state.visited:
                state.visited.add(seed)
                state.queue.append((seed, 0))

    limiter = RateLimiter(delay)
    lock = threading.Lock()          # guards state.* and the counters below
    work = threading.Condition(lock)
    in_flight = 0
    consecutive_failures = 0
    unchanged = 0
    stop = False

    def worker() -> None:
        nonlocal in_flight, consecutive_failures, stop, unchanged
        while True:
            with work:
                while not state.queue and in_flight and not stop:
                    work.wait()
                if stop or (not state.queue and not in_flight) or \
                        len(state.found) >= max_pages:
                    stop = True
                    work.notify_all()
                    return
                url, depth = state.queue.popleft()
                if not robots.can_fetch(user_agent, url):
                    state.robots_skipped.add(url)
                    continue
                cached = state.cache.get(url)
                in_flight += 1
            limiter.wait()
            cond = {}
            if cached:
                if cached.get("etag"):
                    cond["If-None-Match"] = cached["etag"]
                if cached.get("lastmod"):
                    cond["If-Modified-Since"] = cached["lastmod"]
            data, status, rheaders, final = fetch(url, user_agent, cond or None)
            final_url = normalize(final) if final else url
            offsite = not same_host(final_url, host)
            links: list[str] = []
            out_links: list[dict] = []
            fresh_hit = False
            noindex = False
            if status == 304 and cached:
                links = cached.get("links", [])
                noindex = cached.get("noindex", False)
                out_links = cached.get("out", [])
                fresh_hit = True
            elif data is not None:
                fresh_hit = True
                ctype = rheaders.get("Content-Type", "")
                if "html" in ctype or not ctype:
                    parser = LinkExtractor()
                    try:
                        parser.feed(data.decode("utf-8", "replace"))
                        links = parser.links
                        noindex = parser.noindex
                        out_links = outbound_links(final_url, parser.anchors, host)
                    except Exception:
                        links = []
            with work:
                in_flight -= 1
                if offsite and (fresh_hit or status == 304):
                    # redirected off this host: not a page of this site, not a failure
                    consecutive_failures = 0
                    fresh_hit = False
                    work.notify_all()
                    continue
                if fresh_hit:
                    if noindex:
                        state.noindex.add(final_url)
                    if status == 304:
                        unchanged += 1
                    elif rheaders.get("ETag") or rheaders.get("Last-Modified"):
                        state.cache[url] = {"etag": rheaders.get("ETag"),
                                            "lastmod": rheaders.get("Last-Modified"),
                                            "links": links, "noindex": noindex,
                                            "out": out_links}
                if not fresh_hit:
                    state.failed[url] = f"HTTP {status}" if status else "connection error"
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        log(f"Aborting: {consecutive_failures} consecutive failures "
                            f"(last HTTP status {status}). The site is likely rate-limiting "
                            "or blocking this crawler — wait a while, raise --delay, and "
                            "resume with --state.")
                        stop = True
                else:
                    consecutive_failures = 0
                    state.found.add(final_url)
                    state.visited.add(final_url)
                    if out_links:
                        state.page_links[final_url] = out_links
                    if len(state.found) % 25 == 0:
                        log(f"  crawled {len(state.found)} pages, queue {len(state.queue)}")
                        state.save()
                    if depth >= max_depth:
                        links = []
                    for href in links:
                        absolute = urllib.parse.urljoin(final_url, href)
                        if not absolute.startswith(("http://", "https://")):
                            continue
                        absolute = normalize(absolute)
                        path = urllib.parse.urlsplit(absolute).path
                        if not same_host(absolute, host) or SKIP_EXTENSIONS.search(path):
                            continue
                        if absolute not in state.visited:
                            state.visited.add(absolute)
                            state.queue.append((absolute, depth + 1))
                work.notify_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    try:
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log("\nInterrupted — saving state so the crawl can resume.")
        with work:
            stop = True
            work.notify_all()
    state.save()
    if unchanged:
        log(f"  {unchanged} pages unchanged since last crawl (304, no body transferred)")
    return state.found


# --- tree building and rendering ---------------------------------------------

def build_tree(urls: set[str]) -> dict:
    """Fold URL paths into a nested dict: {segment: {child: {...}}}."""
    tree: dict = {}
    for url in sorted(urls):
        path = urllib.parse.urlsplit(url).path
        node = tree
        for segment in [s for s in path.split("/") if s]:
            node = node.setdefault(segment, {})
    return tree


def render_ascii(tree: dict, root_label: str) -> str:
    lines = [root_label]

    def walk(node: dict, prefix: str) -> None:
        entries = sorted(node.items())
        for i, (name, children) in enumerate(entries):
            last = i == len(entries) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{name}")
            walk(children, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def render_markdown(tree: dict, root_label: str) -> str:
    lines = [f"# Sitemap for {root_label}", ""]

    def walk(node: dict, depth: int) -> None:
        for name, children in sorted(node.items()):
            lines.append(f"{'  ' * depth}- {name}")
            walk(children, depth + 1)

    walk(tree, 0)
    return "\n".join(lines) + "\n"


LANGUAGE_MIRROR_RE = re.compile(
    r"/(hi|vi|zh-hans|zh-hant|id|ms|ko|ja|th|km|ta|ne|si|bn|ur|ar|fr|de|es)(/|$)")


def build_verify_report(urls: set[str], state: "CrawlState") -> dict:
    """Cross-check crawl completeness: is every discovered internal link
    accounted for by a crawled page, a robots.txt exclusion, or a logged
    fetch failure? Anything left over is a genuine, unexplained gap."""
    robots_skipped = sorted(state.robots_skipped)
    lang_mirrors = [u for u in robots_skipped if LANGUAGE_MIRROR_RE.search(u)]
    other_robots = [u for u in robots_skipped if u not in set(lang_mirrors)]
    # visited = every same-host URL the crawler ever dequeued and processed,
    # whatever the outcome (crawled, redirected into an already-known page,
    # robots-excluded, or failed) — the real "was this handled" superset.
    accounted_for = urls | state.visited | state.robots_skipped | set(state.failed)
    unresolved: set[str] = set()
    for links in state.page_links.values():
        for link in links:
            if link["external"] or link["url"] in accounted_for:
                continue
            path = urllib.parse.urlsplit(link["url"]).path
            if SKIP_EXTENSIONS.search(path):
                continue
            unresolved.add(link["url"])
    return {
        "queue_exhausted": len(state.queue) == 0,
        "pages_crawled": len(state.found),
        "robots_disallowed_language_mirrors": len(lang_mirrors),
        "robots_disallowed_other": sorted(other_robots),
        "fetch_failed": dict(sorted(state.failed.items())),
        "unresolved_internal_links": sorted(unresolved),
    }


def log_verify_report(report: dict) -> None:
    log("\n--- Coverage verification ---")
    log(f"BFS queue exhausted: {'yes' if report['queue_exhausted'] else 'NO — crawl stopped early (raise --max-pages)'}")
    log(f"Pages crawled: {report['pages_crawled']}")
    log(f"Skipped via robots.txt: {report['robots_disallowed_language_mirrors']} language-mirror pages, "
        f"{len(report['robots_disallowed_other'])} other")
    if report["robots_disallowed_other"]:
        for u in report["robots_disallowed_other"][:10]:
            log(f"    robots-disallowed: {u}")
    if report["fetch_failed"]:
        log(f"Fetch failures: {len(report['fetch_failed'])}")
        for u, why in list(report["fetch_failed"].items())[:15]:
            log(f"    failed ({why}): {u}")
    unresolved = report["unresolved_internal_links"]
    if unresolved:
        log(f"UNEXPLAINED GAPS: {len(unresolved)} internal links found on crawled pages were "
            "never reached, never skipped by robots.txt, and never logged as a failure. "
            "These likely need --max-depth raised or a resumed crawl.")
        for u in unresolved[:15]:
            log(f"    unresolved: {u}")
        if len(unresolved) > 15:
            log(f"    ... and {len(unresolved) - 15} more (see --json output under 'verify')")
    else:
        log("No unexplained gaps: every internal link found during the crawl is accounted for.")


def run_scan(args, base: str, host: str) -> dict:
    """Run the full sitemap+crawl pipeline and return the result payload."""
    sitemap_urls: set[str] = set()
    if args.mode in ("auto", "sitemap", "hybrid"):
        log(f"Looking for XML sitemaps on {host}...")
        sitemap_urls = collect_from_sitemaps(base, host, args.user_agent)
        log(f"  {len(sitemap_urls)} URLs listed in sitemaps")

    urls: set[str] = set(sitemap_urls)
    crawled: set[str] = set()
    noindex_urls: set[str] = set()
    page_links: dict[str, list] = {}
    verify_report: dict | None = None
    if args.mode == "crawl" or args.mode == "hybrid" or (args.mode == "auto" and not sitemap_urls):
        seeds = sitemap_urls if args.mode == "hybrid" else set()
        log(f"BFS crawling {host} (max {args.max_pages} pages, depth {args.max_depth}, "
            f"~{args.delay}s between requests)...")
        state = CrawlState(args.state, fresh=args.fresh)
        crawled = crawl(base, host, seeds, state, args.max_pages, args.max_depth,
                        args.delay, args.user_agent, args.workers)
        noindex_urls = state.noindex
        page_links = state.page_links
        urls = crawled if args.mode == "crawl" else urls | crawled
        if getattr(args, "verify", False):
            verify_report = build_verify_report(urls, state)
    elif getattr(args, "verify", False):
        log("--verify has no effect in --mode sitemap (nothing was crawled to check).")

    if not urls:
        return {}
    payload = {"base": base, "mode": args.mode, "count": len(urls),
               "crawled_at": time.strftime("%Y-%m-%d"),
               "urls": sorted(urls), "tree": build_tree(urls),
               "noindex": sorted(noindex_urls & urls),
               "links_out": {u: page_links[u] for u in urls if u in page_links}}
    if args.mode == "hybrid" and sitemap_urls:
        payload["not_in_sitemap"] = sorted(crawled - sitemap_urls)
        payload["sitemap_only"] = sorted(sitemap_urls - crawled)
    if verify_report is not None:
        payload["verify"] = verify_report
        log_verify_report(verify_report)
    return payload


def render_html(payload: dict) -> str:
    template = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "chart.html")
    with open(template, encoding="utf-8") as f:
        html = f.read()
    return html.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))


CURRENT_STATE: CrawlState | None = None  # live progress for --serve status polling


def serve_map(args, base: str, host: str, payload: dict, port: int) -> None:
    import http.server

    holder = {"payload": payload, "running": False}

    def recrawl() -> None:
        holder["running"] = True
        try:
            fresh_args = argparse.Namespace(**vars(args))
            fresh_args.fresh = True
            result = run_scan(fresh_args, base, host)
            if result:
                holder["payload"] = result
        finally:
            holder["running"] = False

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = render_html(holder["payload"]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                st = CURRENT_STATE if holder["running"] else None
                self._json({"running": holder["running"],
                            "pages": len(st.found) if st else 0,
                            "queue": len(st.queue) if st else 0})
            elif self.path == "/api/data":
                self._json(holder["payload"])
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/recrawl":
                if not holder["running"]:
                    threading.Thread(target=recrawl, daemon=True).start()
                self._json({"ok": True}, 202)
            else:
                self.send_error(404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"Serving interactive map at http://127.0.0.1:{port}  (Ctrl-C to stop)")
    log("The Re-crawl button on the page re-runs the scan and refreshes the map.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\nServer stopped.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl a site and print its sitemap as a tree.")
    ap.add_argument("base_url", help="site to map, e.g. https://example.com")
    ap.add_argument("--mode", choices=["auto", "sitemap", "crawl", "hybrid"], default="auto",
                    help="auto: sitemap with crawl fallback; sitemap: sitemap.xml only; "
                         "crawl: parse pages only; hybrid: crawl seeded by sitemap and "
                         "report the differences (default: %(default)s)")
    ap.add_argument("--max-pages", type=int, default=2000, help="crawl page cap (default: %(default)s)")
    ap.add_argument("--max-depth", type=int, default=6, help="crawl depth limit (default: %(default)s)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="base seconds between request starts (site-wide, shared by all "
                         "workers); jitter is added (default: %(default)s)")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent fetch threads; the --delay rate limit is shared "
                         "across them, so this hides latency without hitting the site "
                         "harder (default: %(default)s)")
    ap.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                    help="identify yourself; include a contact email so site admins can "
                         "reach you instead of blocking you")
    ap.add_argument("--state", metavar="FILE",
                    help="checkpoint file: crawl progress is saved here and resumed on rerun")
    ap.add_argument("--fresh", action="store_true",
                    help="with --state: re-crawl everything from scratch but reuse cached "
                         "ETag/Last-Modified validators, so unchanged pages return cheap "
                         "304s instead of full downloads")
    ap.add_argument("--verify", action="store_true",
                    help="after crawling, cross-check that every internal link found on any "
                         "crawled page is accounted for (crawled, robots.txt-excluded, or a "
                         "logged fetch failure) and report anything left unexplained")
    ap.add_argument("--json", metavar="FILE", help="also write the tree + URL list as JSON")
    ap.add_argument("--markdown", metavar="FILE", help="also write the tree as a Markdown outline")
    ap.add_argument("--html", metavar="FILE",
                    help="also write the interactive flowchart map as a standalone HTML file")
    ap.add_argument("--serve", nargs="?", type=int, const=8600, metavar="PORT",
                    help="after scanning, serve the interactive map locally (default port "
                         "8600); the page's Re-crawl button re-runs the scan in place")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    host = urllib.parse.urlsplit(base).netloc.lower()
    if not host:
        ap.error("base_url must include a scheme, e.g. https://example.com")

    payload = run_scan(args, base, host)
    if not payload:
        log("No URLs discovered.")
        return 1

    print(render_ascii(payload["tree"], base))
    log(f"\n{payload['count']} URLs discovered.")
    if "not_in_sitemap" in payload:
        nis, only = payload["not_in_sitemap"], payload["sitemap_only"]
        log(f"hybrid report: {len(nis)} crawled pages are MISSING from the sitemap; "
            f"{len(only)} sitemap URLs were not reached by the crawl "
            "(raise --max-pages/--max-depth if that number should be 0).")
        for u in nis[:50]:
            log(f"  not in sitemap: {u}")
        if len(nis) > 50:
            log(f"  ... and {len(nis) - 50} more (see --json output)")
    if payload["noindex"]:
        log(f"{len(payload['noindex'])} pages carry a noindex meta tag.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        log(f"JSON written to {args.json}")
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(payload["tree"], base))
        log(f"Markdown written to {args.markdown}")
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(payload))
        log(f"Interactive map written to {args.html}")
    if args.serve is not None:
        serve_map(args, base, host, payload, args.serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
