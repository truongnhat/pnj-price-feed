#!/usr/bin/env python3
"""Fetch publicly available market prices and append changes to data/prices.csv.

Sources (all public, no authentication):
  - SJC          : domestic gold prices (sjc.com.vn)
  - Vietcombank  : VND exchange rates (vietcombank.com.vn)
  - gold-api.com : international spot prices for gold (XAU) and silver (XAG)

Output files (UTF-8, no BOM, comma-separated, "\n" line endings):
  - data/prices.csv : append-only history. A row is written only when a price
                      differs from the last recorded row for the same key.
  - data/latest.csv : the latest row per key (same schema).

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
]
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

SJC_URL = "https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"
VCB_JSON_URL = "https://www.vietcombank.com.vn/api/exchangerates"
VCB_XML_URL = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx"
GOLD_API_URL = "https://api.gold-api.com/price/{symbol}"

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
    }


def http(method: str, url: str, **kwargs) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.request(method, url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
            log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt, RETRIES, exc)
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{method} {url} failed after {RETRIES} attempts: {last_error}")


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


def parse_gold_api(payload: dict, symbol: str, ts: str) -> list[dict]:
    price = to_number(payload.get("price"))
    if price is None:
        raise ValueError(f"gold-api: no price for {symbol}")
    price = price.quantize(Decimal("0.01"))
    category = {"XAU": "gold", "XAG": "silver"}.get(symbol, "metal")
    row = make_row(ts, "gold-api.com", category, "spot", symbol, price, price, "USD", "troy_oz")
    return [row]


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def fetch_sjc(ts: str) -> list[dict]:
    resp = http("POST", SJC_URL, data={"method": "GetCurrentGoldPricesByBranch", "BranchId": "1"})
    return parse_sjc(resp.json(), ts)


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


def fetch_gold_api(ts: str) -> list[dict]:
    rows = []
    for symbol in ("XAU", "XAG"):
        rows += parse_gold_api(http("GET", GOLD_API_URL.format(symbol=symbol)).json(), symbol, ts)
    return rows


FETCHERS = {
    "SJC": fetch_sjc,
    "Vietcombank": fetch_vietcombank,
    "gold-api.com": fetch_gold_api,
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
    """Append rows whose values differ from the latest recorded row for the same key."""
    latest = {key_of(r): r for r in history}
    added = 0
    for row in new_rows:
        prev = latest.get(key_of(row))
        if prev and all(prev[c] == row[c] for c in VALUE_COLUMNS):
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
    log.info("Appended %d changed rows (%d total). Failed sources: %s",
             added, len(history), ", ".join(failed) or "none")

    return 1 if len(failed) == len(FETCHERS) else 0


if __name__ == "__main__":
    sys.exit(main())
