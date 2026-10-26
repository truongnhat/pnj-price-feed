"""Offline tests for the jewellery peer stocks (Yahoo Finance chart API).

Run: python -m unittest discover -s tests
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402

TS = "2026-10-26T08:17:05+07:00"
FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "yahoo_chart_1929.json")
                     .read_text(encoding="utf-8"))


def by_item(rows):
    return {r["item"]: r for r in rows}


class YahooChartTests(unittest.TestCase):
    def test_parses_last_bar_previous_close_volume_and_date(self):
        rows = by_item(fp.parse_yahoo_chart(FIXTURE, "1929.HK", "Chow Tai Fook", TS))
        close = rows["1929.HK Chow Tai Fook đóng cửa"]
        self.assertEqual((close["buy"], close["sell"], close["currency"]), ("12.48", "12.48", "HKD"))
        self.assertEqual(rows["1929.HK Chow Tai Fook đóng cửa phiên trước"]["sell"], "12.62")
        self.assertEqual(rows["1929.HK Chow Tai Fook khối lượng"]["sell"], "14550000")
        # 1792720800 = 2026-10-23 02:00 UTC -> 10:00 Hong Kong
        self.assertEqual(rows["1929.HK Chow Tai Fook ngày phiên"]["sell"], "20261023")
        self.assertTrue(close["source_updated_at"].startswith("2026-10-23T"))
        self.assertEqual({r["source"] for r in rows.values()}, {"finance.yahoo.com"})

    def test_skips_null_closes(self):
        payload = json.loads(json.dumps(FIXTURE))
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        quote["close"][-1] = None
        rows = by_item(fp.parse_yahoo_chart(payload, "1929.HK", "Chow Tai Fook", TS))
        self.assertEqual(rows["1929.HK Chow Tai Fook đóng cửa"]["sell"], "12.62")
        self.assertEqual(rows["1929.HK Chow Tai Fook ngày phiên"]["sell"], "20261022")

    def test_error_payload_raises(self):
        with self.assertRaises(ValueError):
            fp.parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found"}}},
                                 "XXXX.HK", "x", TS)

    def test_one_failing_peer_does_not_stop_the_others(self):
        good = mock.Mock()
        good.json.return_value = FIXTURE

        def fake_http(method, url, **kwargs):
            if "0590.HK" in url:
                raise RuntimeError("boom")
            return good
        with mock.patch.object(fp, "http", side_effect=fake_http):
            rows = fp.fetch_peer_stocks(TS)
        items = {r["item"] for r in rows}
        self.assertNotIn("0590.HK Luk Fook đóng cửa", items)
        self.assertEqual(len([i for i in items if i.endswith(" đóng cửa")]), len(fp.PEER_STOCKS) - 1)

    def test_all_blocked_raises_blocked(self):
        def blocked(*a, **k):
            raise fp.BlockedError("HTTP 403")
        with mock.patch.object(fp, "http", side_effect=blocked):
            with self.assertRaises(fp.BlockedError):
                fp.fetch_peer_stocks(TS)

    def test_registered_in_fetchers(self):
        self.assertIn("Peer stocks (Yahoo)", fp.FETCHERS)


if __name__ == "__main__":
    unittest.main()
