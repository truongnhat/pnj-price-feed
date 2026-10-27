"""VND columns (v4): USD/troy_oz -> VND/luong, price per share -> VND/cp."""

import csv
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402

T1 = "2026-10-27T08:00:00+07:00"
T2 = "2026-10-27T09:00:00+07:00"


def row(ts, source, category, price_type, item, buy, sell, currency, unit):
    return fp.make_row(ts, source, category, price_type, item,
                       Decimal(buy) if buy else None, Decimal(sell) if sell else None,
                       currency, unit)


def rates(ts, sbv="25000", vcb_usd=("25000", "25400"), hkd=("3200", "3300")):
    return [row(ts, "sbv.gov.vn", "fx", "central", "USD", sbv, sbv, "VND", "1 USD"),
            row(ts, "Vietcombank", "fx", "transfer", "USD", *vcb_usd, "VND", "1 USD"),
            row(ts, "Vietcombank", "fx", "transfer", "HKD", *hkd, "VND", "1 HKD")]


class VndColumnsTest(unittest.TestCase):
    def test_columns_appended_after_v3(self):
        self.assertEqual(fp.COLUMNS[:12], fp.V3_COLUMNS)
        self.assertEqual(tuple(fp.COLUMNS[12:]), fp.VN_COLUMNS)

    def test_gold_spot_to_vnd_per_luong(self):
        history = rates(T1) + [row(T1, "gold-api.com", "gold", "spot", "XAU",
                                   "4000", "4000", "USD", "troy_oz")]
        self.assertEqual(fp.add_vn(history), 1)
        xau = history[-1]
        expected = (Decimal(4000) * 25000 * Decimal("37.5") / Decimal("31.1034768")
                    ).quantize(Decimal(1))
        self.assertEqual(xau["buy_vn"], str(expected))  # ~120.565.000 VND/luong
        self.assertEqual(xau["unit_vn"], "VND/luong")
        self.assertEqual(xau["fx_vn"], "25000")

    def test_foreign_share_through_usd(self):
        history = rates(T1) + [row(T1, "finance.yahoo.com", "stock", "peer_session",
                                   "1929.HK Chow Tai Fook đóng cửa", "10", "10", "HKD", "1 cp")]
        fp.add_vn(history)
        ctf = history[-1]
        rate = Decimal(3250) / Decimal(25200) * 25000  # HKD mid / USD mid x SBV
        self.assertEqual(ctf["buy_vn"], str((10 * rate).quantize(Decimal(1))))
        self.assertEqual(ctf["unit_vn"], "VND/cp")

    def test_vnd_share_and_untouched_rows(self):
        history = rates(T1) + [
            row(T1, "vietcap.com.vn", "stock", "session", "PNJ đóng cửa", "17450", "17450",
                "VND", "1 cp"),
            row(T1, "vietcap.com.vn", "stock", "session", "PNJ KL khớp lệnh", "5837200",
                "5837200", "", "cp"),
            row(T1, "giavang.org", "gold", "retail", "PNJ Nhẫn", "137600000", "140600000",
                "VND", "luong")]
        fp.add_vn(history)
        self.assertEqual((history[3]["buy_vn"], history[3]["unit_vn"]), ("17450", "VND/cp"))
        for r in history[4:] + history[:3]:
            self.assertEqual(r["unit_vn"], "")

    def test_uses_rate_as_of_row_time_and_keeps_old_values(self):
        history = rates(T1, sbv="25000") + [row(T1, "gold-api.com", "gold", "spot", "XAU",
                                                "4000", "4000", "USD", "troy_oz")]
        fp.add_vn(history)
        first = history[-1]["buy_vn"]
        history += rates(T2, sbv="26000") + [row(T2, "gold-api.com", "gold", "spot", "XAU",
                                                 "4001", "4001", "USD", "troy_oz")]
        self.assertEqual(fp.add_vn(history), 1)  # only the new row
        self.assertEqual(history[3]["buy_vn"], first)
        self.assertEqual(history[-1]["fx_vn"], "26000")

    def test_no_rate_no_value(self):
        history = [row(T1, "finance.yahoo.com", "stock", "peer_session", "MC.PA LVMH đóng cửa",
                       "385", "385", "EUR", "1 cp")]
        self.assertEqual(fp.add_vn(history), 0)
        self.assertEqual(history[0]["buy_vn"], "")

    def test_v3_file_is_migrated_and_back_filled(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "prices.csv"
            rows = rates(T1) + [row(T1, "gold-api.com", "gold", "spot", "XAU",
                                    "4000", "4000", "USD", "troy_oz")]
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fp.V3_COLUMNS, extrasaction="ignore",
                                   lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
            history = fp.read_csv(path)
            self.assertEqual(list(history[0]), fp.COLUMNS)
            fp.add_vn(history)
            fp.write_csv(path, history)
            header = path.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(header, ",".join(fp.COLUMNS))
            self.assertTrue(header.startswith(",".join(fp.V3_COLUMNS) + ","))
            self.assertEqual(fp.read_csv(path)[-1]["unit_vn"], "VND/luong")


if __name__ == "__main__":
    unittest.main()
