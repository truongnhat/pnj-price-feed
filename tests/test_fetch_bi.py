import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_bi as fb  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Custom SQL Pools (Generally Available)</title>
<link>https://example.com/a</link><pubDate>Mon, 12 Oct 2026 15:00:00 GMT</pubDate></item>
<item><title>No date item</title><link>https://example.com/b</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>0.12.3</title><link rel="alternate" href="https://github.com/x/y/releases/tag/0.12.3"/>
<updated>2026-10-11T08:00:00Z</updated></entry>
<entry><title>Direct Lake feature (Preview)</title><link href="https://example.com/c"/>
<published>2026-10-10T01:00:00+00:00</published></entry>
</feed>"""


class TestBiFeed(unittest.TestCase):
    def test_rss_drops_undated_and_converts_time(self):
        rows = fb.parse_rss(RSS, "Fabric Blog", "T1", "new")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pub_date"], "2026-10-12T22:00:00+07:00")
        self.assertEqual(rows[0]["status"], "ga")

    def test_atom(self):
        rows = fb.parse_atom(ATOM, "rel", "T1", "new")
        self.assertEqual([r["url"] for r in rows],
                         ["https://github.com/x/y/releases/tag/0.12.3", "https://example.com/c"])
        self.assertEqual(rows[1]["status"], "preview")
        self.assertEqual(rows[0]["pub_date"], "2026-10-11T15:00:00+07:00")

    def test_status_guess(self):
        self.assertEqual(fb.guess_status("Deprecation of old file picker"), "deprecated")
        self.assertEqual(fb.guess_status("Some pattern"), "unknown")

    def test_gh_commits(self):
        payload = [{"html_url": "https://github.com/c/1",
                    "commit": {"message": "Update whats-new\n\nbody",
                               "committer": {"date": "2026-10-03T05:12:00Z"}}}]
        rows = fb.parse_gh_commits(payload, "Learn: Fabric", "T1", "new")
        self.assertEqual(rows[0]["pub_date"], "2026-10-03T12:12:00+07:00")
        self.assertIn("Update whats-new", rows[0]["title"])

    def test_repo_search(self):
        payload = {"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "stargazers_count": 3,
                              "description": "Retail TMDL", "pushed_at": "2026-10-10T00:00:00Z"}]}
        rows = fb.parse_gh_repo_search(payload, "GitHub TMDL samples", "T3", "sample")
        self.assertEqual(rows[0]["pub_date"], "2026-10-10T07:00:00+07:00")
        self.assertEqual(rows[0]["category"], "sample")

    def test_learn_page(self):
        html = ('<html><head><title>What\'s new in Power BI</title>'
                '<meta name="updated_at" content="2026-08-25T17:13:00Z"></head></html>')
        rows = fb.parse_learn_page(html, "Learn: Power BI", "T1", "new", "https://learn/x")
        self.assertEqual(rows[0]["pub_date"], "2026-08-26T00:13:00+07:00")
        self.assertEqual(rows[0]["url"], "https://learn/x#updated-2026-08-26")
        html2 = '<meta name="ms.date" content="08/18/2026">'
        self.assertEqual(fb.parse_learn_page(html2, "s", "T1", "new", "u")[0]["pub_date"][:10], "2026-08-18")

    def test_candidates_fallback(self):
        calls = []
        orig = fb.fetch_one

        def fake_one(session, name, url, tier, category, kind, ts):
            calls.append(url)
            if url == "bad":
                raise RuntimeError("404")
            return []
        fb.fetch_one = fake_one
        try:
            fb.fetch(None, "n", "bad|good", "T2", "craft", "rss", "2026-10-12T23:00:00+07:00")
        finally:
            fb.fetch_one = orig
        self.assertEqual(calls, ["bad", "good"])

    def test_himalayas(self):
        payload = {"jobs": [{"title": "Power BI Developer", "companyName": "Acme",
                             "locationRestrictions": ["Singapore"], "minSalary": 90000, "maxSalary": 120000,
                             "currency": "USD", "excerpt": "<p>DAX, Microsoft Fabric</p>",
                             "applicationLink": "https://himalayas.app/j/1", "pubDate": 1791095436}]}
        rows = fb.parse_himalayas(payload, "Himalayas jobs", "T3", "job")
        self.assertEqual(len(rows), 1)
        self.assertIn("Acme · Singapore · 90000-120000 USD | DAX, Microsoft Fabric", rows[0]["title"])
        self.assertTrue(rows[0]["pub_date"].endswith("+07:00"))

    def test_trends(self):
        now = datetime.fromisoformat("2026-10-13T00:00:00+07:00")
        rows = [{"title": "Direct Lake tips", "pub_date": "2026-10-12T00:00:00+07:00", "category": "craft"},
                {"title": "Direct Lake old", "pub_date": "2026-10-02T00:00:00+07:00", "category": "craft"},
                {"title": "MCP server preview", "pub_date": "2026-10-11T00:00:00+07:00", "category": "new"},
                {"title": "Data Architect — X | Direct Lake", "pub_date": "2026-10-10T00:00:00+07:00", "category": "job"},
                {"title": "repo with direct lake", "pub_date": "2026-10-12T00:00:00+07:00", "category": "sample"}]
        kws = [{"group": "M", "keyword": "Direct Lake", "pattern": r"direct\s?lake"},
               {"group": "A", "keyword": "MCP", "pattern": r"\bmcp\b"},
               {"group": "X", "keyword": "bad", "pattern": "("}]
        out = fb.compute_trends(rows, kws, now, "ts")
        by = {r["keyword"]: r for r in out}
        self.assertEqual((by["Direct Lake"]["news_7d"], by["Direct Lake"]["news_prev_7d"],
                          by["Direct Lake"]["jobs_14d"]), (1, 1, 1))
        self.assertEqual(by["MCP"]["delta"], 1)
        self.assertNotIn("bad", by)
        self.assertEqual(out[0]["keyword"], "MCP")

    def test_keywords_file_valid(self):
        import re
        kws = fb.load_keywords()
        self.assertGreater(len(kws), 20)
        for k in kws:
            re.compile(k["pattern"])

    def test_merge_keeps_first_seen_and_drops_old(self):
        now = datetime.fromisoformat("2026-10-12T23:00:00+07:00")
        old = [{"url": "u1", "timestamp": "2026-10-11T10:00:00+07:00", "pub_date": "2026-10-11T09:00:00+07:00",
                "source": "s", "title": "t"},
               {"url": "u2", "timestamp": "x", "pub_date": "2026-09-01T00:00:00+07:00", "source": "s", "title": "t"}]
        new = [{"url": "u1", "pub_date": "2026-10-11T09:00:00+07:00", "source": "s", "title": "t"},
               {"url": "u3", "pub_date": "2026-10-12T20:00:00+07:00", "source": "s", "title": "t"}]
        out = fb.merge(old, new, now.isoformat(), now)
        self.assertEqual([r["url"] for r in out], ["u3", "u1"])
        self.assertEqual(out[1]["timestamp"], "2026-10-11T10:00:00+07:00")

    def test_main_survives_failing_source(self):
        calls = []

        def fake(session, name, url, tier, category, kind, ts):
            calls.append(name)
            if name == "bad":
                raise RuntimeError("boom")
            return fb.parse_rss(RSS, name, tier, category)

        orig = (fb.LATEST, fb.HEALTH)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fb.LATEST, fb.HEALTH = Path(d) / "l.csv", Path(d) / "h.csv"
            orig_kt = (fb.KEYWORDS, fb.TRENDS)
            fb.KEYWORDS, fb.TRENDS = Path(d) / "k.csv", Path(d) / "t.csv"
            try:
                code = fb.main(feeds=[("bad", "u", "T1", "new", "rss"), ("good", "u", "T1", "new", "rss")],
                               fetcher=fake)
                health = fb.read_csv(fb.HEALTH)
            finally:
                fb.LATEST, fb.HEALTH = orig
                fb.KEYWORDS, fb.TRENDS = orig_kt
        self.assertEqual(code, 0)
        self.assertEqual([h["status"] for h in health], ["error", "ok"])


if __name__ == "__main__":
    unittest.main()
