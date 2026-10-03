"""Offline tests for the v3 column source_updated_at and health.csv source_stale.

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


def times(rows):
    return {r["source_updated_at"] for r in rows}


class ParseTimeTests(unittest.TestCase):
    def test_to_vn_iso(self):
        cases = {
            1790874000: "2026-10-02T00:00:00+07:00",              # epoch seconds
            "1790874000": "2026-10-02T00:00:00+07:00",            # as a string
            1790874000000: "2026-10-02T00:00:00+07:00",           # epoch milliseconds
            "2026-10-03T01:30:00Z": "2026-10-03T08:30:00+07:00",  # UTC -> Vietnam
            "2026-10-03T08:30:00+07:00": "2026-10-03T08:30:00+07:00",
            "2026-10-03T08:30:00": "2026-10-03T08:30:00+07:00",   # naive = Vietnam time
            "15:20 03/10/2026": "2026-10-03T15:20:00+07:00",
        }
        for raw, expected in cases.items():
            self.assertEqual(fp.to_vn_iso(raw), expected, raw)
        for raw in (None, "", "N/A", "đang cập nhật", True):
            self.assertEqual(fp.to_vn_iso(raw), "", raw)

    def test_find_vn_datetime(self):
        self.assertEqual(fp.find_vn_datetime("Cập nhật lúc: 03/10/2026 15:20"),
                         "2026-10-03T15:20:00+07:00")
        self.assertEqual(fp.find_vn_datetime("cập nhật 15h20 ngày 03/10/2026"),
                         "2026-10-03T15:20:00+07:00")
        self.assertEqual(fp.find_vn_datetime("áp dụng cho ngày 03/10/2026"),
                         "2026-10-03T00:00:00+07:00")  # date only -> 00:00
        self.assertEqual(fp.find_vn_datetime("10/3/2026 8:30:00 PM", month_first=True),
                         "2026-10-03T20:30:00+07:00")
        self.assertEqual(fp.find_vn_datetime("32/13/2026"), "")
        self.assertEqual(fp.find_vn_datetime("không có ngày"), "")


class SourceTimeTests(unittest.TestCase):
    def test_giavang_org_reads_cap_nhat_line(self):
        self.assertEqual(fp.giavang_org_updated_at(fixture("giavang_org_pnj.html")),
                         "2026-10-03T15:20:00+07:00")
        self.assertEqual(fp.giavang_org_updated_at(fixture("giavang_org_btmc.html")), "")
        menu_first = "<nav>Cập nhật giá vàng</nav><p>Cập nhật lúc: 03/10/2026 15:20</p>"
        self.assertEqual(fp.giavang_org_updated_at(menu_first), "2026-10-03T15:20:00+07:00")
        resp = mock.Mock(text=fixture("giavang_org_pnj.html"))
        with mock.patch.object(fp, "http", return_value=resp):
            rows = fp.fetch_giavang_org("pnj", TS)
        self.assertEqual(times(rows), {"2026-10-03T15:20:00+07:00"})

    def test_page_without_time_stays_empty_not_run_time(self):
        resp = mock.Mock(text=fixture("giavang_org_btmc.html"))
        with mock.patch.object(fp, "http", return_value=resp):
            rows = fp.fetch_giavang_org("bao-tin-minh-chau", TS)
        self.assertEqual(times(rows), {""})
        self.assertTrue(all(r["last_checked"] == TS for r in rows))  # run time stays in last_checked

    def test_vnappmob_datetime(self):
        payload = {"results": [{"buy_1l": 141000000, "sell_1l": 143000000, "datetime": "1791019200"}]}
        self.assertEqual(times(fp.parse_vnappmob(payload, "sjc", TS)), {"2026-10-03T16:20:00+07:00"})
        payload = {"results": [{"buy_1l": 141000000, "sell_1l": 143000000}]}
        self.assertEqual(times(fp.parse_vnappmob(payload, "sjc", TS)), {""})

    def test_vietcombank_json_and_xml(self):
        payload = {"UpdatedDate": "2026-10-03T08:30:00+07:00",
                   "Data": [{"currencyCode": "USD", "cash": "25760", "transfer": "25790", "sell": "26170"}]}
        self.assertEqual(times(fp.parse_vcb_json(payload, TS)), {"2026-10-03T08:30:00+07:00"})
        self.assertEqual(times(fp.parse_vcb_json({"Data": payload["Data"]}, TS)), {""})
        xml = ('<ExrateList><DateTime>10/3/2026 8:30:00 AM</DateTime>'
               '<Exrate CurrencyCode="USD" Buy="25,760" Transfer="25,790" Sell="26,170"/></ExrateList>')
        self.assertEqual(times(fp.parse_vcb_xml(xml, TS)), {"2026-10-03T08:30:00+07:00"})

    def test_sbv_date_near_label(self):
        rows = fp.parse_central_rate(fixture("sbv_central.html"), "sbv.gov.vn", TS)
        self.assertEqual(times(rows), {"2026-10-03T00:00:00+07:00"})  # "áp dụng cho ngày 03/10/2026"
        rows = fp.parse_central_rate(fixture("sbv_central_table.html"), "sbv.gov.vn", TS)
        self.assertEqual(times(rows), {""})

    def test_vietcap_chart_and_board(self):
        session = fp.parse_ohlc_arrays(fixture("vci_chart.json"))
        rows = fp.stock_session_rows(session, "vietcap.com.vn", TS)
        self.assertEqual(times(rows), {"2026-10-02T00:00:00+07:00"})  # last bar's own timestamp
        board = fixture("vci_board.json")
        rows = fp.stock_foreign_rows(fp.parse_vci_board_foreign(board), "vietcap.com.vn", TS,
                                     close=fp.Decimal(98500))
        self.assertEqual(times(rows), {""})  # fixture states no time
        board[0]["matchPrice"]["receivedTime"] = 1791013502000  # 2026-10-03 14:45:02 +07:00
        rows = fp.stock_foreign_rows(fp.parse_vci_board_foreign(board), "vietcap.com.vn", TS,
                                     close=fp.Decimal(98500))
        self.assertEqual(times(rows), {"2026-10-03T14:45:02+07:00"})

    def test_gold_api_updated_at(self):
        rows = fp.parse_gold_api({"price": 4141.8, "updatedAt": "2026-10-03T08:15:00Z"}, "XAU", TS)
        self.assertEqual(times(rows), {"2026-10-03T15:15:00+07:00"})
        self.assertEqual(times(fp.parse_goldprice_org(
            {"items": [{"curr": "USD", "xauPrice": 1, "xagPrice": 2}]}, TS)), {""})


class CsvV3Tests(unittest.TestCase):
    def test_v2_file_is_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.csv"
            path.write_text(",".join(fp.V2_COLUMNS) + "\n"
                            f"{TS},Vietcombank,fx,cash,USD,25760,26170,VND,1 USD,ok,{TS}\n",
                            encoding="utf-8")
            (row,) = fp.read_csv(path)
            fp.write_csv(path, [row])
            header = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(row["source_updated_at"], "")
        self.assertEqual(list(row), fp.COLUMNS)
        self.assertEqual(header, ",".join(fp.COLUMNS))
        self.assertTrue(header.startswith(",".join(fp.V2_COLUMNS) + ","))  # old 11 columns unchanged

    def test_unchanged_price_refreshes_source_time_only(self):
        first = fp.stamp([fp.make_row(TS, "s", "fx", "cash", "USD", fp.Decimal(1), fp.Decimal(2),
                                      "VND", "1 USD")], "2026-10-03T08:30:00+07:00")
        history, _ = fp.merge([], first)
        later = "2026-10-03T16:17:05+07:00"
        again = fp.stamp([fp.make_row(later, "s", "fx", "cash", "USD", fp.Decimal(1), fp.Decimal(2),
                                      "VND", "1 USD")], "2026-10-03T15:00:00+07:00")
        history, added = fp.merge(history, again)
        self.assertEqual(added, 0)
        self.assertEqual((history[0]["timestamp"], history[0]["last_checked"],
                          history[0]["source_updated_at"]), (TS, later, "2026-10-03T15:00:00+07:00"))


class SourceStaleTests(unittest.TestCase):
    def test_source_stale_values(self):
        row = lambda t: {"source_updated_at": t}  # noqa: E731
        self.assertEqual(fp.source_stale([row("2026-10-03T08:30:00+07:00")], TS), "false")
        self.assertEqual(fp.source_stale([row("2026-10-02T00:00:00+07:00")], TS), "true")
        self.assertEqual(fp.source_stale([row(""), row("")], TS), "")
        self.assertEqual(fp.source_stale([row("2026-10-02T00:00:00+07:00"),
                                          row("2026-10-03T09:00:00+07:00")], TS), "false")  # newest wins

    def test_health_csv_source_stale(self):
        def stamped(t):
            return lambda ts: fp.stamp([fp.make_row(ts, "s", "fx", "cash", f"X{t}", fp.Decimal(1),
                                                    fp.Decimal(2), "VND", "1")], t)
        fetchers = {"today": stamped("2026-10-03T08:30:00+07:00"),
                    "yesterday": stamped("2026-10-02T00:00:00+07:00"),
                    "no time": stamped(""),
                    "down": mock.Mock(side_effect=ValueError("boom"))}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fp, "FETCHERS", fetchers), \
                mock.patch.object(fp, "PRICES_CSV", Path(d) / "p.csv"), \
                mock.patch.object(fp, "LATEST_CSV", Path(d) / "l.csv"), \
                mock.patch.object(fp, "HEALTH_CSV", Path(d) / "h.csv"), \
                mock.patch.object(fp, "now_vn", return_value=TS), \
                mock.patch.object(fp.logging, "basicConfig"), \
                mock.patch.object(fp.log, "error"), \
                contextlib.redirect_stdout(io.StringIO()):
            (Path(d) / "h.csv").write_text("source,status,last_success,last_error,error_msg,source_stale\n"
                                           "down,ok,2026-10-02T10:17:05+07:00,,,false\n", encoding="utf-8")
            fp.main()
            with (Path(d) / "h.csv").open(encoding="utf-8", newline="") as fh:
                health = {r["source"]: r for r in csv.DictReader(fh)}
            with (Path(d) / "l.csv").open(encoding="utf-8", newline="") as fh:
                latest = list(csv.DictReader(fh))
        self.assertEqual({k: v["source_stale"] for k, v in health.items()},
                         {"today": "false", "yesterday": "true", "no time": "", "down": "false"})
        self.assertEqual(health["down"]["status"], "error")  # keeps describing the rows still published
        self.assertEqual(list(latest[0]), fp.COLUMNS)
        self.assertEqual({r["item"]: r["source_updated_at"] for r in latest}["X"], "")


if __name__ == "__main__":
    unittest.main()
