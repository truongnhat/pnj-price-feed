"""Offline tests for parsers and CSV logic. Run: python -m unittest discover -s tests"""

import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402

TS = "2026-10-03T10:17:05+07:00"

SJC_PAYLOAD = {
    "success": True,
    "latestDate": "08:30 03/10/2026",
    "data": [
        {"TypeName": "Vàng SJC 1L, 10L, 1KG", "BranchName": "Hồ Chí Minh",
         "Buy": "119,000", "BuyValue": 119000000.0, "Sell": "121,000", "SellValue": 121000000.0},
        {"TypeName": "Vàng nhẫn SJC 99,99% 1 chỉ, 2 chỉ, 5 chỉ", "BranchName": "Hồ Chí Minh",
         "Buy": "114,500", "Sell": "117,000"},  # text only, thousand VND
        {"TypeName": "Broken", "Buy": "-", "Sell": None},
    ],
}

VCB_JSON = {
    "Count": 2,
    "Data": [
        {"currencyName": "US DOLLAR", "currencyCode": "USD",
         "cash": "26100.00", "transfer": "26130.00", "sell": "26400.00"},
        {"currencyName": "KUWAITI DINAR", "currencyCode": "KWD",
         "cash": "-", "transfer": "85000.00", "sell": "88000.00"},
    ],
}

VCB_XML = """<?xml version="1.0" encoding="utf-8"?>
<ExrateList><DateTime>10/3/2026 8:30:00 AM</DateTime>
<Exrate CurrencyCode="USD" CurrencyName="US DOLLAR" Buy="26,100.00" Transfer="26,130.00" Sell="26,400.00" />
<Exrate CurrencyCode="KWD" CurrencyName="KUWAITI DINAR" Buy="-" Transfer="85,000.00" Sell="88,000.00" />
</ExrateList>"""


class ParserTests(unittest.TestCase):
    def test_sjc(self):
        rows = fp.parse_sjc(SJC_PAYLOAD, TS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["item"], "Vàng SJC 1L, 10L, 1KG (Hồ Chí Minh)")
        self.assertEqual((rows[0]["buy"], rows[0]["sell"]), ("119000000", "121000000"))
        self.assertEqual((rows[1]["buy"], rows[1]["sell"]), ("114500000", "117000000"))
        self.assertTrue(all(r["status"] == "ok" and r["unit"] == "luong" for r in rows))

    def test_sjc_failure(self):
        with self.assertRaises(ValueError):
            fp.parse_sjc({"success": False}, TS)

    def test_vcb_json_and_xml_agree(self):
        a, b = fp.parse_vcb_json(VCB_JSON, TS), fp.parse_vcb_xml(VCB_XML, TS)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 4)
        kwd_cash = next(r for r in a if r["item"] == "KWD" and r["price_type"] == "cash")
        self.assertEqual((kwd_cash["buy"], kwd_cash["sell"], kwd_cash["status"]),
                         ("", "88000", "partial"))

    def test_gold_api(self):
        (row,) = fp.parse_gold_api({"price": 2650.1234, "symbol": "XAU"}, "XAU", TS)
        self.assertEqual((row["buy"], row["sell"], row["category"]), ("2650.12", "2650.12", "gold"))
        with self.assertRaises(ValueError):
            fp.parse_gold_api({}, "XAG", TS)

    def test_timestamp_is_vietnam_iso8601(self):
        self.assertRegex(fp.now_vn(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+07:00$")

    def test_number_format_has_no_exponent(self):
        self.assertEqual(fp.fmt(fp.to_number("1.20E+8")), "120000000")
        self.assertEqual(fp.fmt(fp.to_number("26,130.50")), "26130.5")


class CsvTests(unittest.TestCase):
    def test_merge_skips_unchanged_and_appends_changes(self):
        rows = fp.parse_vcb_json(VCB_JSON, TS)
        history, added = fp.merge([], rows)
        self.assertEqual(added, 4)
        history, added = fp.merge(history, fp.parse_vcb_json(VCB_JSON, "2026-10-03T11:17:05+07:00"))
        self.assertEqual(added, 0)
        changed = fp.parse_vcb_json(VCB_JSON, "2026-10-03T12:17:05+07:00")
        changed[0]["sell"] = "26450"
        history, added = fp.merge(history, changed)
        self.assertEqual(added, 1)
        self.assertEqual(len(fp.latest_rows(history)), 4)

    def test_merge_refreshes_last_checked_only(self):
        history, _ = fp.merge([], fp.parse_vcb_json(VCB_JSON, TS))
        later = "2026-10-03T11:17:05+07:00"
        history, added = fp.merge(history, fp.parse_vcb_json(VCB_JSON, later))
        self.assertEqual(added, 0)
        self.assertTrue(all(r["timestamp"] == TS and r["last_checked"] == later for r in history))

    def test_main_end_to_end(self):
        gold_down = mock.Mock(side_effect=RuntimeError("down"))
        fake = {"SJC": lambda ts: fp.parse_sjc(SJC_PAYLOAD, ts),
                "Vietcombank": lambda ts: fp.parse_vcb_json(VCB_JSON, ts),
                "gold-api.com": gold_down}
        gold_ok = lambda ts: fp.parse_gold_api({"price": 2650.1}, "XAU", ts)  # noqa: E731
        t1, t2, t3 = (f"2026-10-03T{h}:17:05+07:00" for h in ("10", "11", "12"))
        with tempfile.TemporaryDirectory() as d:
            prices, latest = Path(d) / "prices.csv", Path(d) / "latest.csv"
            with mock.patch.object(fp, "FETCHERS", fake), \
                 mock.patch.object(fp, "PRICES_CSV", prices), \
                 mock.patch.object(fp, "LATEST_CSV", latest):
                with mock.patch.object(fp, "now_vn", return_value=t1):
                    self.assertEqual(fp.main(), 0)
                first = prices.read_bytes()
                # Second run, same prices: no new rows, only last_checked moves.
                with mock.patch.object(fp, "now_vn", return_value=t2):
                    self.assertEqual(fp.main(), 0)
                self.assertEqual(prices.read_bytes(), first.replace(t1.encode(), t2.encode())
                                 .replace(b"\n" + t2.encode(), b"\n" + t1.encode()))
                # Third run: SJC down, gold-api back. SJC rows keep the t2 last_checked.
                fake["SJC"], fake["gold-api.com"] = gold_down, gold_ok
                with mock.patch.object(fp, "now_vn", return_value=t3):
                    self.assertEqual(fp.main(), 0)
            raw = prices.read_bytes().decode("utf-8")
            self.assertFalse(raw.startswith("\ufeff"))
            self.assertNotIn("\r", raw)
            with prices.open(encoding="utf-8", newline="") as fh:
                data = list(csv.DictReader(fh))
            self.assertEqual(list(data[0].keys()), fp.COLUMNS)
            self.assertEqual(len(data), 7)
            self.assertEqual(len({fp.key_of(r) for r in data}), 7)
            by_source = {}
            for r in data:
                by_source.setdefault(r["source"], set()).add((r["timestamp"], r["last_checked"]))
            self.assertEqual(by_source["SJC"], {(t1, t2)})          # stale source
            self.assertEqual(by_source["Vietcombank"], {(t1, t3)})  # unchanged price, fresh check
            self.assertEqual(by_source["gold-api.com"], {(t3, t3)})
            with latest.open(encoding="utf-8", newline="") as fh:
                self.assertEqual(list(csv.DictReader(fh)), sorted(data, key=fp.key_of))

    def test_legacy_header_is_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.csv"
            p.write_text(",".join(fp.LEGACY_COLUMNS) + "\n"
                         f"{TS},SJC,gold,retail,X,1,2,VND,luong,ok\n", encoding="utf-8")
            (row,) = fp.read_csv(p)
            self.assertEqual(row["last_checked"], TS)
            self.assertEqual(list(row.keys()), fp.COLUMNS)

    def test_main_all_sources_fail(self):
        boom = mock.Mock(side_effect=RuntimeError("down"))
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(fp, "FETCHERS", {"a": boom, "b": boom}), \
             mock.patch.object(fp, "PRICES_CSV", Path(d) / "p.csv"), \
             mock.patch.object(fp, "LATEST_CSV", Path(d) / "l.csv"):
            self.assertEqual(fp.main(), 1)

    def test_header_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.csv"
            p.write_text("a,b\n1,2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                fp.read_csv(p)

    def test_committed_csv_header(self):
        root = Path(fp.__file__).resolve().parent.parent / "data"
        for name in ("prices.csv", "latest.csv"):
            header = (root / name).read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(header, ",".join(fp.COLUMNS))
            self.assertTrue(header.endswith(",last_checked"))
            self.assertIsNone(re.search(r"\s", header))


if __name__ == "__main__":
    unittest.main()
