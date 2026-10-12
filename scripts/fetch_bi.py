#!/usr/bin/env python3
"""BI feed for BI Agent (Agent #2): Power BI / Fabric news, patterns and samples.

Every source is public. A source that fails writes no rows; its failure is
recorded in data/bi_health.csv. No item is ever invented, and pub_date is the
date the source itself states (never the run time).

Output files (UTF-8, no BOM, "\n" line endings):
  - data/bi_latest.csv : one row per item published in the last KEEP_DAYS days,
                         newest first. Columns:
                         timestamp, source, tier, title, url, pub_date,
                         category, status
                           timestamp : first time this run saw the item (VN time)
                           tier      : T1 official / T2 expert / T3 community
                           category  : new / craft / sample
                           status    : preview / ga / deprecated / unknown
                                       (keyword guess from the title; the agent
                                       must confirm it from the source)
  - data/bi_health.csv : one row per source: source, url, status, items,
                         newest_pub_date, error_msg, last_checked.

Exit code: 0 if at least one source worked, 1 if all failed.
"""

from __future__ import annotations

import csv
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

LATEST_COLS = ["timestamp", "source", "tier", "title", "url", "pub_date", "category", "status"]
HEALTH_COLS = ["source", "url", "status", "items", "newest_pub_date", "error_msg", "last_checked"]

HEADERS = {
    "User-Agent": "pnj-price-feed-bi/1.0 (+https://github.com/truongnhat/pnj-price-feed)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, */*",
}

# (name, url, tier, category, kind)
#   kind: rss | atom | gh_commits | gh_code_search
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
     "https://api.github.com/repos/MicrosoftDocs/powerbi-docs/commits?path=powerbi-docs/fundamentals/desktop-latest-update.md&per_page=10",
     "T1", "new", "gh_commits"),
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
    ("powerbi.tips", "https://powerbi.tips/feed/", "T2", "craft", "rss"),
    ("Guy in a Cube (YouTube)",
     "https://www.youtube.com/feeds/videos.xml?channel_id=UCFp1vaKzpfvoGai0vE5VJ0w",
     "T2", "craft", "atom"),
    ("r/PowerBI top day", "https://www.reddit.com/r/PowerBI/top/.rss?t=day", "T3", "craft", "atom"),
    ("GitHub TMDL samples",
     "https://api.github.com/search/code?q=extension:tmdl+retail+OR+sales+OR+inventory&sort=indexed&per_page=20",
     "T3", "sample", "gh_code_search"),
]

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
    text = re.sub(r"<[^>]+>", " ", text or "")
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


def item(source, tier, category, title, url, pub_date) -> dict | None:
    title = clean(title)
    if not title or not url or not pub_date:
        return None  # never keep an undated item: the agent gates on pub_date
    return {"source": source, "tier": tier, "title": title, "url": url.strip(),
            "pub_date": pub_date, "category": category, "status": guess_status(title)}


# ---------------------------------------------------------------- parsers

def parse_rss(text: str | bytes, source: str, tier: str, category: str) -> list[dict]:
    root = ET.fromstring(text)
    out = []
    for it in root.iter("item"):
        row = item(source, tier, category,
                   it.findtext("title", ""), it.findtext("link", ""),
                   to_vn_iso(it.findtext("pubDate", "") or it.findtext("{http://purl.org/dc/elements/1.1/}date", "")))
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
        row = item(source, tier, category, e.findtext(f"{ATOM}title", ""), link, to_vn_iso(date))
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


def parse_gh_code_search(payload: dict, source: str, tier: str, category: str, ts: str) -> list[dict]:
    """Code search gives no file date; use the run date only for repos seen for
    the first time (handled in merge) — here we tag pub_date with the run time
    and mark the title so the agent knows the date is the discovery date."""
    out, seen = [], set()
    for hit in (payload or {}).get("items", []):
        repo = (hit.get("repository") or {}).get("full_name", "")
        if not repo or repo in seen:
            continue
        seen.add(repo)
        url = (hit.get("repository") or {}).get("html_url", "")
        row = item(source, tier, category, f"[discovered] {repo} — {hit.get('path', '')}", url, ts)
        if row:
            out.append(row)
    return out


# ---------------------------------------------------------------- fetch

def fetch(session: requests.Session, name, url, tier, category, kind, ts: str) -> list[dict]:
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
    if kind == "gh_code_search":
        return parse_gh_code_search(resp.json(), name, tier, category, ts)
    raise ValueError(f"unknown kind {kind}")


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


def merge(old: list[dict], new: list[dict], ts: str, now: datetime) -> list[dict]:
    """Keep first-seen timestamp, drop items older than KEEP_DAYS, newest first."""
    by_url = {r["url"]: r for r in old if r.get("url")}
    for r in new:
        prev = by_url.get(r["url"])
        if prev:
            r["timestamp"] = prev.get("timestamp") or ts
            if r["source"] == "GitHub TMDL samples":
                r["pub_date"] = prev.get("pub_date") or r["pub_date"]  # keep discovery date
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
    return keep


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
    write_csv(LATEST, LATEST_COLS, merge(read_csv(LATEST), new_rows, ts, now))
    write_csv(HEALTH, HEALTH_COLS, health)
    logging.info("%d/%d sources ok", ok, len(feeds))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
