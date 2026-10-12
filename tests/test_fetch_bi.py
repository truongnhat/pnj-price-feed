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
            try:
                code = fb.main(feeds=[("bad", "u", "T1", "new", "rss"), ("good", "u", "T1", "new", "rss")],
                               fetcher=fake)
                health = fb.read_csv(fb.HEALTH)
            finally:
                fb.LATEST, fb.HEALTH = orig
        self.assertEqual(code, 0)
        self.assertEqual([h["status"] for h in health], ["error", "ok"])


if __name__ == "__main__":
    unittest.main()
