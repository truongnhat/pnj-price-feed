"""Offline tests for the run-report email (no SMTP connection is made)."""

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify_email as ne  # noqa: E402

NOW = datetime(2026, 10, 5, 10, 47, tzinfo=timezone.utc)  # 17:47 in Vietnam
HEALTH = [
    {"source": "PNJ gold", "status": "ok", "last_checked": "2026-10-05T17:47:10+07:00"},
    {"source": "SJC (sjc.com.vn)", "status": "blocked_non_vn", "last_checked": "x"},
    {"source": "PNJ stock", "status": "error", "last_checked": "x"},
]
OK_ENV = {"TESTS": "success", "FETCH": "success", "COMMIT": "success", "COMMITTED": "true",
          "NEW_ROWS": "3", "EVENT": "schedule", "RUN_URL": "https://example.invalid/run/1"}


class NotifyEmailTests(unittest.TestCase):
    def test_success_report_is_in_vietnam_time(self):
        subject, body, _, ok = ne.build_message(OK_ENV, HEALTH, NOW)
        self.assertTrue(ok)
        self.assertEqual(subject, "[pnj-price-feed] OK 2026-10-05 17:47 (UTC+7) - 1/3 sources ok")
        self.assertIn("Committed: yes, new price rows: 3", body)
        self.assertIn("blocked_non_vn: SJC (sjc.com.vn)", body)
        self.assertIn("error: PNJ stock", body)
        self.assertIn("https://example.invalid/run/1", body)

    def test_skipped_fetch_is_a_failure(self):
        env = OK_ENV | {"TESTS": "failure", "FETCH": "skipped", "COMMITTED": ""}
        subject, body, _, ok = ne.build_message(env, HEALTH, NOW)
        self.assertFalse(ok)
        self.assertIn("FAILED", subject)
        self.assertIn("Committed: no", body)

    def run_main(self, env):
        with mock.patch.dict(ne.os.environ, env, clear=True), \
             mock.patch.object(ne, "read_health", return_value=HEALTH), \
             mock.patch.object(ne, "read_new_rows", return_value=[]), \
             mock.patch.object(ne.smtplib, "SMTP_SSL") as smtp, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ne.main(), 0)
        return smtp, out.getvalue()

    def test_without_credentials_nothing_is_sent(self):
        smtp, out = self.run_main(OK_ENV)
        smtp.assert_not_called()
        self.assertIn("Email not configured", out)

    def test_sends_with_credentials_and_does_not_log_recipient(self):
        env = OK_ENV | {"MAIL_USERNAME": "bot@example.invalid", "MAIL_PASSWORD": "pw",
                        "MAIL_TO": "me@example.invalid"}
        smtp, out = self.run_main(env)
        sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertEqual(sent["To"], "me@example.invalid")
        self.assertNotIn("me@example.invalid", out)

    def test_failure_mode_skips_successful_runs(self):
        env = OK_ENV | {"MAIL_USERNAME": "u", "MAIL_PASSWORD": "p", "EMAIL_NOTIFY": "failure"}
        smtp, _ = self.run_main(env)
        smtp.assert_not_called()

    def test_new_rows_table(self):
        run = "2026-10-05T17:47:10+07:00"
        header = ("timestamp,source,category,price_type,item,buy,sell,currency,unit,status,"
                  "last_checked,source_updated_at")
        lines = [
            f"2026-10-05T16:17:00+07:00,giavang.org,gold,retail,PNJ Nhẫn,139800000,142800000,"
            f"VND,luong,ok,{run},",
            "2026-10-05T16:17:00+07:00,Vietcombank,fx,cash,EUR,28283.8,29775.61,VND,1 EUR,ok,"
            f"{run},",
            f"{run},giavang.org,gold,retail,PNJ Nhẫn,138800000,142800000,VND,luong,ok,{run},"
            "2026-10-05T17:45:00+07:00",
            f"{run},Vietcombank,fx,cash,EUR,28225.74,29714.48,VND,1 EUR,ok,{run},",
            f"{run},gold-api.com,silver,spot,XAG,59.12,59.12,USD,troy_oz,ok,{run},",
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "prices.csv"
            path.write_text("\n".join([header] + lines) + "\n", encoding="utf-8")
            new = ne.read_new_rows(path, run)
            self.assertEqual(ne.read_new_rows(path, ""), [])
        self.assertEqual([r["item"] for r in new], ["PNJ Nhẫn", "EUR", "XAG"])
        _, text, page, _ = ne.build_message(OK_ENV, HEALTH, NOW, new)
        self.assertIn("New price rows inserted: 3", text)
        rows = ne.table_rows(new)
        self.assertEqual(rows[0][3:8], ("138,800,000", "142,800,000", "-1,000,000", "0",
                                        "VND / luong"))
        self.assertEqual(rows[0][-1], "2026-10-05 17:45")
        self.assertEqual(rows[0][8:11], ("", "", ""))  # v3 file: no VND columns
        xau = {"item": "XAU", "buy": "4000", "sell": "4000", "currency": "USD",
               "unit": "troy_oz", "buy_vn": "120565000", "sell_vn": "120565000",
               "unit_vn": "VND/luong", "_prev": None}
        self.assertEqual(ne.table_rows([xau])[0][8:11],
                         ("120,565,000", "120,565,000", "VND/luong"))
        self.assertIn("Unit VN", text)
        self.assertEqual(rows[1][3:7], ("28,225.74", "29,714.48", "-58.06", "-61.13"))
        self.assertEqual(rows[2][5:7], ("new", "new"))
        self.assertIn("<table", page)
        self.assertIn("color:#cf222e", page)  # falls shown in red
        self.assertIn("PNJ Nhẫn", text)

    def test_no_new_rows_and_cap(self):
        _, text, page, _ = ne.build_message(OK_ENV, HEALTH, NOW, [])
        self.assertIn("No new rows", text)
        self.assertNotIn("<table", page)
        many = [{"item": f"I{i}", "buy": "1", "sell": "1", "_prev": None} for i in range(310)]
        with mock.patch.object(ne, "MAX_TABLE_ROWS", 300):
            _, text, _, _ = ne.build_message(OK_ENV, HEALTH, NOW, many)
        self.assertIn("New price rows inserted: 310 (first 300 shown, 10 more", text)

    def test_email_has_html_alternative(self):
        env = OK_ENV | {"MAIL_USERNAME": "u@example.invalid", "MAIL_PASSWORD": "p"}
        smtp, _ = self.run_main(env)
        sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertEqual([p.get_content_type() for p in sent.iter_parts()],
                         ["text/plain", "text/html"])


if __name__ == "__main__":
    unittest.main()
