"""Offline tests for parsers and CSV logic. Run: python -m unittest discover -s tests"""

import contextlib
import csv
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402

TS = "2026-10-03T10:17:05+07:00"


def run_main() -> int:
    """Run fp.main() without its ::warning:: lines reaching the real Actions log."""
    with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(fp.logging, "basicConfig"):
        return fp.main()

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

    def test_doji_xml_thousand_vnd_per_chi(self):
        xml = """<?xml version="1.0" encoding="utf-8"?><GoldList>
        <DGPlist><DateTime>14:40 03/10/2026</DateTime>
        <Row Name="DOJI HN lẻ" Key="dojihanoile" Sell="14,300" Buy="14,100" />
        <Row Name="DOJI HCM lẻ" Key="dojihcmle" Sell="14,300" Buy="14,100" /></DGPlist>
        <JewelryList><Row Name="Nữ trang 18K" Key="18k" Sell="10,500" Buy="9,900" />
        <Row Name="DOJI HN lẻ" Key="dup" Sell="1" Buy="1" />
        <Row Name="Bạc" Key="bac" Sell="150" Buy="140" /></JewelryList></GoldList>"""
        rows = fp.parse_doji_xml(xml.encode("utf-8"), TS)
        got = {r["item"]: (r["buy"], r["sell"]) for r in rows}
        self.assertEqual(got, {"DOJI HN lẻ": ("141000000", "143000000"),
                               "DOJI HCM lẻ": ("141000000", "143000000"),
                               "Nữ trang 18K": ("99000000", "105000000")})
        self.assertTrue(all(r["source"] == "DOJI" and r["unit"] == "luong" for r in rows))

    def test_gold_multiplier_units(self):
        for top in ("143000000", "14300000", "143000", "14300"):
            self.assertEqual(fp.to_number(top) * fp.gold_multiplier([fp.to_number(top)]),
                             143000000)
        with self.assertRaises(ValueError):
            fp.gold_multiplier([fp.to_number("2650")])  # USD/oz is not VND/luong
        with self.assertRaises(ValueError):
            fp.gold_multiplier([])

    def test_vnappmob(self):
        payload = {"results": [{"buy_1l": 141000000.0, "sell_1l": 143000000.0,
                                "buy_nutrang_75": 96860651.0651, "sell_nutrang_75": 106660651.5,
                                "buy_xyz": 140000000.0, "datetime": "1759477200"}]}
        rows = fp.parse_vnappmob(payload, "sjc", TS)
        got = {r["item"]: (r["buy"], r["sell"], r["status"]) for r in rows}
        self.assertEqual(got, {"SJC 1L, 10L, 1KG": ("141000000", "143000000", "ok"),
                               "SJC nữ trang 75%": ("96860651", "106660652", "ok"),
                               "SJC xyz": ("140000000", "", "partial")})
        self.assertTrue(all(r["source"] == "vnappmob.com" for r in rows))
        doji = fp.parse_vnappmob({"results": [{"buy_hn": 14050, "sell_hn": 14350}]}, "doji", TS)
        self.assertEqual([(r["item"], r["buy"]) for r in doji], [("DOJI Hà Nội", "140500000")])
        with self.assertRaises(ValueError):
            fp.parse_vnappmob({"results": []}, "sjc", TS)

    def test_read_csv_renames_items_and_rounds_vnd(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.csv"
            p.write_text(",".join(fp.COLUMNS) + "\n"
                         f"{TS},vnappmob.com,gold,retail,SJC nutrang_75,96860651.0651,"
                         f"106660651.0651,VND,luong,ok,{TS}\n"
                         f"{TS},gold-api.com,gold,spot,XAU,4141.8,4141.8,USD,troy_oz,ok,{TS}\n",
                         encoding="utf-8")
            sjc, xau = fp.read_csv(p)
        self.assertEqual((sjc["item"], sjc["buy"], sjc["sell"]),
                         ("SJC nữ trang 75%", "96860651", "106660651"))
        self.assertEqual(xau["buy"], "4141.8")  # USD prices keep their decimals

    def test_http_does_not_retry_403(self):
        resp = mock.Mock(status_code=403)
        err = fp.requests.HTTPError("403 Forbidden", response=resp)
        resp.raise_for_status.side_effect = err
        with mock.patch.object(fp.requests, "request", return_value=resp) as req, \
             mock.patch.object(fp.time, "sleep"), mock.patch.object(fp.log, "warning"):
            with self.assertRaises(RuntimeError):
                fp.http("GET", "https://example.invalid")
        self.assertEqual(req.call_count, 1)

    def test_http_does_not_retry_bad_certificate(self):
        err = fp.requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        with mock.patch.object(fp.requests, "request", side_effect=err) as req, \
             mock.patch.object(fp.time, "sleep"), mock.patch.object(fp.log, "warning"):
            with self.assertRaises(RuntimeError):
                fp.http("GET", "https://example.invalid")
        self.assertEqual(req.call_count, 1)

    def test_goldprice_org(self):
        rows = fp.parse_goldprice_org(
            {"items": [{"curr": "USD", "xauPrice": 2650.1, "xagPrice": 31.234}]}, TS)
        self.assertEqual([(r["source"], r["item"], r["sell"]) for r in rows],
                         [("goldprice.org", "XAU", "2650.1"), ("goldprice.org", "XAG", "31.23")])
        with self.assertRaises(ValueError):
            fp.parse_goldprice_org({"items": []}, TS)

    def test_stooq(self):
        text = "Symbol,Date,Time,Open,High,Low,Close\nXAUUSD,2026-10-03,09:25:00,2640,2660,2635,2650.5\n"
        (row,) = fp.parse_stooq(text, "XAU", TS)
        self.assertEqual((row["source"], row["buy"], row["unit"]), ("stooq.com", "2650.5", "troy_oz"))
        with self.assertRaises(ValueError):
            fp.parse_stooq("Symbol,Date,Time,Open,High,Low,Close\nXAUUSD,N/D,N/D,N/D,N/D,N/D,N/D\n",
                           "XAU", TS)

    def test_metals_fall_back_to_next_provider(self):
        down = mock.Mock(side_effect=RuntimeError("DNS"), __name__="_down")
        ok = lambda ts: fp.parse_goldprice_org(  # noqa: E731
            {"items": [{"curr": "USD", "xauPrice": 1, "xagPrice": 2}]}, ts)
        with mock.patch.object(fp, "METAL_PROVIDERS", (down, ok)):
            self.assertEqual({r["source"] for r in fp.fetch_metals(TS)}, {"goldprice.org"})
        with mock.patch.object(fp, "METAL_PROVIDERS", (down, down)):
            with self.assertRaises(RuntimeError):
                fp.fetch_metals(TS)

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
                 mock.patch.object(fp, "LATEST_CSV", latest), \
                 mock.patch.object(fp, "HEALTH_CSV", Path(d) / "health.csv"):
                with mock.patch.object(fp, "now_vn", return_value=t1):
                    self.assertEqual(run_main(), 0)
                first = prices.read_bytes()
                # Second run, same prices: no new rows, only last_checked moves.
                with mock.patch.object(fp, "now_vn", return_value=t2):
                    self.assertEqual(run_main(), 0)
                self.assertEqual(prices.read_bytes(), first.replace(t1.encode(), t2.encode())
                                 .replace(b"\n" + t2.encode(), b"\n" + t1.encode()))
                # Third run: SJC down, gold-api back. SJC rows keep the t2 last_checked.
                fake["SJC"], fake["gold-api.com"] = gold_down, gold_ok
                with mock.patch.object(fp, "now_vn", return_value=t3):
                    self.assertEqual(run_main(), 0)
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
             mock.patch.object(fp, "LATEST_CSV", Path(d) / "l.csv"), \
             mock.patch.object(fp, "HEALTH_CSV", Path(d) / "h.csv"):
            self.assertEqual(run_main(), 1)

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
