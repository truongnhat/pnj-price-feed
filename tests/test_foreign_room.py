"""Foreign ownership (room) rows and their 12-month FOL average.

Run: python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402
import notify_email as ne  # noqa: E402

TS = "2026-11-02T08:17:00+07:00"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def by_item(rows):
    return {r["item"]: (r["buy"], r["sell"], r["unit"]) for r in rows}


def fol_row(day, value, ts=None):
    ts = ts or f"{day}T15:17:00+07:00"
    row = fp.make_row(ts, "vietcap.com.vn", "stock", "foreign_room", "PNJ NN FOL %",
                      fp.Decimal(value), fp.Decimal(value), "", "%")
    row["source_updated_at"] = f"{day}T15:00:00+07:00"
    return row


class RoomRowsTest(unittest.TestCase):
    def test_board_fixture(self):
        board = json.loads((FIXTURES / "vci_board.json").read_text(encoding="utf-8"))
        room = fp.parse_vci_board_room(board)
        self.assertEqual((room["current_room"], room["total_room"]), (1200000, 16000000))
        with self.assertLogs(fp.log, "WARNING"):  # fixture total is not PNJ's real size
            rows = by_item(fp.stock_room_rows(room, "vietcap.com.vn", TS))
        # held 14.8M of a 16M limit; outstanding 16M / 0.49
        self.assertEqual(rows["PNJ NN FOL %"], ("92.5", "92.5", "%"))
        self.assertEqual(rows["PNJ NN sở hữu %"], ("45.33", "45.33", "%"))
        self.assertEqual(rows["PNJ NN room còn lại"], ("1200000", "1200000", "cp"))

    def test_real_size_no_warning_and_26_percent(self):
        total = fp.PNJ_SHARES * fp.FOREIGN_LIMIT  # 250.74M shares
        held = fp.PNJ_SHARES * fp.Decimal("0.26")
        rows = by_item(fp.stock_room_rows(
            {"current_room": total - held, "total_room": total}, "vietcap.com.vn", TS))
        self.assertEqual(rows["PNJ NN sở hữu %"][0], "26")
        self.assertEqual(rows["PNJ NN FOL %"][0], "53.06")

    def test_missing_fields_fail_loudly(self):
        with self.assertRaises(ValueError):
            fp.parse_vci_board_room([{"matchPrice": {"foreignBuyVolume": 1}}])
        with self.assertRaises(ValueError):
            fp.stock_room_rows({"current_room": 5, "total_room": 3}, "vietcap.com.vn", TS)


class RoomAverageTest(unittest.TestCase):
    def test_forward_fill_over_weekdays(self):
        # Mon 2026-10-26 95, Thu 2026-10-29 53 → Mon..Wed 95 (3), Thu, Fri, Mon 02/11 53 (3)
        history = [fol_row("2026-10-26", "95"), fol_row("2026-10-29", "53")]
        rows = by_item(fp.room_average_rows(history, TS))
        self.assertEqual(rows["PNJ NN FOL số phiên tính bình quân"][0], "6")
        self.assertEqual(rows["PNJ NN FOL bình quân 12 tháng %"][0], "74")

    def test_window_is_12_months(self):
        history = [fol_row("2024-01-02", "10"), fol_row("2026-11-02", "50")]
        rows = by_item(fp.room_average_rows(history, TS))
        # the old value is carried into the window start, the 2024 days are left out
        n = int(rows["PNJ NN FOL số phiên tính bình quân"][0])
        self.assertTrue(255 <= n <= 263, n)
        avg = fp.Decimal(rows["PNJ NN FOL bình quân 12 tháng %"][0])
        self.assertTrue(fp.Decimal("10") < avg < fp.Decimal("10.3"), avg)

    def test_no_history_no_rows(self):
        self.assertEqual(fp.room_average_rows([], TS), [])

    def test_email_shows_single_value(self):
        row = fol_row("2026-11-02", "53.06") | {"_prev": None}
        out = ne.table_rows([row])[0]
        self.assertEqual((out[0], out[3], out[4]), ("PNJ NN FOL %", "53.06", ""))

    def test_registered_as_source(self):
        self.assertIn("PNJ foreign room", fp.FETCHERS)


if __name__ == "__main__":
    unittest.main()
