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
        by = {k["keyword"]: k for k in kws}
        # C15: a pattern with a comma must survive CSV parsing intact
        self.assertTrue(re.search(by["Capacity / CU"]["pattern"],
                                  "A new look in the Capacity Metrics App", re.I))
        self.assertIn(r"\bf\d{1,3}\b capacity", by["Capacity / CU"]["pattern"])
        self.assertIn("SSAS / Analysis Services", by)
        self.assertIn("Throttling", by)

    def test_load_keywords_skips_unquoted_comma_rows(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "k.csv"
            f.write_text("group,keyword,pattern\nP,Bad,a{1,3}|b\nP,Good,\"a{1,3}|b\"\n", encoding="utf-8")
            with self.assertLogs(level="WARNING"):
                kws = fb.load_keywords(f)
        self.assertEqual([k["keyword"] for k in kws], ["Good"])
        self.assertEqual(kws[0]["pattern"], "a{1,3}|b")

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

    def test_stale_title_flag(self):
        self.assertTrue(fb.stale_title("Fabric September 2026 Feature Summary", "2026-10-12T23:56:01+07:00"))
        self.assertFalse(fb.stale_title("Fabric October 2026 Feature Summary", "2026-10-12T23:56:01+07:00"))
        self.assertFalse(fb.stale_title("Custom SQL Pools GA", "2026-10-12T22:00:00+07:00"))
        row = fb.annotate({"title": "Power BI September 2026 Feature Summary", "pub_date": "2026-10-09T01:20:42+07:00",
                           "category": "new"})
        self.assertEqual(row["flags"], "stale_title")
        self.assertEqual(row["region"], "")

    def test_job_region(self):
        self.assertEqual(fb.job_region("Principal Architect — FPT Asia Pacific Pte Ltd · United States · salary n/a"
                                       " | join our APAC Architecture team"), "APAC")
        self.assertEqual(fb.job_region("BI Lead — Acme · Vietnam · n/a"), "VN")
        self.assertEqual(fb.job_region("Power BI Developer — Tier One · United States · n/a"), "Other")
        self.assertEqual(fb.job_region("Analyst — Indiana Corp · United States · n/a"), "Other")
        self.assertEqual(fb.job_region("Analyst — X · Worldwide · n/a"), "Worldwide")
        row = fb.annotate({"title": "BI Lead — Acme · Singapore · n/a", "pub_date": "2026-10-12T00:00:00+07:00",
                           "category": "job"})
        self.assertEqual(row["region"], "APAC")

    def test_has_model_files(self):
        self.assertTrue(fb.has_model_files({"tree": [{"path": "Sales.SemanticModel/definition/tables/Sales.tmdl"}]}))
        self.assertTrue(fb.has_model_files({"tree": [{"path": "model/model.bim"}]}))
        self.assertFalse(fb.has_model_files({"tree": [{"path": "README.md"}, {"path": "app/tmdl_notes.txt"}]}))
        self.assertFalse(fb.has_model_files({}))

    def test_repo_search_keeps_only_repos_with_model(self):
        class Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        search = {"items": [
            {"full_name": "a/real", "html_url": "https://github.com/a/real", "stargazers_count": 1,
             "description": "", "pushed_at": "2026-10-10T00:00:00Z"},
            {"full_name": "b/noise", "html_url": "https://github.com/b/noise", "stargazers_count": 0,
             "description": "ERP", "pushed_at": "2026-10-10T00:00:00Z"}]}

        class Session:
            def get(self, url, **kw):
                if "/search/" in url:
                    return Resp(search)
                if "a/real" in url:
                    return Resp({"tree": [{"path": "x.SemanticModel/definition/model.tmdl"}]})
                return Resp({"tree": [{"path": "src/main.py"}]})

        rows = fb.fetch_one(Session(), "GitHub TMDL samples",
                            "https://api.github.com/search/repositories?q={query}+pushed:>{since}",
                            "T3", "sample", "gh_repo_search", "2026-10-13T00:00:00+07:00")
        self.assertEqual([r["url"] for r in rows], ["https://github.com/a/real"])

    def test_merge_replaces_sample_source(self):
        now = datetime.fromisoformat("2026-10-13T00:00:00+07:00")
        old = [{"url": "https://github.com/b/noise", "source": "GitHub TMDL samples", "pub_date": "2026-10-12T00:00:00+07:00",
                "title": "noise", "category": "sample"},
               {"url": "https://x/news", "source": "Fabric Blog", "pub_date": "2026-10-12T00:00:00+07:00",
                "title": "news", "category": "new"}]
        out = fb.merge(old, [], "2026-10-13T00:00:00+07:00", now, {"GitHub TMDL samples"})
        self.assertEqual([r["url"] for r in out], ["https://x/news"])
        kept = fb.merge(old, [], "2026-10-13T00:00:00+07:00", now)  # sample source failed: keep old rows
        self.assertEqual(len(kept), 2)

    def test_official_sample_repos(self):
        class Resp:
            def __init__(self, data, ok=True):
                self.data, self.ok = data, ok

            def raise_for_status(self):
                if not self.ok:
                    raise RuntimeError("404")

            def json(self):
                return self.data

        commit = [{"html_url": "https://github.com/microsoft/x/commit/1",
                   "commit": {"message": "Add retail TMDL sample\nbody", "committer": {"date": "2026-10-12T01:00:00Z"}}}]

        class Session:
            def get(self, url, **kw):
                return Resp(commit) if "powerbi-desktop-samples" in url else Resp(None, ok=False)

        rows = fb.fetch_one(Session(), "Microsoft model samples", "https://api.github.com/repos/{repo}/commits",
                            "T1", "sample", "gh_repo_commits", "2026-10-13T00:00:00+07:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "microsoft/powerbi-desktop-samples: Add retail TMDL sample")
        self.assertEqual(rows[0]["category"], "sample")

    def test_summary_feeds_trends(self):
        rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>What are Claude Dashboards?</title><link>https://example.com/d</link>
<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate>
<description>&lt;p&gt;Live DAX through a remote MCP connector.&lt;/p&gt;</description></item>
</channel></rss>"""
        rows = fb.parse_rss(rss, "Tabular Editor blog", "T2", "craft")
        self.assertEqual(rows[0]["summary"], "Live DAX through a remote MCP connector.")
        now = datetime.fromisoformat("2026-10-13T00:00:00+07:00")
        trends = fb.compute_trends(rows, [{"group": "AI on model", "keyword": "MCP", "pattern": r"\bmcp\b"}],
                                   now, "ts")
        self.assertEqual(trends[0]["news_7d"], 1)

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
            orig_kt = (fb.KEYWORDS, fb.TRENDS, fb.ARTICLES)
            fb.KEYWORDS, fb.TRENDS, fb.ARTICLES = Path(d) / "k.csv", Path(d) / "t.csv", Path(d) / "a.csv"
            try:
                code = fb.main(feeds=[("bad", "u", "T1", "new", "rss"), ("good", "u", "T1", "new", "rss")],
                               fetcher=fake)
                health = fb.read_csv(fb.HEALTH)
            finally:
                fb.LATEST, fb.HEALTH = orig
                fb.KEYWORDS, fb.TRENDS, fb.ARTICLES = orig_kt
        self.assertEqual(code, 0)
        self.assertEqual([h["status"] for h in health], ["error", "ok"])


    # ---- C28 / C29: trend counting
    def test_trends_skip_stale_and_dedupe_reposts(self):
        now = datetime.fromisoformat("2026-10-18T12:00:00+07:00")
        rows = [
            {"title": "What are Claude Dashboards?", "pub_date": "2026-10-12T10:00:00+07:00",
             "category": "craft", "source": "Tabular Editor blog", "url": "a"},
            {"title": "What are Claude dashboards", "pub_date": "2026-10-12T11:00:00+07:00",
             "category": "craft", "source": "r/PowerBI top day", "url": "b"},
            {"title": "Fabric September 2026 Feature Summary", "pub_date": "2026-10-13T00:00:00+07:00",
             "category": "new", "flags": "stale_title", "url": "c"},
            {"title": "Fabric Policies (Preview)", "pub_date": "2026-10-16T00:00:00+07:00",
             "category": "new", "url": "d"}]
        kws = [{"group": "A", "keyword": "Claude", "pattern": "claude"},
               {"group": "C", "keyword": "Fabric", "pattern": r"\bfabric\b"}]
        by = {r["keyword"]: r for r in fb.compute_trends(rows, kws, now, "ts")}
        self.assertEqual(by["Claude"]["news_7d"], 1)
        self.assertEqual(by["Fabric"]["news_7d"], 1)

    def test_trends_match_article_body(self):
        now = datetime.fromisoformat("2026-10-18T12:00:00+07:00")
        rows = [{"title": "Power BI October 2026 Feature Summary", "pub_date": "2026-10-16T04:25:00+07:00",
                 "category": "new", "url": "u"}]
        kws = [{"group": "P", "keyword": "Throttling", "pattern": r"throttl\w*|surge protection"}]
        self.assertEqual(fb.compute_trends(rows, kws, now, "ts")[0]["news_7d"], 0)
        self.assertEqual(fb.compute_trends(rows, kws, now, "ts",
                                           {"u": "More accurate surge protection"})[0]["news_7d"], 1)

    # ---- C18: ITviec
    def test_itviec_links(self):
        html = ('<a href="/it-jobs/senior-bi-developer-power-bi-pnj-1234?lab=x">a</a>'
                '<a href="/it-jobs/senior-bi-developer-power-bi-pnj-1234?lab=y">dup</a>'
                '<a href="https://itviec.com/it-jobs/data-architect-abc-56789">b</a>'
                '<a href="/it-jobs/power-bi">category page</a>')
        self.assertEqual(fb.itviec_job_links(html),
                         ["https://itviec.com/it-jobs/senior-bi-developer-power-bi-pnj-1234",
                          "https://itviec.com/it-jobs/data-architect-abc-56789"])

    def test_parse_jobposting(self):
        page = """<html><script type="application/ld+json">{"@context":"https://schema.org",
        "@type":"JobPosting","title":"Senior BI Engineer","datePosted":"2026-10-17T09:00:00+07:00",
        "hiringOrganization":{"@type":"Organization","name":"ACME"},
        "jobLocation":[{"@type":"Place","address":{"addressLocality":"Ho Chi Minh"}}],
        "description":"&lt;p&gt;Power BI, DAX, Microsoft Fabric and AI agents&lt;/p&gt;"}</script></html>"""
        row = fb.parse_jobposting(page, "https://itviec.com/it-jobs/x-1234", "ITviec jobs", "T3", "job")
        self.assertEqual(row["title"], "Senior BI Engineer — ACME · Ho Chi Minh, Vietnam · salary n/a")
        self.assertEqual(row["pub_date"], "2026-10-17T09:00:00+07:00")
        self.assertTrue(row["summary"].startswith("skills: fabric, dax, ai agent |"))
        self.assertEqual(fb.job_region(row["title"]), "VN")
        self.assertIsNone(fb.parse_jobposting("<html>no json</html>", "u", "s", "T3", "job"))

    def test_itviec_fetch_flow(self):
        class Resp:
            def __init__(self, text): self.text = text
            def raise_for_status(self): pass

        job = ('<script type="application/ld+json">{"@type":"JobPosting","title":"BI Dev",'
               '"datePosted":"2026-10-17","hiringOrganization":{"name":"X"},"description":"DAX"}</script>')

        class Sess:
            def get(self, url, **kw):
                if url.endswith("power-bi"):
                    return Resp('<a href="/it-jobs/bi-dev-x-1111">j</a>')
                if "/it-jobs/bi-dev-x-1111" in url:
                    return Resp(job)
                return Resp("")

        orig = fb.time.sleep
        fb.time.sleep = lambda s: None
        try:
            rows = fb.fetch_one(Sess(), "ITviec jobs", "https://itviec.com/it-jobs/{query}", "T3", "job",
                                "itviec", "2026-10-18T12:00:00+07:00")
        finally:
            fb.time.sleep = orig
        self.assertEqual(len(rows), 1)
        self.assertIn("BI Dev — X", rows[0]["title"])

    def test_itviec_no_links_is_error(self):
        class Resp:
            text = "<html>captcha</html>"
            def raise_for_status(self): pass

        class Sess:
            def get(self, url, **kw): return Resp()

        with self.assertRaises(RuntimeError):
            fb.fetch_one(Sess(), "ITviec jobs", "https://itviec.com/it-jobs/{query}", "T3", "job",
                         "itviec", "2026-10-18T12:00:00+07:00")

    # ---- C31: Vietnam job sources
    def test_parse_vnw(self):
        payload = {"data": [
            {"jobId": 123, "alias": "bi-lead", "jobUrl": "", "jobTitle": "BI Lead",
             "companyName": "ACME", "approvedOn": "2026-10-16T17:11:17+07:00",
             "jobDescription": "<p>Semantic model, DAX</p>", "jobRequirement": "",
             "skills": [{"skillName": "Power Bi"}, {"skillName": "Sql"}],
             "workingLocations": [{"cityNameVI": "Hồ Chí Minh"}], "prettySalary": "Thương lượng"},
            {"jobId": 0, "alias": "", "jobUrl": "", "jobTitle": "No link", "approvedOn": "2026-10-16"}]}
        rows = fb.parse_vnw(payload, "VietnamWorks jobs", "T3", "job")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["url"], "https://www.vietnamworks.com/bi-lead-123-jv")
        self.assertEqual(r["title"], "BI Lead — ACME · Hồ Chí Minh, Vietnam · Thương lượng")
        self.assertTrue(r["summary"].startswith("skills: dax, semantic model, power bi, sql |"))
        self.assertEqual(fb.job_region(r["title"]), "VN")

    def test_parse_linkedin(self):
        html = """<!DOCTYPE html><li><div class="base-card job-search-card">
<a class="base-card__full-link absolute" href="https://vn.linkedin.com/jobs/view/bi-architect-x-4480321530?position=1">
<span class="sr-only">BI Architect</span></a>
<h3 class="base-search-card__title"> Data Architect (Microsoft Fabric) </h3>
<h4 class="base-search-card__subtitle"><a class="hidden-nested-link" href="x"> Masan </a></h4>
<span class="job-search-card__location"> Ho Chi Minh City, Vietnam </span>
<time class="job-search-card__listdate" datetime="2026-10-15"> 3 days ago </time></div></li>
<li><div>broken card</div></li>"""
        rows = fb.parse_linkedin(html, "LinkedIn jobs (VN)", "T3", "job")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://vn.linkedin.com/jobs/view/bi-architect-x-4480321530")
        self.assertEqual(rows[0]["title"],
                         "Data Architect (Microsoft Fabric) — Masan · Ho Chi Minh City, Vietnam · salary n/a")
        self.assertTrue(rows[0]["pub_date"].startswith("2026-10-15"))
        self.assertTrue(rows[0]["summary"].startswith("skills: fabric |"))

    def test_vn_sources_survive_one_failing_query(self):
        class Resp:
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data

        calls = []

        class Sess:
            def post(self, url, json=None, **kw):
                calls.append(json["query"])
                if json["query"] == "power bi":
                    raise RuntimeError("429")
                return Resp({"data": [{"jobUrl": f"https://vnw/{json['query']}", "jobTitle": json["query"],
                                       "companyName": "C", "approvedOn": "2026-10-17"}]})

        rows = fb.fetch_one(Sess(), "VietnamWorks jobs", "u", "T3", "job", "vnw", "2026-10-18T12:00:00+07:00")
        self.assertEqual(len(rows), len(fb.VN_JOB_QUERIES) - 1)

    # ---- C18: T1 article text
    def test_build_articles(self):
        now = datetime.fromisoformat("2026-10-18T12:00:00+07:00")
        long_body = "x " * 400
        new = [
            {"source": "Fabric Blog", "tier": "T1", "title": "Long", "url": "l",
             "pub_date": "2026-10-16T00:00:00+07:00", "body": long_body},
            {"source": "Power BI Blog", "tier": "T1", "title": "Teaser", "url": "t",
             "pub_date": "2026-10-16T00:00:00+07:00", "body": "short"},
            {"source": "Power BI Blog", "tier": "T1", "title": "Old", "url": "o",
             "pub_date": "2026-10-01T00:00:00+07:00", "body": long_body},
            {"source": "SQLBI", "tier": "T2", "title": "Not T1 blog", "url": "s",
             "pub_date": "2026-10-16T00:00:00+07:00", "body": long_body},
            {"source": "Fabric Blog", "tier": "T1", "title": "Fabric September 2026 Feature Summary",
             "url": "st", "pub_date": "2026-10-16T00:00:00+07:00", "body": long_body, "flags": "stale_title"}]
        pages = []

        def get_page(u):
            pages.append(u)
            return '<div class="lia-message-body-content"><p>Full post text about surge protection</p></div>'

        out = {a["url"]: a for a in fb.build_articles([], new, now, "ts", get_page)}
        self.assertEqual(set(out), {"l", "t"})
        self.assertEqual(out["l"]["text_source"], "rss")
        self.assertEqual(out["t"]["text_source"], "page")
        self.assertIn("surge protection", out["t"]["text"])
        self.assertEqual(pages, ["t"])
        # stored article is not fetched again
        again = fb.build_articles(list(out.values()), new, now, "ts2", get_page)
        self.assertEqual(pages, ["t"])
        self.assertEqual({a["fetched_at"] for a in again}, {"ts"})

    def test_page_fetch_failure_keeps_rss_text(self):
        now = datetime.fromisoformat("2026-10-18T12:00:00+07:00")
        new = [{"source": "Power BI Blog", "tier": "T1", "title": "T", "url": "t",
                "pub_date": "2026-10-16T00:00:00+07:00", "body": "short teaser"}]

        def boom(u):
            raise RuntimeError("blocked")

        out = fb.build_articles([], new, now, "ts", boom)
        self.assertEqual(out[0]["text"], "short teaser")


if __name__ == "__main__":
    unittest.main()
