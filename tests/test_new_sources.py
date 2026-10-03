"""Offline tests for brand gold, PNJ stock, SBV central rate and health.csv.

Fixtures in tests/fixtures are hand-written samples of each provider's format.
Run: python -m unittest discover -s tests
"""

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402

TS = "2026-10-03T15:17:05+07:00"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def by_item(rows):
    return {r["item"]: (r["buy"], r["sell"]) for r in rows}


class HelperTests(unittest.TestCase):
    def test_parse_number_vietnamese_formats(self):
        cases = {"14.200": "14200", "14,200": "14200", "14,150,000": "14150000",
                 "98.5": "98.5", "4,141.8": "4141.8", "26.130,50": "26130.50",
                 "14.210 ▲10": "14210", 98500: "98500", 97.2: "97.2"}
        for raw, expected in cases.items():
            self.assertEqual(fp.parse_number(raw), fp.Decimal(expected), raw)
        for raw in ("-", "", None, "N/D", 0, "1.2.34"):
            self.assertIsNone(fp.parse_number(raw), raw)
        self.assertEqual(fp.parse_number(0, allow_zero=True), 0)

    def test_html_tables_expand_rowspan(self):
        grid = fp.html_tables(fixture("giavang_org_pnj.html"))[0]
        self.assertEqual(grid[2], ["TP.HCM", "Vàng miếng SJC 999.9", "14.050", "14.350"])
        self.assertEqual(grid[5], ["Hà Nội", "Bạc 999", "150", "160"])

    def test_json_price_pairs_keeps_region_context(self):
        pairs = fp.json_price_pairs(fixture("pnj_api.json"))
        self.assertEqual(pairs[0], ("TPHCM Nhẫn Trơn PNJ 999.9", "14.200", "14.500"))
        self.assertEqual(len(pairs), 3)

    def test_with_brand(self):
        self.assertEqual(fp.with_brand("BTMC", "BTMC VÀNG MIẾNG VRTL"), "BTMC VÀNG MIẾNG VRTL")
        self.assertEqual(fp.with_brand("Phú Quý", "Nhẫn tròn 999.9"), "Phú Quý Nhẫn tròn 999.9")


class BrandGoldTests(unittest.TestCase):
    def test_giavang_org_pnj_filters_and_converts(self):
        rows = fp.parse_gold_html(fixture("giavang_org_pnj.html"), "giavang.org", TS)
        self.assertEqual(by_item(rows), {
            "TP.HCM Nhẫn Trơn PNJ 999.9": ("142000000", "145000000"),
            "TP.HCM Vàng miếng SJC 999.9": ("140500000", "143500000"),
            "Hà Nội Nhẫn Trơn PNJ 999.9": ("142100000", "145100000"),
        })  # 18K jewelry and silver dropped; USD world table skipped
        self.assertTrue(all(r["unit"] == "luong" and r["currency"] == "VND" for r in rows))

    def test_giavang_org_btmc_partial_row(self):
        rows = fp.parse_gold_html(fixture("giavang_org_btmc.html"), "giavang.org", TS)
        got = {r["item"]: (r["buy"], r["sell"], r["status"]) for r in rows}
        self.assertEqual(got, {
            "BTMC VÀNG MIẾNG VRTL": ("141500000", "144500000", "ok"),
            "BTMC NHẪN TRÒN TRƠN": ("141000000", "144000000", "ok"),
            "BTMC VÀNG NGUYÊN LIỆU": ("139000000", "", "partial"),
        })

    def test_pnj_api_json(self):
        rows = fp.parse_gold_json(fixture("pnj_api.json"), "pnj.com.vn", TS)
        self.assertEqual(by_item(rows), {
            "TPHCM Nhẫn Trơn PNJ 999.9": ("142000000", "145000000"),
            "TPHCM Vàng miếng SJC 999.9": ("140500000", "143500000"),
        })

    def test_vnappmob_falls_back_to_named_records(self):
        payload = {"results": {"items": [{"name": "Nhẫn 999.9", "buy": 14200000, "sell": 14500000}]}}
        rows = fp.parse_vnappmob(payload, "phuquy", TS)
        self.assertEqual(by_item(rows), {"Nhẫn 999.9": ("142000000", "145000000")})

    def test_brand_chain_uses_next_provider_and_prefixes_items(self):
        html_rows = fp.parse_gold_html(fixture("giavang_org_pnj.html"), "giavang.org", TS)
        with mock.patch.object(fp, "fetch_vnappmob", side_effect=RuntimeError("404")), \
             mock.patch.object(fp, "fetch_giavang_org", return_value=html_rows), \
             mock.patch.object(fp.log, "warning"):
            rows = fp.fetch_brand_gold("PNJ", TS)
        self.assertEqual({r["source"] for r in rows}, {"giavang.org"})
        self.assertIn("PNJ TP.HCM Nhẫn Trơn PNJ 999.9", by_item(rows))
        self.assertTrue(all(r["item"].startswith("PNJ ") for r in rows))

    def test_brand_chain_all_blocked_raises_blocked(self):
        blocked = fp.BlockedError("HTTP 403")
        with mock.patch.object(fp, "fetch_vnappmob", side_effect=blocked), \
             mock.patch.object(fp, "fetch_giavang_org", side_effect=blocked), \
             mock.patch.object(fp.log, "warning"):
            with self.assertRaises(fp.BlockedError):
                fp.fetch_brand_gold("BTMH", TS)  # no official provider: 2 of 2 blocked

    def test_mixed_failures_are_errors_not_blocked(self):
        with mock.patch.object(fp.log, "warning"):
            with self.assertRaises(RuntimeError) as ctx:
                fp.first_working("x", [("a", mock.Mock(side_effect=fp.BlockedError("403"))),
                                       ("b", mock.Mock(side_effect=ValueError("bad json")))])
        self.assertNotIsInstance(ctx.exception, fp.BlockedError)
        self.assertIn("b: bad json", str(ctx.exception))


class StockTests(unittest.TestCase):
    EXPECTED = {"PNJ ngày phiên": ("20261002", "20261002"), "PNJ mở cửa": ("97200", "97200"),
                "PNJ cao nhất": ("98800", "98800"), "PNJ thấp nhất": ("96900", "96900"),
                "PNJ đóng cửa": ("98500", "98500"), "PNJ KL khớp lệnh": ("1234500", "1234500")}

    def test_all_session_parsers_agree(self):
        for parser, name in ((fp.parse_tcbs_bars, "tcbs_bars.json"),
                             (fp.parse_vnd_stock, "vnd_stock.json"),
                             (fp.parse_cafef_price, "cafef_price.json")):
            rows = fp.stock_session_rows(parser(fixture(name)), "x", TS)
            self.assertEqual(by_item(rows), self.EXPECTED, name)
            units = {r["item"]: (r["price_type"], r["currency"], r["unit"]) for r in rows}
            self.assertEqual(units["PNJ đóng cửa"], ("session", "VND", "1 cp"))
            self.assertEqual(units["PNJ KL khớp lệnh"], ("session", "", "cp"))
            self.assertEqual(units["PNJ ngày phiên"], ("session_date", "", "yyyymmdd"))
            self.assertTrue(all(r["category"] == "stock" for r in rows))

    def test_implausible_price_is_rejected(self):
        with self.assertRaises(ValueError):
            fp.stock_session_rows({"date": "2026-10-02", "open": 0.5, "high": 1, "low": 1,
                                   "close": 1, "volume": 1}, "x", TS)

    def test_foreign_parsers_agree_across_value_units(self):
        expected = {"PNJ NN ngày phiên": ("20261002", "20261002"),
                    "PNJ NN KL mua/bán": ("210000", "305000"),
                    "PNJ NN GT mua/bán": ("20685000000", "30042500000")}
        for parser, name in ((fp.parse_vnd_foreign, "vnd_foreign.json"),
                             (fp.parse_cafef_foreign, "cafef_foreign.json")):  # VND vs million VND
            rows = fp.stock_foreign_rows(parser(fixture(name)), "x", TS, close=fp.Decimal(98500))
            self.assertEqual(by_item(rows), expected, name)

    def test_foreign_zero_sell_volume_is_kept(self):
        rows = fp.stock_foreign_rows({"date": "2026-10-02", "buy_vol": 1000, "sell_vol": 0,
                                      "buy_val": 98500000, "sell_val": 0}, "x", TS,
                                     close=fp.Decimal(98500))
        self.assertEqual(by_item(rows)["PNJ NN KL mua/bán"], ("1000", "0"))
        self.assertEqual(by_item(rows)["PNJ NN GT mua/bán"], ("98500000", "0"))

    def test_foreign_values_need_a_consistent_close(self):
        # The live CafeF case: value / volume = 7,102 VND per share. No unit of
        # 1/1e3/1e6/1e9 makes that ~98,500, so the value row is left out.
        bad = {"date": "2026-10-02", "buy_vol": 22400, "sell_vol": 67800,
               "buy_val": 159.096, "sell_val": 483.344}
        with mock.patch.object(fp.log, "warning") as warn:
            rows = fp.stock_foreign_rows(bad, "cafef.vn", TS, close=fp.Decimal(98500))
        self.assertNotIn("PNJ NN GT mua/bán", by_item(rows))
        self.assertEqual(by_item(rows)["PNJ NN KL mua/bán"], ("22400", "67800"))
        warn.assert_called_once()
        good = dict(bad, buy_val=2206.4, sell_val=6678.3)  # million VND, ~98,500 per share
        with mock.patch.object(fp.log, "warning"):
            self.assertNotIn("PNJ NN GT mua/bán", by_item(fp.stock_foreign_rows(good, "x", TS)))
        rows = fp.stock_foreign_rows(good, "x", TS, close=fp.Decimal(98500))
        self.assertEqual(by_item(rows)["PNJ NN GT mua/bán"], ("2206400000", "6678300000"))

    def test_vietcap_chart(self):
        session = fp.parse_ohlc_arrays(fixture("vci_chart.json"))
        self.assertEqual(session["date"], "2026-10-02")  # 1790874000 = 2026-10-02 00:00 +07:00
        self.assertEqual(by_item(fp.stock_session_rows(session, "vietcap.com.vn", TS)), self.EXPECTED)
        with self.assertRaises(ValueError):
            fp.parse_ohlc_arrays({"data": []})

    def test_vietcap_board_foreign(self):
        foreign = fp.parse_vci_board_foreign(fixture("vci_board.json"))
        self.assertEqual((foreign["buy_vol"], foreign["sell_vol"], foreign["buy_val"], foreign["date"]),
                         (210000, 305000, 20685000000, None))
        rows = fp.stock_foreign_rows(foreign, "vietcap.com.vn", TS, close=fp.Decimal(98500))
        self.assertEqual(by_item(rows), {"PNJ NN KL mua/bán": ("210000", "305000"),
                                         "PNJ NN GT mua/bán": ("20685000000", "30042500000")})
        with self.assertRaises(ValueError) as ctx:
            fp.parse_vci_board_foreign([{"matchPrice": {"matchPrice": 1}}])
        self.assertIn("matchPrice", str(ctx.exception))  # lists available fields

    def test_stock_chain_records_close_for_foreign_check(self):
        with mock.patch.dict(fp._session_close, clear=True), \
             mock.patch.object(fp, "_vci_post", return_value=fixture("vci_chart.json")):
            fp.fetch_pnj_stock(TS)
            self.assertEqual(fp._session_close["close"], 98500)


class CentralRateTests(unittest.TestCase):
    def test_sbv_page(self):
        (row,) = fp.parse_central_rate(fixture("sbv_central.html"), "sbv.gov.vn", TS)
        self.assertEqual((row["category"], row["price_type"], row["item"], row["buy"], row["sell"],
                          row["unit"]), ("fx", "central", "USD", "25118", "25118", "1 USD"))

    def test_mirror_page_skips_ceiling_and_floor(self):
        (row,) = fp.parse_central_rate(fixture("webgia_central.html"), "webgia.com", TS)
        self.assertEqual(row["buy"], "25118")

    def test_other_currency_is_not_taken_for_usd(self):
        page = "<p>Tỷ giá trung tâm EUR: 27.950 đồng/EUR</p><p>1 USD = 25.118 VND</p>"
        (row,) = fp.parse_central_rate(page, "x", TS)
        self.assertEqual(row["buy"], "25118")
        self.assertEqual(fp.parse_central_rate("<p>Tỷ giá trung tâm EUR: 27.950</p>", "x", TS), [])

    def test_sbv_home_page_and_english(self):
        (row,) = fp.parse_central_rate(fixture("sbv_home.html"), "sbv.gov.vn", TS)
        self.assertEqual(row["buy"], "25118")
        (row,) = fp.parse_central_rate("<p>Central exchange rate USD/VND</p><p>25,118.00</p>", "x", TS)
        self.assertEqual(row["buy"], "25118")

    def test_date_between_label_and_rate(self):
        page = "<div>Tỷ giá trung tâm (áp dụng 03/10/2026 08:30)</div><div>USD/VND 25.118</div>"
        (row,) = fp.parse_central_rate(page, "x", TS)
        self.assertEqual(row["buy"], "25118")
        page = "<div>Tỷ giá trung tâm 03/10/2026</div><div>Tỷ giá trần USD 26.374</div>"
        self.assertEqual(fp.parse_central_rate(page, "x", TS), [])  # stops at "trần"

    def test_failure_context_shows_text_near_label(self):
        page = "<html><nav>Menu</nav><p>Tỷ giá trung tâm</p><script>load()</script></html>"
        self.assertIn("Tỷ giá trung tâm", fp.central_rate_context(page))
        self.assertEqual(fp.central_rate_context("<p>none</p>"), "label not in page text")

    def test_central_rate_table(self):
        (row,) = fp.parse_central_rate(fixture("sbv_central_table.html"), "sbv.gov.vn", TS)
        self.assertEqual(row["buy"], "25118")  # USD row, not EUR

    def test_follows_menu_link_when_home_page_has_no_value(self):
        home = mock.Mock(text=fixture("sbv_home_js.html"), url="https://sbv.gov.vn/vi/trang-chu")
        table = mock.Mock(text=fixture("sbv_central_table.html"))
        self.assertEqual(fp.central_rate_links(home.text, home.url),
                         ["https://sbv.gov.vn/vi/web/guest/ty-gia-trung-tam?p_p_id=abc&x=1"])
        with mock.patch.object(fp, "http", side_effect=[home, table]) as http, \
             mock.patch.object(fp, "SBV_CENTRAL_URLS", (("sbv.gov.vn", home.url),)):
            (row,) = fp.fetch_central_rate(TS)
        self.assertEqual((row["source"], row["buy"]), ("sbv.gov.vn", "25118"))
        self.assertEqual(http.call_args_list[1].args[1],
                         "https://sbv.gov.vn/vi/web/guest/ty-gia-trung-tam?p_p_id=abc&x=1")

    def test_no_rate_found(self):
        self.assertEqual(fp.parse_central_rate("<p>Tỷ giá trung tâm: đang cập nhật</p>", "x", TS), [])


class HealthTests(unittest.TestCase):
    def run_main(self, fetchers, d, ts):
        with mock.patch.object(fp, "FETCHERS", fetchers), \
             mock.patch.object(fp, "PRICES_CSV", Path(d) / "p.csv"), \
             mock.patch.object(fp, "LATEST_CSV", Path(d) / "l.csv"), \
             mock.patch.object(fp, "HEALTH_CSV", Path(d) / "health.csv"), \
             mock.patch.object(fp, "now_vn", return_value=ts), \
             mock.patch.object(fp.logging, "basicConfig"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = fp.main()
        with (Path(d) / "health.csv").open(encoding="utf-8", newline="") as fh:
            return code, {r["source"]: r for r in csv.DictReader(fh)}, out.getvalue()

    def test_health_statuses_and_history(self):
        ok = lambda ts: fp.parse_central_rate(fixture("sbv_central.html"), "sbv.gov.vn", ts)  # noqa: E731
        blocked = mock.Mock(side_effect=fp.BlockedError("GET https://sjc.com.vn: HTTP 403"))
        broken = mock.Mock(side_effect=ValueError("no gold table"))
        t1, t2 = TS, "2026-10-03T16:17:05+07:00"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(fp.log, "warning"), \
                mock.patch.object(fp.log, "error"):
            code, health, out = self.run_main({"A": ok, "SJC": blocked, "B": broken}, d, t1)
            self.assertEqual(code, 0)
            self.assertEqual(list(health), ["A", "SJC", "B"])
            self.assertEqual((health["A"]["status"], health["A"]["last_success"]), ("ok", t1))
            self.assertEqual(health["SJC"]["status"], "blocked_non_vn")
            self.assertEqual(health["B"]["status"], "error")
            self.assertEqual((health["B"]["last_error"], health["B"]["error_msg"]),
                             (t1, "no gold table"))
            self.assertNotIn("SJC", out)      # expected block: no Actions warning
            self.assertIn("::warning", out)   # real error: warning
            # Second run: A fails, B recovers. A keeps its last_success.
            code, health, _ = self.run_main({"A": broken, "SJC": blocked, "B": ok}, d, t2)
            self.assertEqual((health["A"]["status"], health["A"]["last_success"],
                              health["A"]["last_error"]), ("error", t1, t2))
            self.assertEqual((health["B"]["status"], health["B"]["last_success"],
                              health["B"]["last_error"]), ("ok", t2, t1))
            with (Path(d) / "health.csv").open(encoding="utf-8") as fh:
                self.assertEqual(fh.readline().strip(), ",".join(fp.HEALTH_COLUMNS))

    def test_http_403_raises_blocked(self):
        resp = mock.Mock(status_code=403)
        resp.raise_for_status.side_effect = fp.requests.HTTPError("403", response=resp)
        with mock.patch.object(fp.requests, "request", return_value=resp) as req, \
             mock.patch.object(fp.log, "warning"):
            with self.assertRaises(fp.BlockedError):
                fp.http("GET", "https://example.invalid")
        self.assertEqual(req.call_count, 1)

    def test_vnappmob_token_failure_is_not_retried_per_brand(self):
        with mock.patch.dict(fp._vnappmob_token, clear=True), \
             mock.patch.object(fp, "http", side_effect=RuntimeError("down")) as http:
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    fp.vnappmob_token()
        self.assertEqual(http.call_count, 1)

    def test_committed_health_header(self):
        header = (FIXTURES.parent.parent / "data" / "health.csv").read_text(encoding="utf-8")
        columns = header.splitlines()[0].split(",")
        # May predate source_stale until the next run rewrites it.
        self.assertEqual(columns, fp.HEALTH_COLUMNS[:len(columns)])
        self.assertGreaterEqual(len(columns), 5)

    def test_connect_timeout_marks_host_dead_for_the_run(self):
        err = fp.requests.exceptions.ConnectTimeout("timed out")
        with mock.patch.object(fp, "_dead_hosts", set()), \
             mock.patch.object(fp.requests, "request", side_effect=err) as req:
            for path in ("/a", "/b"):
                with self.assertRaises(RuntimeError):
                    fp.http("GET", "https://slow.example" + path)
        self.assertEqual(req.call_count, 1)

    def test_read_csv_applies_gold_filter_and_corrections(self):
        header = ",".join(fp.COLUMNS)
        lines = [
            f"{TS},giavang.org,gold,retail,BTMC Quà Mừng Vàng 999.9,132600000,,VND,luong,partial,{TS}",
            f"{TS},giavang.org,gold,retail,BTMC VRTL Vàng miếng 999.9,139500000,143500000,VND,luong,ok,{TS}",
            "2026-10-03T15:20:06+07:00,cafef.vn,stock,foreign,PNJ NN GT mua/bán,159096000,483344000,"
            f"VND,VND,ok,{TS}",
            f"{TS},cafef.vn,stock,foreign,PNJ NN KL mua/bán,22400,67800,,cp,ok,{TS}",
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.csv"
            path.write_text("\n".join([header] + lines) + "\n", encoding="utf-8")
            items = [r["item"] for r in fp.read_csv(path)]
        self.assertEqual(items, ["BTMC VRTL Vàng miếng 999.9", "PNJ NN KL mua/bán"])

    def test_every_source_has_a_health_name(self):
        self.assertIn("SJC (sjc.com.vn)", fp.FETCHERS)
        for name in ("PNJ gold", "DOJI gold", "BTMC gold", "BTMH gold", "Phú Quý gold",
                     "SBV central rate", "PNJ stock", "PNJ foreign trading"):
            self.assertIn(name, fp.FETCHERS)


if __name__ == "__main__":
    unittest.main()
