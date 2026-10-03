#!/usr/bin/env python3
"""Fetch publicly available market prices and append changes to data/prices.csv.

Sources (all public, no credentials). Where several providers are listed, they
are tried in order and the first one that works is used; `source` names it.
  - SJC gold          : sjc.com.vn (blocks non-Vietnam IPs), vnappmob.com
  - Brand gold        : PNJ, DOJI, BTMC, BTMH, Phu Quy via vnappmob.com ->
                        giavang.org -> the brand's own site (where one exists)
  - Vietcombank       : VND exchange rates
  - SBV central rate  : USD/VND central rate (sbv.gov.vn, then mirrors)
  - Metals spot       : XAU/XAG via gold-api.com -> goldprice.org -> stooq.com
  - PNJ stock (HOSE)  : session OHLC + volume via TCBS -> VNDirect -> CafeF;
                        foreign buy/sell via VNDirect -> CafeF

Output files (UTF-8, no BOM, comma-separated, "\n" line endings):
  - data/prices.csv : price history. A row is appended only when a price
                      differs from the last recorded row for the same key;
                      otherwise only that row's last_checked is refreshed.
  - data/latest.csv : the latest row per key (same schema).
  - data/health.csv : one row per source: status, last_success, last_error,
                      error_msg. status=blocked_non_vn when every provider
                      refused the request (HTTP 403/451).

No number is ever invented: a source that fails writes no price rows, and its
existing rows simply stop getting a fresh last_checked.

timestamp    = first time this specific price was observed.
last_checked = most recent successful check of the source that returned it.

Exit codes: 0 if at least one source succeeded, 1 if every source failed.
"""

from __future__ import annotations

import csv
import functools
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

COLUMNS = [
    "timestamp",
    "source",
    "category",
    "price_type",
    "item",
    "buy",
    "sell",
    "currency",
    "unit",
    "status",
    "last_checked",  # appended last: positional readers of the first 10 columns are unaffected
]
# Header written before last_checked existed; such files are migrated on read.
LEGACY_COLUMNS = COLUMNS[:-1]
KEY_COLUMNS = ("source", "category", "price_type", "item")
VALUE_COLUMNS = ("buy", "sell", "currency", "unit", "status")

# Vietnam has no DST, so a fixed offset is exact and needs no tzdata.
VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PRICE_FEED_DATA_DIR", ROOT / "data"))
PRICES_CSV = DATA_DIR / "prices.csv"
LATEST_CSV = DATA_DIR / "latest.csv"
HEALTH_CSV = DATA_DIR / "health.csv"
HEALTH_COLUMNS = ["source", "status", "last_success", "last_error", "error_msg"]

TIMEOUT = (10, 25)  # connect, read (seconds)
RETRIES = 3
HEADERS = {
    "User-Agent": "pnj-price-feed/1.0 (+https://github.com/truongnhat/pnj-price-feed)",
    "Accept": "application/json, text/xml, */*",
}
# Some public sites reject non-browser clients (HTTP 403). These are ordinary
# request headers, not credentials.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

SJC_URL = "https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"
VCB_JSON_URL = "https://www.vietcombank.com.vn/api/exchangerates"
VCB_XML_URL = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx"
SJC_HOME_URL = "https://sjc.com.vn/"
GOLD_API_URL = "https://api.gold-api.com/price/{symbol}"
GOLDPRICE_ORG_URL = "https://data-asg.goldprice.org/dbXRates/USD"
STOOQ_URL = "https://stooq.com/q/l/"
METAL_SYMBOLS = ("XAU", "XAG")
DOJI_URLS = ("https://update.giavang.doji.vn/banggia/doji_92411/92411",
             "http://update.giavang.doji.vn/banggia/doji_92411/92411")
VNAPPMOB_KEY_URL = "https://api.vnappmob.com/api/request_api_key"
VNAPPMOB_GOLD_URL = "https://api.vnappmob.com/api/v2/gold/{brand}"

# Plausible domestic gold price range in VND per luong (37.5 g); covers 10K-24K gold.
GOLD_LUONG_MIN, GOLD_LUONG_MAX = 30_000_000, 1_000_000_000

GIAVANG_ORG_URL = "https://giavang.org/trong-nuoc/{slug}/"
PNJ_API_URL = "https://edge-api.pnj.io/ecom-frontend/v1/get-gold-price"
PNJ_HTML_URL = "https://giavang.pnj.com.vn/"

STOCK = "PNJ"
VCI_CHART_URL = "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart"
VCI_BOARD_URL = "https://trading.vietcap.com.vn/api/price/symbols/getList"
VCI_HEADERS = {"Referer": "https://trading.vietcap.com.vn/", "Origin": "https://trading.vietcap.com.vn",
               "Content-Type": "application/json"}
TCBS_BARS_URL = "https://apipubaws.tcbs.com.vn/stock-insight/v1/stock/bars-long-term"
VND_STOCK_URL = "https://finfo-api.vndirect.com.vn/v4/stock_prices"
VND_FOREIGN_URL = "https://finfo-api.vndirect.com.vn/v4/foreigns"
CAFEF_PRICE_URL = "https://s.cafef.vn/Ajax/PageNew/DataHistory/PriceHistory.ashx"
CAFEF_FOREIGN_URL = "https://s.cafef.vn/Ajax/PageNew/DataHistory/GDKhoiNgoai.ashx"
STOCK_PRICE_MIN, STOCK_PRICE_MAX = 1_000, 10_000_000  # VND per share

SBV_CENTRAL_URLS = (  # the SBV home page shows "Tỷ giá trung tâm USD/VND"
    ("sbv.gov.vn", "https://sbv.gov.vn/vi/trang-chu"),
    ("sbv.gov.vn", "https://www.sbv.gov.vn/en/trang-chu"),
    ("tygiausd.org", "https://tygiausd.org/"),
)
CENTRAL_RATE_MIN, CENTRAL_RATE_MAX = 15_000, 50_000  # VND per USD

log = logging.getLogger("fetch_prices")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_vn() -> str:
    """Current time in Vietnam as ISO-8601, e.g. 2026-10-03T10:17:05+07:00."""
    return datetime.now(VN_TZ).replace(microsecond=0).isoformat()


def to_number(value) -> Decimal | None:
    """Parse '25,030.00', '119000000', 2650.1, '-' into a Decimal (None if empty/invalid/<=0)."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if text in ("", "-", "null", "None"):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number > 0 else None


def fmt(number: Decimal | None) -> str:
    """Format a number as a plain decimal string (no thousands separator, no exponent)."""
    if number is None:
        return ""
    return format(number.normalize(), "f")


def make_row(ts, source, category, price_type, item, buy, sell, currency, unit) -> dict | None:
    if buy is None and sell is None:
        return None
    return {
        "timestamp": ts,
        "source": source,
        "category": category,
        "price_type": price_type,
        "item": " ".join(str(item).split()),  # collapse whitespace/newlines
        "buy": fmt(buy),
        "sell": fmt(sell),
        "currency": currency,
        "unit": unit,
        "status": "ok" if buy is not None and sell is not None else "partial",
        "last_checked": ts,
    }


class BlockedError(RuntimeError):
    """The host refused the request (HTTP 403/451): typically it blocks non-Vietnam IPs."""


# Hosts that did not accept a connection earlier in this run (connect timeout):
# later calls fail at once instead of waiting again.
_dead_hosts: set = set()


def http(method: str, url: str, session: requests.Session | None = None,
         headers: dict | None = None, **kwargs) -> requests.Response:
    host = urlparse(url).hostname
    if host in _dead_hosts:
        raise RuntimeError(f"{method} {url}: skipped, {host} timed out earlier in this run")
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = (session or requests).request(method, url, headers=headers or HEADERS,
                                                 timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.ConnectTimeout as exc:
            # Typically a firewall silently dropping this IP: retrying only waits longer.
            _dead_hosts.add(host)
            raise RuntimeError(f"{method} {url}: connect timeout (host may drop "
                               "non-Vietnam traffic)") from exc
        except requests.RequestException as exc:
            last_error = exc
            log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt, RETRIES, exc)
            status = getattr(exc.response, "status_code", None)
            if status in (403, 451):
                raise BlockedError(f"{method} {url}: HTTP {status} "
                                   "(host refuses this IP, likely non-Vietnam)") from exc
            if (status and 400 <= status < 500 and status != 429) or \
                    isinstance(exc, requests.exceptions.SSLError):
                break  # other 4xx and bad certificates do not fix themselves
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{method} {url} failed: {last_error}")


def snippet(resp: requests.Response) -> str:
    """Start of a response body, for error messages when parsing finds nothing."""
    return repr(resp.text[:300])


def first_working(label: str, providers: list[tuple[str, Callable[[], list[dict]]]]) -> list[dict]:
    """Return the rows of the first provider that yields any.

    Raises BlockedError when every provider refused us (403/451), so health.csv
    can say blocked_non_vn; otherwise RuntimeError listing each provider's error.
    """
    errors, blocked = [], 0
    for name, provider in providers:
        try:
            rows = provider()
            if rows:
                log.info("%s: using %s (%d rows)", label, name, len(rows))
                return rows
            errors.append(f"{name}: no rows")
        except BlockedError as exc:
            blocked += 1
            errors.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - try the next provider
            errors.append(f"{name}: {exc}")
        log.warning("%s: provider %s failed: %s", label, name, errors[-1])
    message = " | ".join(errors) or "no providers"
    if providers and blocked == len(providers):
        raise BlockedError(message)
    raise RuntimeError(message)


# --------------------------------------------------------------------------- #
# Number / HTML / JSON helpers for Vietnamese sources
# --------------------------------------------------------------------------- #
NUMBER_TOKEN = re.compile(r"\d+(?:[.,]\d+)*")


def parse_number(value, allow_zero: bool = False) -> Decimal | None:
    """Parse Vietnamese-formatted numbers: '14.200' and '14,200' -> 14200, '98.5' -> 98.5.

    A separator followed only by 3-digit groups is a thousands separator; with
    both '.' and ',' present the last one is the decimal point.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
    else:
        match = NUMBER_TOKEN.search(str(value).replace("\xa0", " "))
        if not match:
            return None
        token = match.group(0)
        if "." in token and "," in token:
            point = max(token.rfind("."), token.rfind(","))
            token = re.sub(r"[.,]", "", token[:point]) + "." + token[point + 1:]
        elif "." in token or "," in token:
            sep = "." if "." in token else ","
            parts = token.split(sep)
            if all(len(p) == 3 for p in parts[1:]):
                token = "".join(parts)
            elif len(parts) == 2:
                token = ".".join(parts)
            else:
                return None
        number = Decimal(token)
    if number > 0 or (allow_zero and number == 0):
        return number
    return None


def is_number_cell(text: str) -> bool:
    """A table cell holding a price: starts with a digit (after signs/arrows)."""
    return bool(re.match(r"^[\s+\-\u2212\u25b2\u25bc]*\d", text or ""))


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._row, self._cell, self._skip = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
            self.tables[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            span = lambda k: int(a.get(k) or 1) if str(a.get(k) or 1).isdigit() else 1  # noqa: E731
            self._cell = {"text": [], "rowspan": span("rowspan"), "colspan": span("colspan")}
            self._row.append(self._cell)
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell["text"].append(data)


def html_tables(markup: str) -> list[list[list[str]]]:
    """Every <table> as a grid of cell texts, with rowspan/colspan expanded."""
    parser = _TableParser()
    parser.feed(markup)
    grids = []
    for table in parser.tables:
        grid, pending = [], {}  # pending: column -> (text, rows left)
        for raw in table:
            row, col, cells = [], 0, list(raw)
            while cells or col in pending:
                if col in pending:
                    text, left = pending[col]
                    row.append(text)
                    pending[col] = (text, left - 1)
                    if left - 1 == 0:
                        del pending[col]
                    col += 1
                    continue
                cell = cells.pop(0)
                text = " ".join("".join(cell["text"]).split())
                for _ in range(cell["colspan"]):
                    if cell["rowspan"] > 1:
                        pending[col] = (text, cell["rowspan"] - 1)
                    row.append(text)
                    col += 1
            if any(row):
                grid.append(row)
        grids.append(grid)
    return grids


def html_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


JSON_LABEL_KEYS = ("tensp", "ten", "name", "typename", "loai", "loaivang", "type", "title",
                   "product", "gold_type", "masp")
JSON_BUY_KEYS = ("giamua", "gia_mua", "buy", "mua", "buyprice", "buy_price", "purchase")
JSON_SELL_KEYS = ("giaban", "gia_ban", "sell", "ban", "sellprice", "sell_price")


def json_price_pairs(obj, context: tuple = ()) -> list[tuple[str, object, object]]:
    """Find {name, buy, sell}-shaped records anywhere in a JSON document.

    Names of enclosing records without prices (e.g. a region) are prefixed.
    """
    pairs = []
    if isinstance(obj, list):
        for value in obj:
            pairs += json_price_pairs(value, context)
    elif isinstance(obj, dict):
        lower = {str(k).lower(): k for k in obj}
        pick = lambda keys: next((lower[k] for k in keys if k in lower), None)  # noqa: E731
        label_key, buy_key, sell_key = pick(JSON_LABEL_KEYS), pick(JSON_BUY_KEYS), pick(JSON_SELL_KEYS)
        label = obj.get(label_key) if label_key else None
        label = label if isinstance(label, str) and label.strip() else None
        if label and (buy_key or sell_key):
            pairs.append((" ".join(context + (label,)),
                          obj.get(buy_key) if buy_key else None,
                          obj.get(sell_key) if sell_key else None))
        child_context = context + (label,) if label and not (buy_key or sell_key) else context
        for value in obj.values():
            if isinstance(value, (list, dict)):
                pairs += json_price_pairs(value, child_context)
    return pairs


# --------------------------------------------------------------------------- #
# Parsers (pure functions, unit-tested with fixtures)
# --------------------------------------------------------------------------- #
def parse_sjc(payload: dict, ts: str) -> list[dict]:
    """SJC returns prices in VND per luong (tael). Values may be given in VND or thousand VND."""
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise ValueError("SJC response indicates failure")
    rows = []
    for rec in payload.get("data") or []:
        name = rec.get("TypeName")
        if not name:
            continue
        branch = rec.get("BranchName")
        item = f"{name} ({branch})" if branch else name
        prices = []
        for value_key, text_key in (("BuyValue", "Buy"), ("SellValue", "Sell")):
            number = to_number(rec.get(value_key)) or to_number(rec.get(text_key))
            if number is not None and number < 1_000_000:  # quoted in thousand VND
                number *= 1000
            if number is not None and not (1_000_000 <= number <= 10_000_000_000):
                log.warning("SJC: implausible price %s for %s, skipped", number, item)
                number = None
            prices.append(number)
        row = make_row(ts, "SJC", "gold", "retail", item, prices[0], prices[1], "VND", "luong")
        if row:
            rows.append(row)
    return rows


def gold_multiplier(values: list[Decimal]) -> int:
    """Find the factor that turns a feed's numbers into VND per luong.

    Feeds quote in VND or thousand VND, per luong or per chi (1/10 luong). The
    largest price in a gold feed is 24K gold, so pick the factor that puts it
    in the plausible range and apply the same factor to every row.
    """
    top = max(values, default=None)
    for factor in (1, 10, 1000, 10000):
        if top is not None and GOLD_LUONG_MIN <= top * factor <= GOLD_LUONG_MAX:
            return factor
    raise ValueError(f"cannot infer price unit (largest value {top})")


def gold_rows(ts: str, source: str, pairs: list[tuple[str, object, object]]) -> list[dict]:
    """Turn (item, buy, sell) pairs from one gold feed into rows in VND per luong."""
    parsed = [(item, parse_number(b), parse_number(s)) for item, b, s in pairs if item]
    factor = gold_multiplier([v for _, b, s in parsed for v in (b, s) if v is not None])
    rows, seen = [], set()
    for item, buy, sell in parsed:
        buy, sell = (v * factor if v is not None else None for v in (buy, sell))
        buy, sell = (v.quantize(Decimal(1))
                     if v is not None and GOLD_LUONG_MIN <= v <= GOLD_LUONG_MAX else None
                     for v in (buy, sell))  # whole VND
        row = make_row(ts, source, "gold", "retail", item, buy, sell, "VND", "luong")
        if row and row["item"] not in seen:
            seen.add(row["item"])
            rows.append(row)
    return rows


def parse_doji_xml(text: str | bytes, ts: str) -> list[dict]:
    """DOJI XML: <Row Name="..." Key="..." Buy="..." Sell="..."/> elements."""
    root = ET.fromstring(text)
    pairs = [(el.get("Name"), el.get("Buy"), el.get("Sell"))
             for el in root.iter() if el.get("Name") and (el.get("Buy") or el.get("Sell"))]
    return gold_rows(ts, "DOJI", pairs)


# Field codes seen in vnappmob responses -> readable item names. Unknown codes
# fall back to "<BRAND> <code>" so new products still appear.
VNAPPMOB_LABELS = {
    "sjc": {
        "1l": "SJC 1L, 10L, 1KG",
        "1c": "SJC 1 chỉ",
        "5c": "SJC 5 chỉ",
        "nhan1c": "SJC nhẫn 99,99% 1-5 chỉ",
        "nutrang_9999": "SJC nữ trang 99,99%",
        "nutrang_99": "SJC nữ trang 99%",
        "nutrang_75": "SJC nữ trang 75%",
    },
    "doji": {
        "hn": "DOJI Hà Nội",
        "hcm": "DOJI TP.HCM",
        "dn": "DOJI Đà Nẵng",
        "ct": "DOJI Cần Thơ",
    },
}

# Rows written before a check existed and known to be wrong; dropped on read.
# (source, item, timestamp)
DATA_CORRECTIONS = {
    # value / volume = ~7,100 VND per share vs a ~70,000+ VND share price:
    # CafeF's value unit was misread. Values are now checked against the close.
    ("cafef.vn", "PNJ NN GT mua/bán", "2026-10-03T15:20:06+07:00"),
    # CafeF foreign volumes for the 2026-10-02 session (22,400 / 67,800) do not
    # match the exchange (foreign buy 4,200,700 shares, confirmed on a broker app).
    ("cafef.vn", "PNJ NN KL mua/bán", "2026-10-03T15:20:06+07:00"),
    ("cafef.vn", "PNJ NN ngày phiên", "2026-10-03T15:20:06+07:00"),
}

# One-time renames of item names already written to the CSV, applied on read
# so history and new rows share a key.
ITEM_RENAMES = {
    ("vnappmob.com", "SJC 1 chỉ, 2 chỉ, 5 chỉ"): "SJC 1 chỉ",
    ("vnappmob.com", "SJC 5c"): "SJC 5 chỉ",
    ("vnappmob.com", "SJC nutrang_9999"): "SJC nữ trang 99,99%",
    ("vnappmob.com", "SJC nutrang_99"): "SJC nữ trang 99%",
    ("vnappmob.com", "SJC nutrang_75"): "SJC nữ trang 75%",
}


def parse_vnappmob(payload: dict, brand: str, ts: str) -> list[dict]:
    """{"results": [{"buy_1l": ..., "sell_1l": ..., "buy_nhan1c": ..., "datetime": ...}]}

    Falls back to {name, buy, sell}-shaped records if there are no buy_/sell_ fields.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, dict):
        results = [results]
    rec = results[0] if isinstance(results, list) and results else None
    if not isinstance(rec, dict):
        raise ValueError(f"vnappmob {brand}: no results")
    labels = VNAPPMOB_LABELS.get(brand, {})
    codes = sorted({k[4:] for k in rec if k.startswith("buy_")} |
                   {k[5:] for k in rec if k.startswith("sell_")})
    pairs = [(labels.get(c, f"{brand.upper()} {c}"), rec.get(f"buy_{c}"), rec.get(f"sell_{c}"))
             for c in codes] or json_price_pairs(results)
    return gold_rows(ts, "vnappmob.com", pairs)


# Brand feeds also list jewelry, silver, gifts... keep rings and bars (incl. the
# brands' own bullion lines), plus raw-material / market / other-brand lines.
# Case-insensitive substrings; GOLD_DROP wins.
GOLD_KEEP = ("nhẫn", "nhan ", "miếng", "mieng", "sjc", "nguyên liệu", "nguyen lieu",
             "thị trường", "thi truong", "thương hiệu khác", "thuong hieu khac", "1 lượng",
             "1 luong", "kim bảo", "kim gia bảo", "phúc lộc tài", "thần tài")
GOLD_DROP = ("bạc", "silver", "trang sức", "trang suc", "quà", "gift", "đồng vàng")
GOLD_FILTERED_SOURCES = ("giavang.org", "pnj.com.vn")


def keep_gold_label(label: str) -> bool:
    text = f" {label.lower()} "
    return any(k in text for k in GOLD_KEEP) and not any(k in text for k in GOLD_DROP)


def with_brand(prefix: str, label: str) -> str:
    """Item names start with the brand: 'Nhẫn trơn 999.9' -> 'PNJ Nhẫn trơn 999.9'."""
    label = " ".join(label.split())
    return label if label.lower().startswith(prefix.lower()) else f"{prefix} {label}"


def parse_gold_html(markup: str, source: str, ts: str) -> list[dict]:
    """Gold price tables (giavang.org, brand sites): label cells + Mua/Bán columns.

    Each table is converted on its own, since tables may use different units.
    """
    rows, seen = [], set()
    for grid in html_tables(markup):
        buy_col = sell_col = None
        pairs = []
        for cells in grid:
            lower = [c.lower() for c in cells]
            if buy_col is None and any("mua" in c for c in lower) and any("bán" in c for c in lower):
                buy_col = next(i for i, c in enumerate(lower) if "mua" in c)
                sell_col = next(i for i, c in enumerate(lower) if "bán" in c)
                continue
            numeric = [i for i, c in enumerate(cells) if is_number_cell(c)]
            if buy_col is not None and max(buy_col, sell_col) < len(cells):
                b_i, s_i = buy_col, sell_col
            elif len(numeric) >= 2:
                b_i, s_i = numeric[0], numeric[1]
            else:
                continue
            label = " ".join(dict.fromkeys(c for i, c in enumerate(cells)
                                           if c and not is_number_cell(c) and i not in (b_i, s_i)))
            if label and keep_gold_label(label):
                pairs.append((label, cells[b_i], cells[s_i]))
        if not pairs:
            continue
        try:
            table_rows = gold_rows(ts, source, pairs)
        except ValueError:
            continue  # not a VND gold table (e.g. world price in USD)
        for row in table_rows:
            if row["item"] not in seen:
                seen.add(row["item"])
                rows.append(row)
    return rows


def parse_gold_json(payload, source: str, ts: str) -> list[dict]:
    pairs = [p for p in json_price_pairs(payload) if keep_gold_label(p[0])]
    return gold_rows(ts, source, pairs) if pairs else []


def _vcb_rows(ts: str, code: str, cash, transfer, sell) -> list[dict]:
    code = (code or "").strip().upper()
    if not code:
        return []
    unit = f"1 {code}"
    out = []
    for price_type, buy in (("cash", cash), ("transfer", transfer)):
        row = make_row(ts, "Vietcombank", "fx", price_type, code,
                       to_number(buy), to_number(sell), "VND", unit)
        if row:
            out.append(row)
    return out


def parse_vcb_json(payload: dict, ts: str) -> list[dict]:
    rows = []
    for rec in payload.get("Data") or []:
        rows += _vcb_rows(ts, rec.get("currencyCode"), rec.get("cash"),
                          rec.get("transfer"), rec.get("sell"))
    return rows


def parse_vcb_xml(text: str | bytes, ts: str) -> list[dict]:
    root = ET.fromstring(text)
    rows = []
    for rec in root.iter("Exrate"):
        rows += _vcb_rows(ts, rec.get("CurrencyCode"), rec.get("Buy"),
                          rec.get("Transfer"), rec.get("Sell"))
    return rows


def spot_row(source: str, symbol: str, value, ts: str) -> dict:
    price = to_number(value)
    if price is None:
        raise ValueError(f"{source}: no price for {symbol}")
    price = price.quantize(Decimal("0.01"))
    category = {"XAU": "gold", "XAG": "silver"}.get(symbol, "metal")
    return make_row(ts, source, category, "spot", symbol, price, price, "USD", "troy_oz")


def parse_gold_api(payload: dict, symbol: str, ts: str) -> list[dict]:
    return [spot_row("gold-api.com", symbol, payload.get("price"), ts)]


def parse_goldprice_org(payload: dict, ts: str) -> list[dict]:
    """{"items": [{"curr": "USD", "xauPrice": 2650.1, "xagPrice": 31.2, ...}]}"""
    items = [i for i in payload.get("items") or [] if i.get("curr") == "USD"]
    if not items:
        raise ValueError("goldprice.org: no USD item")
    return [spot_row("goldprice.org", sym, items[0].get(f"{sym.lower()}Price"), ts)
            for sym in METAL_SYMBOLS]


def parse_stooq(text: str, symbol: str, ts: str) -> list[dict]:
    """CSV: Symbol,Date,Time,Open,High,Low,Close (Close is 'N/D' when unavailable)."""
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise ValueError(f"stooq.com: empty response for {symbol}")
    return [spot_row("stooq.com", symbol, rows[0].get("Close"), ts)]


# --------------------------------------------------------------------------- #
# PNJ stock (HOSE)
# --------------------------------------------------------------------------- #
# A session is normalized to {"date": "YYYY-MM-DD", "open", "high", "low",
# "close", "volume"}; foreign trading to {"date", "buy_vol", "sell_vol",
# "buy_val", "sell_val"}. Values are as reported; units are fixed in *_rows().
def _iso_date(text) -> str:
    text = str(text or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text) or re.match(r"(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        raise ValueError(f"unrecognized date {text!r}")
    y, mo, d = (m.group(1), m.group(2), m.group(3)) if len(m.group(1)) == 4 else \
        (m.group(3), m.group(2), m.group(1))
    return f"{y}-{mo}-{d}"


def _records(payload, *path) -> list[dict]:
    original = payload
    for key in path:
        payload = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError(f"no records at {'/'.join(path) or 'root'}, body "
                         f"{json.dumps(original, ensure_ascii=False)[:300]!r}")
    return payload


def _epoch_date(value) -> str:
    """Epoch seconds or milliseconds (number or string) -> Vietnam date YYYY-MM-DD."""
    number = float(value)
    if number > 1e11:
        number /= 1000
    return datetime.fromtimestamp(number, VN_TZ).strftime("%Y-%m-%d")


def parse_ohlc_arrays(payload) -> dict:
    """Chart APIs (Vietcap, TradingView style): {"t": [...], "o": [...], "h", "l", "c", "v"},
    possibly inside a list (one entry per symbol) or under "data". Uses the last bar."""
    def find(node):
        if isinstance(node, dict):
            if all(isinstance(node.get(k), list) and node.get(k) for k in "tohlc"):
                return node
            node = node.get("data")
        if isinstance(node, list):
            for item in node:
                found = find(item)
                if found:
                    return found
        return None
    bars = find(payload)
    if not bars:
        raise ValueError(f"no t/o/h/l/c arrays, body {json.dumps(payload, ensure_ascii=False)[:300]!r}")
    i = len(bars["t"]) - 1
    volumes = bars.get("v") or []
    return {"date": _epoch_date(bars["t"][i]), "open": bars["o"][i], "high": bars["h"][i],
            "low": bars["l"][i], "close": bars["c"][i],
            "volume": volumes[i] if i < len(volumes) else None}


def parse_vci_board_foreign(payload) -> dict:
    """Vietcap price board: [{"listingInfo": {...}, "matchPrice": {...}, ...}].

    Field names are matched by meaning (foreign + buy/sell + volume/value) so a
    renamed field fails loudly instead of being guessed.
    """
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, dict):
        raise ValueError(f"no board entry, body {json.dumps(payload, ensure_ascii=False)[:300]!r}")
    flat = {}
    for part in item.values():
        if isinstance(part, dict):
            flat.update({k: v for k, v in part.items() if not isinstance(v, (dict, list))})
    def pick(side, kind):
        for key, value in flat.items():
            k = key.lower()
            if "foreign" in k and side in k and any(w in k for w in kind) and \
                    not any(w in k for w in ("room", "total", "percent", "net")):
                return value
        return None
    found = {"buy_vol": pick("buy", ("vol", "qtty", "quantity")),
             "sell_vol": pick("sell", ("vol", "qtty", "quantity")),
             "buy_val": pick("buy", ("val",)), "sell_val": pick("sell", ("val",))}
    if found["buy_vol"] is None and found["sell_vol"] is None:
        raise ValueError(f"no foreign buy/sell fields; fields: {sorted(flat)[:60]}")
    date = next((v for k, v in flat.items() if "tradingdate" in k.lower() and v), None)
    found["date"] = _iso_date(date) if date else None
    return found


def parse_tcbs_bars(payload: dict) -> dict:
    """{"ticker": "PNJ", "data": [{"open", "high", "low", "close", "volume", "tradingDate"}]}"""
    rec = max(_records(payload, "data"), key=lambda r: str(r.get("tradingDate")))
    return {"date": _iso_date(rec.get("tradingDate")), "open": rec.get("open"),
            "high": rec.get("high"), "low": rec.get("low"), "close": rec.get("close"),
            "volume": rec.get("volume")}


def parse_vnd_stock(payload: dict) -> dict:
    """{"data": [{"code", "date", "open", "high", "low", "close", "nmVolume", ...}]} (newest first)"""
    rec = _records(payload, "data")[0]
    return {"date": _iso_date(rec.get("date")), "open": rec.get("open"), "high": rec.get("high"),
            "low": rec.get("low"), "close": rec.get("close"), "volume": rec.get("nmVolume")}


def parse_cafef_price(payload: dict) -> dict:
    """{"Data": {"Data": [{"Ngay": "dd/mm/yyyy", "GiaMoCua", "GiaCaoNhat", "GiaThapNhat",
    "GiaDongCua", "KhoiLuongKhopLenh", ...}]}} (newest first)"""
    rec = _records(payload, "Data", "Data")[0]
    return {"date": _iso_date(rec.get("Ngay")), "open": rec.get("GiaMoCua"),
            "high": rec.get("GiaCaoNhat"), "low": rec.get("GiaThapNhat"),
            "close": rec.get("GiaDongCua"), "volume": rec.get("KhoiLuongKhopLenh")}


def parse_vnd_foreign(payload: dict) -> dict:
    """{"data": [{"code", "tradingDate", "buyVol", "sellVol", "buyVal", "sellVal", ...}]}"""
    rec = _records(payload, "data")[0]
    return {"date": _iso_date(rec.get("tradingDate")), "buy_vol": rec.get("buyVol"),
            "sell_vol": rec.get("sellVol"), "buy_val": rec.get("buyVal"),
            "sell_val": rec.get("sellVal")}


def parse_cafef_foreign(payload: dict) -> dict:
    """{"Data": {"Data": [{"Ngay", "KLMua", "GtMua", "KLBan", "GtBan", ...}]}} (newest first)"""
    rec = _records(payload, "Data", "Data")[0]
    return {"date": _iso_date(rec.get("Ngay")), "buy_vol": rec.get("KLMua"),
            "sell_vol": rec.get("KLBan"), "buy_val": rec.get("GtMua"), "sell_val": rec.get("GtBan")}


def _session_date_row(ts, source, item, date: str) -> dict:
    # The schema has no date column: the session date is a row of its own,
    # encoded as a yyyymmdd number in buy and sell.
    value = Decimal(date.replace("-", ""))
    return make_row(ts, source, "stock", "session_date", item, value, value, "", "yyyymmdd")


def stock_session_rows(session: dict, source: str, ts: str) -> list[dict]:
    """OHLC in VND per share (thousand-VND quotes are scaled) plus matched volume."""
    rows = [_session_date_row(ts, source, f"{STOCK} ngày phiên", session["date"])]
    for key, label in (("open", "mở cửa"), ("high", "cao nhất"), ("low", "thấp nhất"),
                       ("close", "đóng cửa")):
        price = parse_number(session.get(key))
        if price is not None and price < STOCK_PRICE_MIN:
            price *= 1000  # quoted in thousand VND
        if price is None or not STOCK_PRICE_MIN <= price <= STOCK_PRICE_MAX:
            raise ValueError(f"{source}: implausible {key} {session.get(key)!r}")
        price = price.quantize(Decimal(1))
        rows.append(make_row(ts, source, "stock", "session", f"{STOCK} {label}",
                             price, price, "VND", "1 cp"))
    volume = parse_number(session.get("volume"), allow_zero=True)
    if volume is not None:
        volume = volume.quantize(Decimal(1))
        rows.append(make_row(ts, source, "stock", "session", f"{STOCK} KL khớp lệnh",
                             volume, volume, "", "cp"))
    return rows


def foreign_value_factor(vols, vals, close: Decimal | None) -> int | None:
    """Unit of the reported values (1, 1e3, 1e6 or 1e9 VND), checked against the close.

    value / volume is what foreigners paid per share, so after scaling it must
    be within 20% of the session close. No close, or no unit fits: None, and
    the value row is left out rather than guessed.
    """
    ratios = [val / vol for vol, val in zip(vols, vals) if vol and val]
    if close is None or not ratios:
        return None
    for factor in (1, 10 ** 3, 10 ** 6, 10 ** 9):
        if all(Decimal("0.8") * close <= r * factor <= Decimal("1.2") * close for r in ratios):
            return factor
    return None


def stock_foreign_rows(foreign: dict, source: str, ts: str, close: Decimal | None = None) -> list[dict]:
    """Foreign investors: buy/sell volume (shares) and, when it checks out, value (VND)."""
    vols = [parse_number(foreign.get(k), allow_zero=True) for k in ("buy_vol", "sell_vol")]
    vals = [parse_number(foreign.get(k), allow_zero=True) for k in ("buy_val", "sell_val")]
    if all(v is None for v in vols):
        raise ValueError(f"{source}: no foreign volumes")
    rows = []
    if foreign.get("date"):
        rows.append(_session_date_row(ts, source, f"{STOCK} NN ngày phiên", foreign["date"]))
    rows.append(make_row(ts, source, "stock", "foreign", f"{STOCK} NN KL mua/bán",
                         *[v.quantize(Decimal(1)) if v is not None else None for v in vols], "", "cp"))
    factor = foreign_value_factor(vols, vals, close)
    if factor is None:
        if any(vals):
            log.warning("%s: foreign values %s not consistent with close %s for volumes %s; "
                        "value row skipped", source, [foreign.get("buy_val"), foreign.get("sell_val")],
                        close, [foreign.get("buy_vol"), foreign.get("sell_vol")])
        return rows
    value_row = make_row(ts, source, "stock", "foreign", f"{STOCK} NN GT mua/bán",
                         *[(v * factor).quantize(Decimal(1)) if v is not None else None for v in vals],
                         "VND", "VND")
    return rows + ([value_row] if value_row else [])


# --------------------------------------------------------------------------- #
# SBV central rate
# --------------------------------------------------------------------------- #
_NUM = r"(\d{1,3}(?:[.,]\d{3})+|\d{5})"
# Between the label and the number: no digits except a date or time ("03/10/2026", "08:30").
_GAP = r"((?:(?!trần|sàn|ceiling|floor)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}:\d{2}|[^0-9])){0,160})"
CENTRAL_PATTERNS = (
    re.compile(r"1\s*(?:USD|Đô\s*la\s*Mỹ)\s*=\s*" + _NUM + r"\s*(?:VND|VNĐ|đồng)", re.I),
    # "... tỷ giá trung tâm ... 25.123", with no ceiling/floor ("trần"/"sàn") in between
    re.compile(r"trung\s*tâm" + _GAP + _NUM, re.I),
    re.compile(r"central\s*(?:exchange\s*)?rate" + _GAP + _NUM, re.I),
)


def central_rate_context(markup: str) -> str:
    """Text around each 'trung tâm' / 'central rate' mention, for error messages."""
    text = html_text(markup)
    spots = [m.start() for m in re.finditer(r"trung\s*tâm|central\s*(?:exchange\s*)?rate", text, re.I)]
    return " || ".join(text[max(0, i - 40):i + 160] for i in spots[:3]) or "label not in page text"


def central_rate_from_tables(markup: str) -> Decimal | None:
    """A table with a 'trung tâm' / 'central' column and a USD row."""
    for grid in html_tables(markup):
        col = None
        for cells in grid:
            lower = [c.lower() for c in cells]
            if col is None:
                col = next((i for i, c in enumerate(lower)
                            if ("trung tâm" in c or "central" in c) and "trần" not in c), None)
                continue
            if col < len(cells) and any(re.search(r"\busd\b|đô la mỹ", c) for c in lower):
                rate = parse_number(cells[col])
                if rate is not None and CENTRAL_RATE_MIN <= rate <= CENTRAL_RATE_MAX:
                    return rate
    return None


def central_rate_links(markup: str, base_url: str) -> list[str]:
    """Links whose text mentions the central rate (the SBV menu entry)."""
    links = []
    for href, label in re.findall(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', markup):
        if re.search(r"trung\s*tâm|central\s*(?:exchange\s*)?rate", html_text(label), re.I):
            url = requests.compat.urljoin(base_url, html.unescape(href))
            if url.startswith("http") and url not in links:
                links.append(url)
    return links[:3]


def parse_central_rate(markup: str, source: str, ts: str) -> list[dict]:
    table_rate = central_rate_from_tables(markup)
    if table_rate is not None:
        return [make_row(ts, source, "fx", "central", "USD", table_rate, table_rate, "VND", "1 USD")]
    text = html_text(markup)
    for pattern in CENTRAL_PATTERNS:
        for match in pattern.finditer(text):
            # Other currencies (EUR ~ 27-30k) also fall in the range: require
            # USD to be named in the match or just after the number.
            if not re.search(r"USD|đô\s*la\s*mỹ", text[match.start():match.end() + 40], re.I):
                continue
            rate = parse_number(match.groups()[-1])
            if rate is not None and CENTRAL_RATE_MIN <= rate <= CENTRAL_RATE_MAX:
                return [make_row(ts, source, "fx", "central", "USD", rate, rate, "VND", "1 USD")]
    return []


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def fetch_sjc(ts: str) -> list[dict]:
    # SJC answers 403 to bare API calls: open the home page first (cookies),
    # then call the price service the way the site's own page does.
    with requests.Session() as session:
        try:
            session.get(SJC_HOME_URL, headers={**BROWSER_HEADERS, "Accept": "text/html"},
                        timeout=TIMEOUT)
        except requests.RequestException as exc:
            log.warning("SJC home page failed (%s), calling the API anyway", exc)
        headers = {**BROWSER_HEADERS, "Referer": SJC_HOME_URL,
                   "Origin": SJC_HOME_URL.rstrip("/"), "X-Requested-With": "XMLHttpRequest"}
        resp = http("POST", SJC_URL, session=session, headers=headers,
                    data={"method": "GetCurrentGoldPricesByBranch", "BranchId": "1"})
        return parse_sjc(resp.json(), ts)


def fetch_doji_direct(ts: str) -> list[dict]:
    errors = []
    for url in DOJI_URLS:  # some networks only reach the plain-HTTP host
        try:
            resp = http("GET", url, headers=BROWSER_HEADERS)
            rows = parse_doji_xml(resp.content, ts)
            if rows:
                return rows
            errors.append(f"{url}: no prices, body {snippet(resp)}")
        except Exception as exc:  # noqa: BLE001 - try the next URL
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


_vnappmob_token: dict = {}


def vnappmob_token() -> str:
    # The API hands out a free, short-lived token on request; it is kept in
    # memory for this run only and never stored or logged. A failure is
    # remembered too, so six gold sources do not each retry a dead endpoint.
    if "error" in _vnappmob_token:
        raise _vnappmob_token["error"]
    if "key" not in _vnappmob_token:
        try:
            key = http("GET", VNAPPMOB_KEY_URL, params={"scope": "gold"}).json().get("results")
            if not key:
                raise ValueError("vnappmob: no token returned")
            _vnappmob_token["key"] = key
        except Exception as exc:
            _vnappmob_token["error"] = exc
            raise
    return _vnappmob_token["key"]


def fetch_vnappmob(brand: str, ts: str) -> list[dict]:
    resp = http("GET", VNAPPMOB_GOLD_URL.format(brand=brand),
                headers={**HEADERS, "Authorization": f"Bearer {vnappmob_token()}"})
    rows = parse_vnappmob(resp.json(), brand, ts)
    if not rows:
        raise ValueError(f"vnappmob {brand}: no prices, body {snippet(resp)}")
    return rows


def fetch_vnappmob_sjc(ts: str) -> list[dict]:
    return fetch_vnappmob("sjc", ts)


HTML_HEADERS = {**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def fetch_giavang_org(slug: str, ts: str) -> list[dict]:
    resp = http("GET", GIAVANG_ORG_URL.format(slug=slug), headers=HTML_HEADERS)
    rows = parse_gold_html(resp.text, "giavang.org", ts)
    if not rows:
        raise ValueError(f"giavang.org/{slug}: no gold table, body {snippet(resp)}")
    return rows


def fetch_pnj_api(ts: str) -> list[dict]:
    resp = http("GET", PNJ_API_URL, headers=BROWSER_HEADERS, params={"zone": "00"})
    rows = parse_gold_json(resp.json(), "pnj.com.vn", ts)
    if not rows:
        raise ValueError(f"PNJ API: no prices, body {snippet(resp)}")
    return rows


def fetch_pnj_html(ts: str) -> list[dict]:
    resp = http("GET", PNJ_HTML_URL, headers=HTML_HEADERS)
    rows = parse_gold_html(resp.text, "pnj.com.vn", ts)
    if not rows:
        raise ValueError(f"giavang.pnj.com.vn: no gold table, body {snippet(resp)}")
    return rows


# brand -> (item prefix, vnappmob code, giavang.org slug, official providers)
GOLD_BRANDS = {
    "PNJ": ("PNJ", "pnj", "pnj",
            [("pnj.com.vn API", fetch_pnj_api), ("giavang.pnj.com.vn", fetch_pnj_html)]),
    "DOJI": ("DOJI", "doji", "doji", [("DOJI XML", fetch_doji_direct)]),
    "BTMC": ("BTMC", "btmc", "bao-tin-minh-chau", []),
    "BTMH": ("BTMH", "btmh", "bao-tin-manh-hai", []),
    "Phú Quý": ("Phú Quý", "phuquy", "phu-quy", []),
}


def fetch_brand_gold(brand: str, ts: str) -> list[dict]:
    """vnappmob -> giavang.org -> brand site; item names always start with the brand."""
    prefix, code, slug, official = GOLD_BRANDS[brand]
    providers = [("vnappmob.com", lambda: fetch_vnappmob(code, ts)),
                 ("giavang.org", lambda: fetch_giavang_org(slug, ts))]
    providers += [(name, functools.partial(fn, ts)) for name, fn in official]
    rows = first_working(f"{brand} gold", providers)
    for row in rows:
        row["item"] = with_brand(prefix, row["item"])
    return rows


def fetch_vietcombank(ts: str) -> list[dict]:
    try:
        date = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        rows = parse_vcb_json(http("GET", VCB_JSON_URL, params={"date": date}).json(), ts)
        if rows:
            return rows
        log.warning("Vietcombank JSON returned no rates, trying XML")
    except Exception as exc:  # noqa: BLE001 - fall back to the legacy XML feed
        log.warning("Vietcombank JSON failed (%s), trying XML", exc)
    return parse_vcb_xml(http("GET", VCB_XML_URL).content, ts)


def _gold_api(ts: str) -> list[dict]:
    return [r for sym in METAL_SYMBOLS
            for r in parse_gold_api(http("GET", GOLD_API_URL.format(symbol=sym)).json(), sym, ts)]


def _goldprice_org(ts: str) -> list[dict]:
    return parse_goldprice_org(http("GET", GOLDPRICE_ORG_URL, headers=BROWSER_HEADERS).json(), ts)


def _stooq(ts: str) -> list[dict]:
    return [r for sym in METAL_SYMBOLS
            for r in parse_stooq(http("GET", STOOQ_URL, headers=BROWSER_HEADERS,
                                      params={"s": f"{sym.lower()}usd", "f": "sd2t2ohlc",
                                              "h": "", "e": "csv"}).text, sym, ts)]


METAL_PROVIDERS = (_gold_api, _goldprice_org, _stooq)


def fetch_metals(ts: str) -> list[dict]:
    """Return XAU/XAG from the first provider that works; `source` names that provider."""
    return first_working("Metals spot", [(p.__name__.lstrip("_"), functools.partial(p, ts))
                                         for p in METAL_PROVIDERS])


def fetch_central_rate(ts: str) -> list[dict]:
    def provider(source, url):
        def run():
            resp = http("GET", url, headers=HTML_HEADERS)
            rows = parse_central_rate(resp.text, source, ts)
            if rows:
                return rows
            # The SBV home page only links to the central-rate page (the value on
            # the home page is filled in by JavaScript): follow that link.
            notes = [f"{url}: {central_rate_context(resp.text)!r}"]
            for link in central_rate_links(resp.text, resp.url or url):
                try:
                    page = http("GET", link, headers=HTML_HEADERS)
                    rows = parse_central_rate(page.text, source, ts)
                    if rows:
                        log.info("SBV central rate: found on linked page %s", link)
                        return rows
                    notes.append(f"{link}: {central_rate_context(page.text)!r}")
                except Exception as exc:  # noqa: BLE001 - try the next link
                    notes.append(f"{link}: {exc}")
            raise ValueError("no central rate found; " + " | ".join(notes))
        return run
    return first_working("SBV central rate", [(s, provider(s, u)) for s, u in SBV_CENTRAL_URLS])


def _json(url: str, **params):
    return http("GET", url, headers=BROWSER_HEADERS, params=params).json()


# Close of the PNJ session fetched in this run, used to check foreign values.
_session_close: dict = {}


def _vci_post(url: str, body: dict):
    return http("POST", url, headers={**BROWSER_HEADERS, **VCI_HEADERS}, json=body).json()


def fetch_pnj_stock(ts: str) -> list[dict]:
    now = int(time.time())
    providers = [
        ("vietcap.com.vn", lambda: parse_ohlc_arrays(_vci_post(
            VCI_CHART_URL, {"timeFrame": "ONE_DAY", "symbols": [STOCK], "to": now, "countBack": 10}))),
        ("tcbs.com.vn", lambda: parse_tcbs_bars(_json(
            TCBS_BARS_URL, ticker=STOCK, type="stock", resolution="D",
            **{"from": now - 15 * 86400, "to": now}))),
        ("vndirect.com.vn", lambda: parse_vnd_stock(_json(
            VND_STOCK_URL, q=f"code:{STOCK}", sort="date", size=1))),
        ("cafef.vn", lambda: parse_cafef_price(_json(
            CAFEF_PRICE_URL, Symbol=STOCK, StartDate="", EndDate="", PageIndex=1, PageSize=1))),
    ]
    rows = first_working("PNJ stock", [
        (source, lambda s=source, f=fn: stock_session_rows(f(), s, ts)) for source, fn in providers])
    close = next(r for r in rows if r["item"] == f"{STOCK} đóng cửa")
    _session_close["close"] = Decimal(close["sell"])
    return rows


def fetch_pnj_foreign(ts: str) -> list[dict]:
    providers = [
        ("vietcap.com.vn", lambda: parse_vci_board_foreign(_vci_post(VCI_BOARD_URL, {"symbols": [STOCK]}))),
        ("vndirect.com.vn", lambda: parse_vnd_foreign(_json(
            VND_FOREIGN_URL, q=f"code:{STOCK}", sort="tradingDate", size=1))),
        # CafeF's GDKhoiNgoai is not used: on 2026-10-03 it returned foreign
        # volumes ~190x below the exchange's figures. parse_cafef_foreign stays
        # for reference/tests only.
    ]
    return first_working("PNJ foreign", [
        (source, lambda s=source, f=fn: stock_foreign_rows(f(), s, ts, _session_close.get("close")))
        for source, fn in providers])


# Every source gets one row in health.csv. Sources that refuse non-Vietnam IPs
# stay listed so health.csv shows blocked_non_vn instead of hiding them.
FETCHERS = {
    "SJC (sjc.com.vn)": fetch_sjc,
    "SJC via vnappmob.com": fetch_vnappmob_sjc,
    **{f"{brand} gold": functools.partial(fetch_brand_gold, brand) for brand in GOLD_BRANDS},
    "Vietcombank": fetch_vietcombank,
    "SBV central rate": fetch_central_rate,
    "Metals spot": fetch_metals,
    "PNJ stock": fetch_pnj_stock,
    "PNJ foreign trading": fetch_pnj_foreign,
}


# --------------------------------------------------------------------------- #
# CSV handling
# --------------------------------------------------------------------------- #
def key_of(row: dict) -> tuple:
    return tuple(row[c] for c in KEY_COLUMNS)


def read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == LEGACY_COLUMNS:
            log.info("%s: migrating legacy header, adding last_checked", path)
            rows = [{**row, "last_checked": row["timestamp"]} for row in reader]
        elif reader.fieldnames == COLUMNS:
            rows = list(reader)
        else:
            raise ValueError(f"{path} header {reader.fieldnames} does not match {COLUMNS}")
    rows = [r for r in rows
            if (r["source"], r["item"], r["timestamp"]) not in DATA_CORRECTIONS
            and not (r["category"] == "gold" and r["source"] in GOLD_FILTERED_SOURCES
                     and not keep_gold_label(r["item"]))]  # same filter as collection
    for row in rows:
        row["item"] = ITEM_RENAMES.get((row["source"], row["item"]), row["item"])
        if row["category"] == "gold" and row["currency"] == "VND":
            for col in ("buy", "sell"):  # early rows carried fractional VND
                if "." in row[col]:
                    row[col] = fmt(Decimal(row[col]).quantize(Decimal(1)))
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)  # atomic: never leave a half-written CSV


def merge(history: list[dict], new_rows: list[dict]) -> tuple[list[dict], int]:
    """Append rows whose values differ from the latest recorded row for the same key.

    An unchanged price keeps its row (and its original timestamp); only that
    row's last_checked is moved forward. Keys absent from new_rows (source
    failed or item no longer published) keep their old last_checked.
    """
    latest = {key_of(r): r for r in history}
    added = 0
    for row in new_rows:
        prev = latest.get(key_of(row))
        if prev and all(prev[c] == row[c] for c in VALUE_COLUMNS):
            prev["last_checked"] = row["last_checked"]
            continue
        history.append(row)
        latest[key_of(row)] = row
        added += 1
    return history, added


def latest_rows(history: list[dict]) -> list[dict]:
    latest = {key_of(r): r for r in history}
    return sorted(latest.values(), key=key_of)


def read_health(path: Path) -> dict[str, dict]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {r["source"]: r for r in csv.DictReader(fh) if r.get("source")}


def write_health(path: Path, health: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEALTH_COLUMNS, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(health[name] for name in FETCHERS if name in health)
    tmp.replace(path)


def update_health(previous: dict | None, name: str, ts: str, error: Exception | None) -> dict:
    """ok -> refresh last_success; failure -> refresh last_error and error_msg."""
    row = {c: "" for c in HEALTH_COLUMNS} | (previous or {}) | {"source": name}
    if error is None:
        row["status"], row["last_success"] = "ok", ts
    else:
        row["status"] = "blocked_non_vn" if isinstance(error, BlockedError) else "error"
        row["last_error"] = ts
        row["error_msg"] = " ".join(str(error).split())[:500]
    return row


# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ts = now_vn()
    collected: list[dict] = []
    failed: list[str] = []
    previous_health = read_health(HEALTH_CSV)
    health = {}

    for name, fetcher in FETCHERS.items():
        error = None
        try:
            rows = fetcher(ts)
            if not rows:
                raise ValueError("no prices parsed")
            log.info("%s: %d rows", name, len(rows))
            collected += rows
        except BlockedError as exc:
            error = exc
            failed.append(name)
            # Expected from GitHub-hosted runners; recorded in health.csv, no annotation.
            log.warning("%s: blocked: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the others
            error = exc
            failed.append(name)
            log.error("%s: %s", name, exc)
            # GitHub Actions annotation, visible in the run summary
            print(f"::warning title=Source failed::{name}: {exc}")
        health[name] = update_health(previous_health.get(name), name, ts, error)

    # De-duplicate within this run (keep first occurrence of each key)
    seen, unique = set(), []
    for row in collected:
        if key_of(row) not in seen:
            seen.add(key_of(row))
            unique.append(row)

    history, added = merge(read_csv(PRICES_CSV), unique)
    write_csv(PRICES_CSV, history)
    write_csv(LATEST_CSV, latest_rows(history))
    write_health(HEALTH_CSV, health)
    log.info("Appended %d changed rows, refreshed last_checked for %d (%d total). "
             "Failed sources: %s", added, len(unique) - added, len(history),
             ", ".join(failed) or "none")

    return 1 if len(failed) == len(FETCHERS) else 0


if __name__ == "__main__":
    sys.exit(main())
