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

    def test_main_end_to_end(self):
        fake = {"SJC": lambda ts: fp.parse_sjc(SJC_PAYLOAD, ts),
                "Vietcombank": lambda ts: fp.parse_vcb_json(VCB_JSON, ts),
                "gold-api.com": mock.Mock(side_effect=RuntimeError("down"))}
        with tempfile.TemporaryDirectory() as d:
            prices, latest = Path(d) / "prices.csv", Path(d) / "latest.csv"
            with mock.patch.object(fp, "FETCHERS", fake), \
                 mock.patch.object(fp, "PRICES_CSV", prices), \
                 mock.patch.object(fp, "LATEST_CSV", latest):
                self.assertEqual(fp.main(), 0)
                first = prices.read_bytes()
                self.assertEqual(fp.main(), 0)  # second run: no changes, no duplicates
                self.assertEqual(prices.read_bytes(), first)
            raw = first.decode("utf-8")
            self.assertFalse(raw.startswith("\ufeff"))
            self.assertNotIn("\r", raw)
            with prices.open(encoding="utf-8", newline="") as fh:
                data = list(csv.DictReader(fh))
            self.assertEqual(list(data[0].keys()), fp.COLUMNS)
            self.assertEqual(len(data), 6)
            self.assertEqual(len({fp.key_of(r) for r in data}), 6)

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
            self.assertIsNone(re.search(r"\s", header))


if __name__ == "__main__":
    unittest.main()
