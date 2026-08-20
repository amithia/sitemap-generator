"""Tests for sitemap_generator.cli.

Stdlib unittest only, matching the project's zero-dependency ethos —
run with `python3 -m unittest discover` (no pip installs needed).
"""

from __future__ import annotations

import argparse
import http.server
import os
import tempfile
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

from sitemap_generator import cli


class NormalizeTests(unittest.TestCase):
    def test_strips_fragment_and_query(self):
        self.assertEqual(cli.normalize("https://example.com/a?x=1#frag"),
                          "https://example.com/a")

    def test_collapses_trailing_slash(self):
        self.assertEqual(cli.normalize("https://example.com/a/"),
                          "https://example.com/a")

    def test_root_path_kept_as_slash(self):
        self.assertEqual(cli.normalize("https://example.com/"),
                          "https://example.com/")

    def test_lowercases_host(self):
        self.assertEqual(cli.normalize("https://Example.COM/a"),
                          "https://example.com/a")

    def test_collapses_duplicate_slashes(self):
        self.assertEqual(cli.normalize("https://example.com/a//b"),
                          "https://example.com/a/b")


class SameHostTests(unittest.TestCase):
    def test_matching_host(self):
        self.assertTrue(cli.same_host("https://example.com/a", "example.com"))

    def test_different_host(self):
        self.assertFalse(cli.same_host("https://other.com/a", "example.com"))

    def test_case_insensitive(self):
        self.assertTrue(cli.same_host("https://EXAMPLE.com/a", "example.com"))


class BuildTreeTests(unittest.TestCase):
    def test_folds_paths_into_nested_dict(self):
        urls = {
            "https://example.com/apply/domestic",
            "https://example.com/apply/international",
            "https://example.com/programs/undergraduate",
        }
        tree = cli.build_tree(urls)
        self.assertEqual(tree, {
            "apply": {"domestic": {}, "international": {}},
            "programs": {"undergraduate": {}},
        })

    def test_root_url_produces_empty_tree(self):
        self.assertEqual(cli.build_tree({"https://example.com/"}), {})


class RenderAsciiTests(unittest.TestCase):
    def test_renders_tree_command_style(self):
        tree = {"apply": {"domestic": {}}, "programs": {}}
        lines = cli.render_ascii(tree, "https://example.com").splitlines()
        self.assertEqual(lines[0], "https://example.com")
        self.assertIn("├── apply", lines)
        self.assertIn("│   └── domestic", lines)
        self.assertIn("└── programs", lines)


class RenderMarkdownTests(unittest.TestCase):
    def test_renders_bullet_outline(self):
        tree = {"apply": {"domestic": {}}}
        out = cli.render_markdown(tree, "https://example.com")
        self.assertIn("# Sitemap for https://example.com", out)
        self.assertIn("- apply", out)
        self.assertIn("  - domestic", out)


class RenderHtmlTests(unittest.TestCase):
    def test_embeds_payload_and_escapes_closing_script_tags(self):
        payload = {"base": "https://example.com", "note": "</script>alert(1)"}
        html = cli.render_html(payload)
        self.assertNotIn("__DATA__", html)
        self.assertIn("<\\/script>alert(1)", html)
        self.assertNotIn("</script>alert(1)", html)


class OutboundLinksTests(unittest.TestCase):
    def test_dedupes_and_classifies_external(self):
        anchors = [
            ("/about", "About"),
            ("/about", ""),  # duplicate, blank text keeps the first text seen
            ("https://other.com/x", "Other site"),
            ("#section", "Skip me"),
            ("javascript:void(0)", "Skip me too"),
            ("mailto:a@example.com", "Email"),
        ]
        links = cli.outbound_links("https://example.com/", anchors, "example.com")
        by_url = {link["url"]: link for link in links}
        self.assertEqual(len(links), 3)
        self.assertFalse(by_url["https://example.com/about"]["external"])
        self.assertEqual(by_url["https://example.com/about"]["text"], "About")
        self.assertTrue(by_url["https://other.com/x"]["external"])
        self.assertTrue(by_url["mailto:a@example.com"]["external"])

    def test_caps_links_per_page(self):
        anchors = [(f"/page{i}", "") for i in range(cli.MAX_LINKS_PER_PAGE + 50)]
        links = cli.outbound_links("https://example.com/", anchors, "example.com")
        self.assertEqual(len(links), cli.MAX_LINKS_PER_PAGE)


def _fake_fetch(responses: dict[str, str]):
    """A fetch() stand-in returning canned bodies for exact URLs, 404 otherwise."""
    def fetch(url, user_agent, cond=None, timeout=20.0):
        body = responses.get(url)
        if body is None:
            return None, 404, {}, url
        return body.encode(), 200, {}, url
    return fetch


class SitemapParsingTests(unittest.TestCase):
    def test_sitemap_urls_from_robots(self):
        robots = "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sitemap.xml\n"
        with patch.object(cli, "fetch", _fake_fetch({
            "https://example.com/robots.txt": robots,
        })):
            urls = cli.sitemap_urls_from_robots("https://example.com", "ua")
        self.assertEqual(urls, ["https://example.com/sitemap.xml"])

    def test_parse_sitemap_index_recurses_and_filters_other_hosts(self):
        index = (
            '<?xml version="1.0"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<sitemap><loc>https://example.com/sitemap-a.xml</loc></sitemap>'
            '</sitemapindex>'
        )
        leaf = (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>https://example.com/a</loc></url>'
            '<url><loc>https://other.com/b</loc></url>'
            '</urlset>'
        )
        responses = {
            "https://example.com/sitemap.xml": index,
            "https://example.com/sitemap-a.xml": leaf,
        }
        with patch.object(cli, "fetch", _fake_fetch(responses)):
            page_urls: set[str] = set()
            cli.parse_sitemap("https://example.com/sitemap.xml", set(), page_urls,
                               "example.com", "ua")
        self.assertEqual(page_urls, {"https://example.com/a"})

    def test_parse_sitemap_invalid_xml_is_ignored(self):
        with patch.object(cli, "fetch", _fake_fetch({
            "https://example.com/sitemap.xml": "not xml",
        })):
            page_urls: set[str] = set()
            cli.parse_sitemap("https://example.com/sitemap.xml", set(), page_urls,
                               "example.com", "ua")
        self.assertEqual(page_urls, set())


class VerifyReportTests(unittest.TestCase):
    def _state(self, **kwargs):
        state = cli.CrawlState(path=None)
        for key, value in kwargs.items():
            setattr(state, key, value)
        return state

    def test_no_gaps_when_everything_accounted_for(self):
        state = self._state(
            visited={"https://example.com/a"},
            page_links={"https://example.com/a": [
                {"url": "https://example.com/b", "text": "", "external": False},
            ]},
            found={"https://example.com/b"},
        )
        report = cli.build_verify_report(
            {"https://example.com/a", "https://example.com/b"}, state)
        self.assertEqual(report["unresolved_internal_links"], [])
        self.assertTrue(report["queue_exhausted"])

    def test_unexplained_gap_detected(self):
        state = self._state(
            visited={"https://example.com/a"},
            page_links={"https://example.com/a": [
                {"url": "https://example.com/missing", "text": "", "external": False},
            ]},
        )
        report = cli.build_verify_report({"https://example.com/a"}, state)
        self.assertEqual(report["unresolved_internal_links"], ["https://example.com/missing"])

    def test_robots_skipped_link_is_not_a_gap(self):
        state = self._state(
            visited={"https://example.com/a"},
            robots_skipped={"https://example.com/fr/page"},
            page_links={"https://example.com/a": [
                {"url": "https://example.com/fr/page", "text": "", "external": False},
            ]},
        )
        report = cli.build_verify_report({"https://example.com/a"}, state)
        self.assertEqual(report["unresolved_internal_links"], [])
        self.assertEqual(report["robots_disallowed_language_mirrors"], 1)

    def test_external_links_are_ignored(self):
        state = self._state(
            visited={"https://example.com/a"},
            page_links={"https://example.com/a": [
                {"url": "https://other.com/x", "text": "", "external": True},
            ]},
        )
        report = cli.build_verify_report({"https://example.com/a"}, state)
        self.assertEqual(report["unresolved_internal_links"], [])


class CrawlStateRoundTripTests(unittest.TestCase):
    def test_save_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = cli.CrawlState(path)
            state.visited.add("https://example.com/")
            state.queue.append(("https://example.com/a", 1))
            state.found.add("https://example.com/")
            state.save()

            resumed = cli.CrawlState(path)
            self.assertEqual(resumed.visited, {"https://example.com/"})
            self.assertEqual(list(resumed.queue), [("https://example.com/a", 1)])
            self.assertEqual(resumed.found, {"https://example.com/"})

    def test_fresh_keeps_cache_but_resets_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = cli.CrawlState(path)
            state.found.add("https://example.com/")
            state.cache["https://example.com/"] = {
                "etag": "abc", "links": [], "noindex": False, "out": []}
            state.save()

            fresh = cli.CrawlState(path, fresh=True)
            self.assertEqual(fresh.found, set())
            self.assertIn("https://example.com/", fresh.cache)


class _TestSiteHandler(http.server.BaseHTTPRequestHandler):
    """Serves a tiny fake site + robots.txt + sitemap.xml for end-to-end tests."""

    redirects: ClassVar[dict[str, str]] = {"/redirect": "/target"}
    pages: ClassVar[dict[str, str]] = {
        "/": '<a href="/apply">Apply</a> <a href="/programs">Programs</a>',
        "/apply": ('<a href="/apply/domestic">Domestic</a> '
                   '<a href="https://external.example/">Ext</a>'),
        "/apply/domestic": "No links here.",
        "/programs": '<meta name="robots" content="noindex">'
                     '<a href="/programs/cs">CS</a>',
        "/programs/cs": "Leaf page.",
        "/disallowed": "Listed in the sitemap but excluded by robots.txt.",
        "/target": "Landed.",
        "/robots.txt": "User-agent: *\nDisallow: /disallowed\n",
        "/sitemap.xml": (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>{root}/</loc></url>"
            "<url><loc>{root}/apply</loc></url>"
            "<url><loc>{root}/disallowed</loc></url>"
            "</urlset>"
        ),
    }

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in self.redirects:
            self.send_response(302)
            self.send_header("Location", self.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/sitemap.xml":
            body = body.format(root=f"http://{self.headers.get('Host')}")
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _LocalServerTestCase(unittest.TestCase):
    """Base class: spins up _TestSiteHandler on 127.0.0.1 for the duration of the class."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestSiteHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.host = f"127.0.0.1:{cls.server.server_port}"
        # These tests deliberately target loopback; opt out of the SSRF guard
        # that otherwise refuses private/loopback addresses by default.
        cli._ALLOW_PRIVATE_TARGETS = True

    @classmethod
    def tearDownClass(cls):
        cli._ALLOW_PRIVATE_TARGETS = False
        cls.server.shutdown()
        cls.thread.join()


class FetchRedirectTests(_LocalServerTestCase):
    def test_follows_redirect_and_reports_final_url(self):
        data, status, _headers, final = cli.fetch(f"{self.base}/redirect", "test-agent")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"Landed.")
        self.assertEqual(final, f"{self.base}/target")


class EndToEndCrawlTests(_LocalServerTestCase):
    """Exercises auto/sitemap/crawl/hybrid modes against a real (local) HTTP server."""

    def _args(self, **overrides):
        defaults = {
            "mode": "crawl", "max_pages": 50, "max_depth": 6, "delay": 0.0, "workers": 2,
            "user_agent": "test-agent", "state": None, "fresh": False, "verify": False,
            "json": None, "markdown": None, "html": None, "serve": None,
            "max_duration": None, "allow_private_ips": True,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_crawl_mode_discovers_linked_pages_only(self):
        payload = cli.run_scan(self._args(mode="crawl"), self.base, self.host)
        self.assertEqual(payload["count"], 5)
        for path in ("/", "/apply", "/apply/domestic", "/programs", "/programs/cs"):
            self.assertIn(f"{self.base}{path}" if path != "/" else f"{self.base}/",
                           payload["urls"])
        self.assertNotIn(f"{self.base}/disallowed", payload["urls"])

    def test_noindex_page_is_flagged(self):
        payload = cli.run_scan(self._args(mode="crawl"), self.base, self.host)
        self.assertIn(f"{self.base}/programs", payload["noindex"])

    def test_sitemap_mode_only_lists_sitemap_urls(self):
        payload = cli.run_scan(self._args(mode="sitemap"), self.base, self.host)
        self.assertEqual(payload["count"], 3)

    def test_auto_mode_prefers_sitemap_when_present(self):
        payload = cli.run_scan(self._args(mode="auto"), self.base, self.host)
        self.assertEqual(payload["count"], 3)
        self.assertNotIn("not_in_sitemap", payload)

    def test_hybrid_mode_reports_diff_in_both_directions(self):
        payload = cli.run_scan(self._args(mode="hybrid"), self.base, self.host)
        # crawled but not listed in the sitemap:
        self.assertIn(f"{self.base}/programs", payload["not_in_sitemap"])
        # listed in the sitemap but excluded by robots.txt, so never crawled:
        self.assertIn(f"{self.base}/disallowed", payload["sitemap_only"])

    def test_verify_reports_no_unexplained_gaps(self):
        payload = cli.run_scan(self._args(mode="crawl", verify=True), self.base, self.host)
        self.assertEqual(payload["verify"]["unresolved_internal_links"], [])
        self.assertTrue(payload["verify"]["queue_exhausted"])

    def test_max_duration_of_zero_stops_before_fetching_anything(self):
        # The budget check runs before a worker dequeues its first URL, so an
        # already-expired budget deterministically crawls zero pages.
        payload = cli.run_scan(self._args(mode="crawl", max_duration=0.0),
                                self.base, self.host)
        self.assertEqual(payload, {})

    def test_generous_max_duration_completes_normally(self):
        payload = cli.run_scan(self._args(mode="crawl", max_duration=30.0),
                                self.base, self.host)
        self.assertEqual(payload["count"], 5)


class SSRFGuardTests(_LocalServerTestCase):
    def test_is_public_host_classifies_known_ranges(self):
        self.assertFalse(cli._is_public_host("127.0.0.1"))         # loopback
        self.assertFalse(cli._is_public_host("10.0.0.5"))          # private
        self.assertFalse(cli._is_public_host("169.254.169.254"))   # link-local / cloud metadata
        self.assertFalse(cli._is_public_host("::1"))               # loopback (IPv6)
        self.assertTrue(cli._is_public_host("8.8.8.8"))

    def test_unresolvable_host_is_treated_as_unsafe(self):
        self.assertFalse(cli._is_public_host("this-host-does-not-resolve.invalid"))

    def test_fetch_refuses_private_target_by_default(self):
        cli._ALLOW_PRIVATE_TARGETS = False
        try:
            data, status, _headers, _final = cli.fetch(f"{self.base}/", "test-agent")
        finally:
            cli._ALLOW_PRIVATE_TARGETS = True  # restore the class-level opt-in
        self.assertIsNone(data)
        self.assertEqual(status, 0)

    def test_fetch_allows_private_target_with_opt_in(self):
        data, status, _headers, _final = cli.fetch(f"{self.base}/", "test-agent")
        self.assertEqual(status, 200)
        self.assertIsNotNone(data)


if __name__ == "__main__":
    unittest.main()
