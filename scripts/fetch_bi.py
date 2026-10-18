#!/usr/bin/env python3
"""BI feed for BI Agent (Agent #2): Power BI / Fabric news, patterns and samples.

Every source is public. A source that fails writes no rows; its failure is
recorded in data/bi_health.csv. No item is ever invented, and pub_date is the
date the source itself states (never the run time).

Output files (UTF-8, no BOM, "\n" line endings):
  - data/bi_latest.csv : one row per item published in the last KEEP_DAYS days,
                         newest first. Columns:
                         timestamp, source, tier, title, url, pub_date,
                         category, status, flags, region
                           timestamp : first time this run saw the item (VN time)
                           tier      : T1 official / T2 expert / T3 community
                           category  : new / craft / sample / job
                           status    : preview / ga / deprecated / unknown
                                       (keyword guess from the title; the agent
                                       must confirm it from the source)
                           summary   : first 300 chars of the item's own
                                       description (RSS/Atom), for keyword
                                       matching; empty when the source has none
                           flags     : ";"-joined markers, e.g. stale_title
                                       (the title names a month older than
                                       pub_date: a re-dated old post)
                           region    : job rows only: VN / APAC / Worldwide /
                                       Other (from locations + JD text)
  - data/bi_trends.csv : keyword counts from data/bi_keywords.csv (news 7d vs
                         previous 7d, jobs 14d), for the agent's Trend Radar.
  - data/bi_articles.csv : full text of T1 blog posts (Power BI / Fabric Blog)
                         from the last 7 days: url, source, tier, title,
                         pub_date, fetched_at, text_source (rss|page), text.
                         Lets the agent read T1 posts without WebFetch (C18).
  - data/bi_health.csv : one row per source: source, url, status, items,
                         newest_pub_date, error_msg, last_checked.

Exit code: 0 if at least one source worked, 1 if all failed.
"""

from __future__ import annotations

import csv
import html
import json
import time
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LATEST = DATA / "bi_latest.csv"
HEALTH = DATA / "bi_health.csv"

VN = timezone(timedelta(hours=7))
KEEP_DAYS = 14
TIMEOUT = 30

LATEST_COLS = ["timestamp", "source", "tier", "title", "url", "pub_date", "category", "status", "flags",
               "region", "summary"]
HEALTH_COLS = ["source", "url", "status", "items", "newest_pub_date", "error_msg", "last_checked"]

HEADERS = {
    "User-Agent": "pnj-price-feed-bi/1.0 (+https://github.com/truongnhat/pnj-price-feed)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, */*",
}

# (name, url, tier, category, kind)
#   kind: rss | atom | gh_commits | gh_repo_search | learn_page
#   A url may hold several candidates separated by "|"; the first that works wins.
FEEDS: list[tuple[str, str, str, str, str]] = [
    ("Power BI Blog",
     "https://community.fabric.microsoft.com/t5/s/rss/board?board.id=fbc_pbiupdatesblog",
     "T1", "new", "rss"),
    ("Fabric Blog",
     "https://community.fabric.microsoft.com/t5/s/rss/board?board.id=fbc_fabricupdatesblogs",
     "T1", "new", "rss"),
    ("Learn: What's new in Fabric",
     "https://api.github.com/repos/MicrosoftDocs/fabric-docs/commits?path=docs/fundamentals/whats-new.md&per_page=10",
     "T1", "new", "gh_commits"),
    ("Learn: Power BI latest update",
     "https://learn.microsoft.com/en-us/power-bi/fundamentals/desktop-latest-update",
     "T1", "new", "learn_page"),
    ("semantic-link-labs releases",
     "https://github.com/microsoft/semantic-link-labs/releases.atom", "T1", "new", "atom"),
    ("fabric-cicd releases",
     "https://github.com/microsoft/fabric-cicd/releases.atom", "T1", "new", "atom"),
    ("Tabular Editor 2 releases",
     "https://github.com/TabularEditor/TabularEditor/releases.atom", "T2", "new", "atom"),
    ("DAX Studio releases",
     "https://github.com/DaxStudio/DaxStudio/releases.atom", "T2", "new", "atom"),
    ("SQLBI", "https://www.sqlbi.com/feed/", "T2", "craft", "rss"),
    ("Chris Webb", "https://blog.crossjoin.co.uk/feed/", "T2", "craft", "rss"),
    ("Tabular Editor blog", "https://tabulareditor.com/blog/rss.xml", "T2", "craft", "rss"),
    ("fabric.guru (Sandeep Pawar)", "https://fabric.guru/rss.xml|https://fabric.guru/feed", "T2", "craft", "rss"),
    ("kerski.tech (John Kerski)",
     "https://www.kerski.tech/feed|https://www.kerski.tech/rss.xml|https://kerski.tech/feed", "T2", "craft", "rss"),
    ("powerbi.tips", "https://powerbi.tips/feed|https://powerbi.tips/rss.xml|https://powerbi.tips/index.xml",
     "T2", "craft", "rss"),
    ("Guy in a Cube (YouTube)",
     "https://www.youtube.com/feeds/videos.xml?channel_id=UCFp1vaKzpfvoGai0vE5VJ0w",
     "T2", "craft", "atom"),
    ("r/PowerBI top day", "https://www.reddit.com/r/PowerBI/top/.rss?t=day", "T3", "craft", "atom"),
    ("GitHub TMDL samples",
     "https://api.github.com/search/repositories?q={query}+pushed:>{since}&sort=updated&per_page=10",
     "T3", "sample", "gh_repo_search"),
    # Official sample repos: each recent commit = the sample changed that day.
    ("Microsoft model samples", "https://api.github.com/repos/{repo}/commits?per_page=5",
     "T1", "sample", "gh_repo_commits"),
    ("SQLBI samples", "https://api.github.com/repos/{repo}/commits?per_page=5",
     "T2", "sample", "gh_repo_commits"),
    # Job Radar: remote/regional jobs (Himalayas public API, no key).
    ("Himalayas jobs",
     "https://himalayas.app/jobs/api/search?q={query}&limit=20",
     "T3", "job", "himalayas"),
    # Vietnam jobs (C31). ITviec and TopCV block GitHub Actions (Cloudflare
    # 403, probe 18/10); VietnamWorks' search API and LinkedIn's public guest
    # job search answer.
    ("VietnamWorks jobs", "https://ms.vietnamworks.com/job-search/v1.0/search", "T3", "job", "vnw"),
    ("LinkedIn jobs (VN)",
     "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
     "?keywords={query}&location=Vietnam&f_TPR=r604800&start=0",
     "T3", "job", "linkedin"),
]

# Sources whose rows are re-checked on every run: a successful run replaces
# their old rows instead of adding to them.
REPLACE_EACH_RUN = {"GitHub TMDL samples"}

# Sample search is not tied to one industry (retail-only searches found no real
# model); every hit must still pass the model-file tree check.
SAMPLE_QUERIES = ["tmdl", "pbip", "SemanticModel", "topic:tmdl", "topic:pbip"]

SAMPLE_REPOS = {
    "Microsoft model samples": ["microsoft/powerbi-desktop-samples", "microsoft/fabric-samples",
                                "microsoft/Analysis-Services"],
    "SQLBI samples": ["sql-bi/Contoso-Data-Generator-V2"],
}

JOB_QUERIES = ["power bi", "microsoft fabric", "data architect", "analytics engineer", "semantic model"]

VN_JOB_QUERIES = ["power bi", "microsoft fabric", "data architect", "data engineer", "head of data"]
VNW_PER_QUERY = 20
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
ITVIEC_QUERIES = ["power-bi", "data-architect", "data-engineer"]
ITVIEC_PER_QUERY = 10
ITVIEC_BASE = "https://itviec.com"

ARTICLES = DATA / "bi_articles.csv"
ARTICLE_COLS = ["url", "source", "tier", "title", "pub_date", "fetched_at", "text_source", "text"]
ARTICLE_SOURCES = {"Power BI Blog", "Fabric Blog"}
ARTICLE_DAYS = 7
BODY_LEN = 8000
MIN_RSS_BODY = 600  # shorter RSS text = a teaser, so read the page instead

KEYWORDS = DATA / "bi_keywords.csv"
TRENDS = DATA / "bi_trends.csv"
TREND_COLS = ["group", "keyword", "news_7d", "news_prev_7d", "delta", "jobs_14d", "last_checked"]

ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------- helpers

def now_vn() -> datetime:
    return datetime.now(VN).replace(microsecond=0)


def to_vn_iso(value: str) -> str:
    """RFC 822 or ISO 8601 date -> ISO 8601 in VN time. Empty when unparseable."""
    value = (value or "").strip()
    if not value:
        return ""
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN).replace(microsecond=0).isoformat()


def clean(text: str) -> str:
    """Strip tags, decode entities (&amp; -> &), collapse spaces. Entities are
    decoded twice so escaped HTML (&lt;p&gt;) is also stripped."""
    text = html.unescape(text or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def guess_status(title: str) -> str:
    t = title.lower()
    if re.search(r"deprecat|retire|retirement|end of support|sunset|khai tử", t):
        return "deprecated"
    if re.search(r"\bpreview\b", t):
        return "preview"
    if re.search(r"generally available|\bga\b|now available", t):
        return "ga"
    return "unknown"


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}
TITLE_MONTH = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(20\d{2})\b", re.I)


def stale_title(title: str, pub_date: str) -> bool:
    """True when the title names a month/year earlier than pub_date's month, e.g.
    'September 2026 Feature Summary' re-dated to October: an old post, not news."""
    m = TITLE_MONTH.search(title or "")
    if not m:
        return False
    try:
        pub = datetime.fromisoformat(pub_date)
    except (TypeError, ValueError):
        return False
    return (int(m.group(2)), MONTHS[m.group(1).lower()]) < (pub.year, pub.month)


VN_RE = re.compile(r"viet\s?nam|ho chi minh|hanoi|ha noi|\bhcmc?\b|da nang", re.I)
APAC_RE = re.compile(r"\b(apac|asia|singapore|malaysia|thailand|indonesia|philippines|hong kong|"
                     r"australia|new zealand|japan|korea|taiwan|india)\b", re.I)
WORLD_RE = re.compile(r"\b(worldwide|anywhere)\b", re.I)


def job_region(title: str) -> str:
    """Region of a job row from its 'Title — Company · Locations · Salary | excerpt'
    text. Himalayas' location field can be wrong (an APAC team listed as United
    States), so the JD text counts too: VN beats APAC beats Worldwide."""
    text = title or ""
    if VN_RE.search(text):
        return "VN"
    if APAC_RE.search(text):
        return "APAC"
    if WORLD_RE.search(text):
        return "Worldwide"
    return "Other"


def annotate(row: dict) -> dict:
    """Fill flags/region (also for rows read back from an older CSV)."""
    flags = [f for f in (row.get("flags") or "").split(";") if f and f != "stale_title"]
    if stale_title(row.get("title", ""), row.get("pub_date", "")):
        flags.append("stale_title")
    row["flags"] = ";".join(flags)
    row["region"] = job_region(row.get("title", "")) if row.get("category") == "job" else ""
    return row


SUMMARY_LEN = 300


def item(source, tier, category, title, url, pub_date, summary: str = "") -> dict | None:
    title = clean(title)
    if not title or not url or not pub_date:
        return None  # never keep an undated item: the agent gates on pub_date
    return {"source": source, "tier": tier, "title": title, "url": url.strip(),
            "pub_date": pub_date, "category": category, "status": guess_status(title),
            "summary": clean(summary)[:SUMMARY_LEN],
            "body": clean(summary)[:BODY_LEN]}  # not written to bi_latest; feeds bi_articles


# ---------------------------------------------------------------- parsers

def parse_rss(text: str | bytes, source: str, tier: str, category: str) -> list[dict]:
    root = ET.fromstring(text)
    out = []
    for it in root.iter("item"):
        row = item(source, tier, category,
                   it.findtext("title", ""), it.findtext("link", ""),
                   to_vn_iso(it.findtext("pubDate", "") or it.findtext("{http://purl.org/dc/elements/1.1/}date", "")),
                   it.findtext("description", ""))
        if row:
            out.append(row)
    return out


def parse_atom(text: str | bytes, source: str, tier: str, category: str) -> list[dict]:
    root = ET.fromstring(text)
    out = []
    for e in root.iter(f"{ATOM}entry"):
        link = ""
        for l in e.findall(f"{ATOM}link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        date = e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated") or ""
        summary = (e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")
                   or e.findtext("{http://search.yahoo.com/mrss/}group/{http://search.yahoo.com/mrss/}description") or "")
        row = item(source, tier, category, e.findtext(f"{ATOM}title", ""), link, to_vn_iso(date), summary)
        if row:
            out.append(row)
    return out


def parse_gh_commits(payload: list, source: str, tier: str, category: str) -> list[dict]:
    """A commit to a Learn 'What's new' page = the page changed that day."""
    out = []
    for c in payload or []:
        commit = c.get("commit") or {}
        date = ((commit.get("committer") or {}).get("date")) or ((commit.get("author") or {}).get("date")) or ""
        msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
        row = item(source, tier, category, f"{source} updated: {msg}", c.get("html_url", ""), to_vn_iso(date))
        if row:
            out.append(row)
    return out


def parse_gh_repo_search(payload: dict, source: str, tier: str, category: str) -> list[dict]:
    """Repo search: pub_date = the repo's last push (a real date, unlike code search)."""
    out = []
    for r in (payload or {}).get("items", []):
        desc = r.get("description") or ""
        row = item(source, tier, category, f"{r.get('full_name', '')} ★{r.get('stargazers_count', 0)} — {desc}",
                   r.get("html_url", ""), to_vn_iso(r.get("pushed_at", "")))
        if row:
            out.append(row)
    return out


MODEL_FILE = re.compile(r"(\.tmdl$|\.SemanticModel/|\.SemanticModel$|\.bim$)", re.I)


def has_model_files(tree: dict) -> bool:
    """A real sample ships model text: TMDL files, a PBIP .SemanticModel folder or
    a model.bim. Repos that only mention 'tmdl/pbip' in words are noise."""
    return any(MODEL_FILE.search(t.get("path", "")) for t in (tree or {}).get("tree", []))


def _epoch_iso(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return to_vn_iso(str(value or ""))
    if v > 1e12:  # milliseconds
        v /= 1000
    return datetime.fromtimestamp(v, tz=timezone.utc).astimezone(VN).replace(microsecond=0).isoformat()


def parse_himalayas(payload: dict, source: str, tier: str, category: str) -> list[dict]:
    """Job title row: 'Title — Company · Locations · Salary | skills: excerpt'.
    The excerpt goes into the title so keyword trends can see the JD skills."""
    out = []
    for j in (payload or {}).get("jobs", []):
        locs = j.get("locationRestrictions") or []
        if isinstance(locs, list):
            locs = ", ".join(l if isinstance(l, str) else (l.get("name") or "") for l in locs)
        loc = locs or "Worldwide"
        lo, hi, cur = j.get("minSalary"), j.get("maxSalary"), j.get("currency") or ""
        salary = f"{lo}-{hi} {cur}".strip() if (lo or hi) else "salary n/a"
        excerpt = clean(j.get("excerpt") or "")[:160]
        title = f"{j.get('title', '')} — {j.get('companyName', '')} · {loc} · {salary}"
        if excerpt:
            title += f" | {excerpt}"
        url = j.get("applicationLink") or j.get("guid") or ""
        row = item(source, tier, category, title, url, _epoch_iso(j.get("pubDate")))
        if row:
            out.append(row)
    return out


ITVIEC_JOB_LINK = re.compile(r'href="((?:https://itviec\.com)?/it-jobs/[a-z0-9-]+-\d{3,7})(?=[?"#])', re.I)
SKILL_TAGS = [("fabric", r"\bfabric\b"), ("direct lake", r"direct\s?lake"), ("dax", r"\bdax\b"),
              ("ssas", r"\bssas\b|analysis services"), ("semantic model", r"semantic model"),
              ("ai agent", r"ai agents?|\bagentic\b"), ("llm", r"\bllm|genai|generative ai")]


def itviec_job_links(markup: str, limit: int = ITVIEC_PER_QUERY) -> list[str]:
    out = []
    for href in ITVIEC_JOB_LINK.findall(markup or ""):
        url = href if href.startswith("http") else ITVIEC_BASE + href
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _jsonld_objects(markup: str):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', markup or "", re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                yield obj
                stack.extend(obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else [])
            elif isinstance(obj, list):
                stack.extend(obj)


def parse_jobposting(markup: str, url: str, source: str, tier: str, category: str) -> dict | None:
    """One job page -> one row, from its schema.org JobPosting. No JobPosting or
    no datePosted -> None (never an undated job)."""
    for obj in _jsonld_objects(markup):
        kind = obj.get("@type")
        if kind != "JobPosting" and not (isinstance(kind, list) and "JobPosting" in kind):
            continue
        org = obj.get("hiringOrganization") or {}
        org = org.get("name", "") if isinstance(org, dict) else str(org)
        locs = obj.get("jobLocation") or []
        locs = locs if isinstance(locs, list) else [locs]
        places = []
        for loc in locs:
            addr = (loc or {}).get("address") or {} if isinstance(loc, dict) else {}
            if isinstance(addr, dict):
                place = addr.get("addressLocality") or addr.get("addressRegion") or ""
                if place and place not in places:
                    places.append(place)
        where = " - ".join(places) or "Vietnam"
        if "viet" not in where.lower():
            where += ", Vietnam"
        desc = clean(obj.get("description", ""))
        text = f"{obj.get('title', '')} {desc}"
        skills = [name for name, pat in SKILL_TAGS if re.search(pat, text, re.I)]
        summary = f"skills: {', '.join(skills) or 'none'} | {desc}"
        title = f"{obj.get('title', '')} — {org} · {where} · salary n/a"
        return item(source, tier, category, title, url, to_vn_iso(obj.get("datePosted", "")), summary)
    return None


def _vn_where(places: list[str]) -> str:
    where = " - ".join(p for p in places if p) or "Vietnam"
    return where if "viet" in where.lower() else where + ", Vietnam"


def _skill_summary(text: str, extra: list[str] | None = None, desc: str = "") -> str:
    skills = [name for name, pat in SKILL_TAGS if re.search(pat, text, re.I)]
    for e in extra or []:
        if e and e.lower() not in skills:
            skills.append(e.lower())
    return f"skills: {', '.join(skills) or 'none'} | {desc}"


def parse_vnw(payload: dict, source: str, tier: str, category: str) -> list[dict]:
    """VietnamWorks search API: one row per job, dated by approvedOn."""
    out = []
    for j in (payload or {}).get("data") or []:
        url = j.get("jobUrl") or ""
        if not url and j.get("alias") and j.get("jobId"):
            url = f"https://www.vietnamworks.com/{j['alias']}-{j['jobId']}-jv"
        places = []
        for loc in j.get("workingLocations") or []:
            name = (loc or {}).get("cityNameVI") or (loc or {}).get("cityName") or ""
            if name and name not in places:
                places.append(name)
        desc = clean(f"{j.get('jobDescription') or ''} {j.get('jobRequirement') or ''}")
        skills = [s.get("skillName", "") for s in (j.get("skills") or []) if isinstance(s, dict)]
        salary = j.get("prettySalary") or "salary n/a"
        title = f"{j.get('jobTitle', '')} — {j.get('companyName', '')} · {_vn_where(places)} · {salary}"
        row = item(source, tier, category, title, url, to_vn_iso(j.get("approvedOn") or ""),
                   _skill_summary(f"{j.get('jobTitle', '')} {desc}", skills, desc))
        if row:
            out.append(row)
    return out


LI_CARD = re.compile(r'<li>(.*?)</li>', re.S)


def _tag_text(markup: str, cls: str) -> str:
    m = re.search(r'class="[^"]*\b' + re.escape(cls) + r'\b[^"]*"[^>]*>(.*?)</', markup, re.S)
    return clean(m.group(1)) if m else ""


def parse_linkedin(markup: str, source: str, tier: str, category: str) -> list[dict]:
    """LinkedIn guest job search cards: title, company, location, date
    (<time datetime>), link. No description, so skills come from the title."""
    out = []
    for card in LI_CARD.findall(markup or ""):
        link = re.search(r'class="base-card__full-link[^"]*"[^>]*href="([^"?]+)', card)
        date = re.search(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})"', card)
        title = _tag_text(card, "base-search-card__title")
        if not (link and date and title):
            continue
        company = _tag_text(card, "hidden-nested-link") or _tag_text(card, "base-search-card__subtitle")
        where = _tag_text(card, "job-search-card__location")
        row = item(source, tier, category, f"{title} — {company} · {_vn_where([where])} · salary n/a",
                   link.group(1), to_vn_iso(date.group(1)), _skill_summary(title))
        if row:
            out.append(row)
    return out


def page_text(markup: str) -> str:
    """Main text of a community.fabric.microsoft.com post (Khoros); falls back
    to the whole page body when the post container is not found."""
    markup = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", markup or "")
    i = markup.find("lia-message-body-content")
    if i >= 0:
        markup = markup[i:]
        markup = markup[markup.find(">") + 1:]
    return clean(markup)[:BODY_LEN]


def build_articles(old: list[dict], new: list[dict], now: datetime, ts: str,
                   get_page: Callable[[str], str] | None = None) -> list[dict]:
    """Keep the full text of T1 blog posts from the last ARTICLE_DAYS days. A
    post already stored is not fetched again. RSS text is used when it is long
    enough; otherwise the page is read once (a failed read keeps the RSS text)."""
    cutoff = now - timedelta(days=ARTICLE_DAYS)

    def recent(r):
        try:
            return datetime.fromisoformat(r["pub_date"]) >= cutoff
        except (ValueError, KeyError):
            return False

    by_url = {a["url"]: a for a in old if recent(a)}
    for r in new:
        if r.get("source") not in ARTICLE_SOURCES or r["url"] in by_url or not recent(r):
            continue
        if "stale_title" in (r.get("flags") or ""):
            continue
        text, origin = r.get("body", ""), "rss"
        if len(text) < MIN_RSS_BODY and get_page:
            try:
                fetched = page_text(get_page(r["url"]))
                if len(fetched) > len(text):
                    text, origin = fetched, "page"
            except Exception as exc:  # keep the RSS text
                logging.info("article page %s failed: %s", r["url"], exc)
        if not text:
            continue
        by_url[r["url"]] = {"url": r["url"], "source": r["source"], "tier": r["tier"], "title": r["title"],
                            "pub_date": r["pub_date"], "fetched_at": ts, "text_source": origin, "text": text}
    return sorted(by_url.values(), key=lambda a: a["pub_date"], reverse=True)


# ---------------------------------------------------------------- trends

def load_keywords(path: Path = None) -> list[dict]:
    """Patterns containing commas must be double-quoted in the CSV; an unquoted
    comma splits the row and silently truncates the pattern (bug C15)."""
    rows = read_csv(path or KEYWORDS)
    out = []
    for r in rows:
        if r.get(None):
            logging.warning("keyword %r has extra columns %r: quote patterns containing commas",
                            r.get("keyword"), r.get(None))
            continue
        if r.get("keyword") and r.get("pattern"):
            out.append(r)
    return out


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def compute_trends(rows: list[dict], keywords: list[dict], now: datetime, ts: str,
                   bodies: dict[str, str] | None = None) -> list[dict]:
    """Per keyword: news items in the last 7 days vs the 7 days before, and
    job rows in the 14-day window. Sorted by delta, then volume.
    Matched on title + summary + stored article text (bi_articles).
    Rows flagged stale_title are skipped (C29); a news title seen twice
    (e.g. a blog post and its r/PowerBI repost) counts once (C28)."""
    bodies = bodies or {}
    out = []
    for k in keywords:
        try:
            pat = re.compile(k["pattern"], re.I)
        except re.error:
            continue
        n7 = p7 = jobs = 0
        seen = set()
        for r in rows:
            if "stale_title" in (r.get("flags") or ""):
                continue
            text = f"{r.get('title', '')} {r.get('summary', '')} {bodies.get(r.get('url', ''), '')}"
            if not pat.search(text):
                continue
            if r.get("category") not in ("job", "sample"):
                key = norm_title(r.get("title", ""))
                if key in seen:
                    continue
                seen.add(key)
            if r.get("category") == "job":
                jobs += 1
                continue
            if r.get("category") == "sample":
                continue
            try:
                age = now - datetime.fromisoformat(r["pub_date"])
            except (ValueError, KeyError):
                continue
            if age <= timedelta(days=7):
                n7 += 1
            elif age <= timedelta(days=14):
                p7 += 1
        out.append({"group": k.get("group", ""), "keyword": k["keyword"], "news_7d": n7, "news_prev_7d": p7,
                    "delta": n7 - p7, "jobs_14d": jobs, "last_checked": ts})
    out.sort(key=lambda r: (r["delta"], r["news_7d"] + r["jobs_14d"]), reverse=True)
    return out


def parse_learn_page(markup: str, source: str, tier: str, category: str, url: str) -> list[dict]:
    """A Learn page as one item dated by its own 'updated_at' metadata; the url
    carries the date so each page update becomes a new item."""
    m = (re.search(r'<meta[^>]+name="updated_at"[^>]+content="([^"]+)"', markup)
         or re.search(r'<meta[^>]+name="ms.date"[^>]+content="([^"]+)"', markup))
    if not m:
        return []
    raw = m.group(1)
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):  # ms.date is MM/DD/YYYY
        mm, dd, yy = raw.split("/")
        raw = f"{yy}-{mm}-{dd}T00:00:00+00:00"
    date = to_vn_iso(raw)
    title = clean(re.search(r"<title>(.*?)</title>", markup, re.S).group(1)) if "<title>" in markup else source
    row = item(source, tier, category, f"{title} (page updated)", f"{url}#updated-{date[:10]}", date)
    return [row] if row else []


# ---------------------------------------------------------------- fetch

def fetch(session: requests.Session, name, url, tier, category, kind, ts: str) -> list[dict]:
    last_exc = None
    for candidate in url.split("|"):
        try:
            return fetch_one(session, name, candidate, tier, category, kind, ts)
        except Exception as exc:  # try the next candidate
            last_exc = exc
    raise last_exc


def fetch_one(session: requests.Session, name, url, tier, category, kind, ts: str) -> list[dict]:
    if kind == "gh_repo_search":
        since = (datetime.fromisoformat(ts) - timedelta(days=KEEP_DAYS)).date().isoformat()
        rows, seen = [], set()
        for query in SAMPLE_QUERIES:
            for r in fetch_one(session, name, url.format(query=requests.utils.quote(query, safe=":"), since=since),
                               tier, category, "gh_repo_page", ts):
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                if repo_has_model(session, r["url"]):
                    rows.append(r)
        return rows
    if kind == "gh_repo_commits":
        rows, errors = [], []
        for repo in SAMPLE_REPOS.get(name, []):
            try:
                for r in fetch_one(session, name, url.format(repo=repo), tier, category, "gh_commits", ts):
                    r["title"] = f"{repo}: " + r["title"].split("updated: ", 1)[-1]
                    rows.append(r)
            except Exception as exc:  # one missing repo never hides the others
                errors.append(f"{repo}: {exc}")
                logging.warning("%s %s failed: %s", name, repo, exc)
        if errors and not rows and len(errors) == len(SAMPLE_REPOS.get(name, [])):
            raise RuntimeError("; ".join(errors)[:180])
        return rows
    if kind == "himalayas":
        rows, seen = [], set()
        for q in JOB_QUERIES:
            for r in fetch_one(session, name, url.format(query=requests.utils.quote(q)), tier, category,
                               "himalayas_page", ts):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    rows.append(r)
        return rows
    if kind == "vnw":
        rows, seen, errors = [], set(), []
        for q in VN_JOB_QUERIES:
            body = {"userId": 0, "query": q, "filter": [], "ranges": [],
                    "order": [{"field": "approvedOn", "value": "desc"}],
                    "hitsPerPage": VNW_PER_QUERY, "page": 0}
            try:
                resp = session.post(url, json=body, timeout=TIMEOUT,
                                    headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
                resp.raise_for_status()
            except Exception as exc:
                errors.append(f"{q}: {exc}")
                continue
            for r in parse_vnw(resp.json(), name, tier, category):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    rows.append(r)
        if errors and len(errors) == len(VN_JOB_QUERIES):
            raise RuntimeError("; ".join(errors)[:180])
        return rows
    if kind == "linkedin":
        rows, seen, errors = [], set(), []
        for q in VN_JOB_QUERIES:
            try:
                resp = session.get(url.format(query=requests.utils.quote(q)), timeout=TIMEOUT,
                                   headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
                resp.raise_for_status()
            except Exception as exc:  # LinkedIn may rate-limit one query
                errors.append(f"{q}: {exc}")
                continue
            for r in parse_linkedin(resp.text, name, tier, category):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    rows.append(r)
            time.sleep(1.0)
        if errors and len(errors) == len(VN_JOB_QUERIES):
            raise RuntimeError("; ".join(errors)[:180])
        return rows
    if kind == "itviec":
        page_headers = dict(HEADERS, Accept="text/html,application/xhtml+xml")
        rows, seen, listed = [], set(), 0
        for q in ITVIEC_QUERIES:
            resp = session.get(url.format(query=q), headers=page_headers, timeout=TIMEOUT)
            resp.raise_for_status()
            for job_url in itviec_job_links(resp.text):
                listed += 1
                if job_url in seen:
                    continue
                seen.add(job_url)
                try:
                    jr = session.get(job_url, headers=page_headers, timeout=TIMEOUT)
                    jr.raise_for_status()
                    row = parse_jobposting(jr.text, job_url, name, tier, category)
                    if row:
                        rows.append(row)
                except Exception as exc:  # one bad job page never hides the rest
                    logging.info("itviec job %s failed: %s", job_url, exc)
                time.sleep(0.5)
        if listed == 0:
            raise RuntimeError("ITviec: no job links found (layout changed or blocked)")
        return rows
    headers = dict(HEADERS)
    if kind.startswith("gh_") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        headers["Accept"] = "application/vnd.github+json"
    resp = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    if kind == "rss":
        return parse_rss(resp.content, name, tier, category)
    if kind == "atom":
        return parse_atom(resp.content, name, tier, category)
    if kind == "gh_commits":
        return parse_gh_commits(resp.json(), name, tier, category)
    if kind == "gh_repo_page":
        return parse_gh_repo_search(resp.json(), name, tier, category)
    if kind == "learn_page":
        return parse_learn_page(resp.text, name, tier, category, url)
    if kind == "himalayas_page":
        return parse_himalayas(resp.json(), name, tier, category)
    raise ValueError(f"unknown kind {kind}")


def repo_has_model(session: requests.Session, html_url: str) -> bool:
    """Check the default branch tree for model files (one API call per repo).
    Any error counts as 'no': an unverified repo is never offered as a sample."""
    full = html_url.rstrip("/").split("github.com/")[-1]
    headers = dict(HEADERS, Accept="application/vnd.github+json")
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        resp = session.get(f"https://api.github.com/repos/{full}/git/trees/HEAD?recursive=1",
                           headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        return has_model_files(resp.json())
    except Exception as exc:
        logging.info("tree check skipped %s: %s", full, exc)
        return False


def _get_page(session: requests.Session, url: str) -> str:
    resp = session.get(url, headers=dict(HEADERS, Accept="text/html"), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def merge(old: list[dict], new: list[dict], ts: str, now: datetime,
          replace_sources: set[str] | None = None) -> list[dict]:
    """Keep first-seen timestamp, drop items older than KEEP_DAYS, newest first.
    Sources in replace_sources are re-verified every run (TMDL samples), so their
    old rows are dropped unless this run returned them again."""
    replace_sources = replace_sources or set()
    by_url = {r["url"]: r for r in old if r.get("url") and r.get("source") not in replace_sources}
    for r in new:
        prev = by_url.get(r["url"])
        if prev:
            r["timestamp"] = prev.get("timestamp") or ts
        else:
            r["timestamp"] = ts
        by_url[r["url"]] = r
    cutoff = now - timedelta(days=KEEP_DAYS)
    keep = []
    for r in by_url.values():
        try:
            if datetime.fromisoformat(r["pub_date"]) >= cutoff:
                keep.append(r)
        except (ValueError, KeyError):
            continue
    keep.sort(key=lambda r: r["pub_date"], reverse=True)
    return [annotate(r) for r in keep]


def main(feeds=FEEDS, fetcher: Callable = fetch) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = now_vn()
    ts = now.isoformat()
    session = requests.Session()
    new_rows, health, ok = [], [], 0
    for name, url, tier, category, kind in feeds:
        try:
            rows = fetcher(session, name, url, tier, category, kind, ts)
            ok += 1
            newest = max((r["pub_date"] for r in rows), default="")
            health.append({"source": name, "url": url, "status": "ok", "items": len(rows),
                           "newest_pub_date": newest, "error_msg": "", "last_checked": ts})
            new_rows.extend(rows)
            logging.info("%s: %d items", name, len(rows))
        except Exception as exc:  # one bad source never stops the rest
            code = getattr(getattr(exc, "response", None), "status_code", "")
            status = "blocked" if code in (401, 403, 429, 451) else "error"
            health.append({"source": name, "url": url, "status": status, "items": 0,
                           "newest_pub_date": "", "error_msg": f"{code} {type(exc).__name__}: {exc}"[:200],
                           "last_checked": ts})
            logging.warning("%s failed: %s", name, exc)
    replace = {h["source"] for h in health if h["status"] == "ok"} & REPLACE_EACH_RUN
    latest = merge(read_csv(LATEST), new_rows, ts, now, replace)
    write_csv(LATEST, LATEST_COLS, latest)
    write_csv(HEALTH, HEALTH_COLS, health)
    by_url = {r["url"]: r for r in latest}
    for r in new_rows:  # carry flags from the merged rows onto this run's rows
        if r["url"] in by_url:
            r["flags"] = by_url[r["url"]].get("flags", "")
    articles = build_articles(read_csv(ARTICLES), new_rows, now, ts, lambda u: _get_page(session, u))
    write_csv(ARTICLES, ARTICLE_COLS, articles)
    keywords = load_keywords()
    if keywords:
        bodies = {a["url"]: a["text"] for a in articles}
        write_csv(TRENDS, TREND_COLS, compute_trends(latest, keywords, now, ts, bodies))
    logging.info("%d/%d sources ok", ok, len(feeds))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
