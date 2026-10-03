#!/usr/bin/env python3
"""Fetch publicly available market prices and append changes to data/prices.csv.

Sources (all public, no authentication):
  - SJC          : domestic gold prices (sjc.com.vn; blocks non-Vietnam IPs)
  - DOJI         : domestic gold prices (giavang.doji.vn public XML feed)
  - vnappmob.com : SJC gold prices via a free public API (short-lived token is
                   requested at runtime; nothing is stored in the repository)
  - Vietcombank  : VND exchange rates (vietcombank.com.vn)
  - Metals spot  : international spot prices for gold (XAU) and silver (XAG),
                   first working provider of gold-api.com, goldprice.org, stooq.com

Output files (UTF-8, no BOM, comma-separated, "\n" line endings):
  - data/prices.csv : price history. A row is appended only when a price
                      differs from the last recorded row for the same key;
                      otherwise only that row's last_checked is refreshed.
  - data/latest.csv : the latest row per key (same schema).

timestamp    = first time this specific price was observed.
last_checked = most recent successful check of the source that returned it.

Exit codes: 0 if at least one source succeeded, 1 if every source failed.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

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

TIMEOUT = 30
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
VNAPPMOB_SJC_URL = "https://api.vnappmob.com/api/v2/gold/sjc"

# Plausible domestic gold price range in VND per luong (37.5 g); covers 10K-24K gold.
GOLD_LUONG_MIN, GOLD_LUONG_MAX = 30_000_000, 1_000_000_000

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


def http(method: str, url: str, session: requests.Session | None = None,
         headers: dict | None = None, **kwargs) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = (session or requests).request(method, url, headers=headers or HEADERS,
                                                 timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
            log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt, RETRIES, exc)
            status = getattr(exc.response, "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                break  # client errors (e.g. 403 geo-block) do not fix themselves on retry
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{method} {url} failed: {last_error}")


def snippet(resp: requests.Response) -> str:
    """Start of a response body, for error messages when parsing finds nothing."""
    return repr(resp.text[:200])


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
    parsed = [(item, to_number(b), to_number(s)) for item, b, s in pairs if item]
    factor = gold_multiplier([v for _, b, s in parsed for v in (b, s) if v is not None])
    rows, seen = [], set()
    for item, buy, sell in parsed:
        buy, sell = (v * factor if v is not None else None for v in (buy, sell))
        buy, sell = (v if v is not None and GOLD_LUONG_MIN <= v <= GOLD_LUONG_MAX else None
                     for v in (buy, sell))
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


VNAPPMOB_LABELS = {
    "1l": "SJC 1L, 10L, 1KG",
    "1c": "SJC 1 chỉ, 2 chỉ, 5 chỉ",
    "nhan1c": "SJC nhẫn 99,99% 1-5 chỉ",
    "trangsuc49": "SJC nữ trang 99,99%",
    "trangsuc99": "SJC nữ trang 99%",
    "75l": "SJC nữ trang 75%",
    "58l": "SJC nữ trang 58,3%",
    "41l": "SJC nữ trang 41,7%",
}


def parse_vnappmob_sjc(payload: dict, ts: str) -> list[dict]:
    """{"results": [{"buy_1l": ..., "sell_1l": ..., "buy_nhan1c": ..., "datetime": ...}]}"""
    results = payload.get("results") if isinstance(payload, dict) else None
    rec = results[0] if isinstance(results, list) and results else None
    if not isinstance(rec, dict):
        raise ValueError("vnappmob: no results")
    codes = sorted({k[4:] for k in rec if k.startswith("buy_")} |
                   {k[5:] for k in rec if k.startswith("sell_")})
    pairs = [(VNAPPMOB_LABELS.get(c, f"SJC {c}"), rec.get(f"buy_{c}"), rec.get(f"sell_{c}"))
             for c in codes]
    return gold_rows(ts, "vnappmob.com", pairs)


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


def fetch_doji(ts: str) -> list[dict]:
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


def fetch_vnappmob_sjc(ts: str) -> list[dict]:
    # The API hands out a free, short-lived token on request; it is used for
    # this run only and never stored.
    key = http("GET", VNAPPMOB_KEY_URL, params={"scope": "gold"}).json().get("results")
    if not key:
        raise ValueError("vnappmob: no token returned")
    resp = http("GET", VNAPPMOB_SJC_URL, headers={**HEADERS, "Authorization": f"Bearer {key}"})
    rows = parse_vnappmob_sjc(resp.json(), ts)
    if not rows:
        raise ValueError(f"vnappmob: no prices, body {snippet(resp)}")
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
    errors = []
    for provider in METAL_PROVIDERS:
        try:
            return provider(ts)
        except Exception as exc:  # noqa: BLE001 - try the next provider
            log.warning("Metals provider %s failed: %s", provider.__name__.lstrip("_"), exc)
            errors.append(f"{provider.__name__.lstrip('_')}: {exc}")
    raise RuntimeError("all metals providers failed: " + " | ".join(errors))


FETCHERS = {
    "SJC": fetch_sjc,
    "DOJI": fetch_doji,
    "vnappmob.com (SJC)": fetch_vnappmob_sjc,
    "Vietcombank": fetch_vietcombank,
    "Metals spot": fetch_metals,
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
            return [{**row, "last_checked": row["timestamp"]} for row in reader]
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path} header {reader.fieldnames} does not match {COLUMNS}")
        return list(reader)


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


# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ts = now_vn()
    collected: list[dict] = []
    failed: list[str] = []

    for name, fetcher in FETCHERS.items():
        try:
            rows = fetcher(ts)
            if not rows:
                raise ValueError("no prices parsed")
            log.info("%s: %d rows", name, len(rows))
            collected += rows
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the others
            failed.append(name)
            log.error("%s: %s", name, exc)
            # GitHub Actions annotation, visible in the run summary
            print(f"::warning title=Source failed::{name}: {exc}")

    # De-duplicate within this run (keep first occurrence of each key)
    seen, unique = set(), []
    for row in collected:
        if key_of(row) not in seen:
            seen.add(key_of(row))
            unique.append(row)

    history, added = merge(read_csv(PRICES_CSV), unique)
    write_csv(PRICES_CSV, history)
    write_csv(LATEST_CSV, latest_rows(history))
    log.info("Appended %d changed rows, refreshed last_checked for %d (%d total). "
             "Failed sources: %s", added, len(unique) - added, len(history),
             ", ".join(failed) or "none")

    return 1 if len(failed) == len(FETCHERS) else 0


if __name__ == "__main__":
    sys.exit(main())
