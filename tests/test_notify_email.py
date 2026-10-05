"""Offline tests for the run-report email (no SMTP connection is made)."""

import contextlib
import io
import sys
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
        subject, body, ok = ne.build_message(OK_ENV, HEALTH, NOW)
        self.assertTrue(ok)
        self.assertEqual(subject, "[pnj-price-feed] OK 2026-10-05 17:47 (UTC+7) - 1/3 sources ok")
        self.assertIn("Committed: yes, new price rows: 3", body)
        self.assertIn("blocked_non_vn: SJC (sjc.com.vn)", body)
        self.assertIn("error: PNJ stock", body)
        self.assertIn("https://example.invalid/run/1", body)

    def test_skipped_fetch_is_a_failure(self):
        env = OK_ENV | {"TESTS": "failure", "FETCH": "skipped", "COMMITTED": ""}
        subject, body, ok = ne.build_message(env, HEALTH, NOW)
        self.assertFalse(ok)
        self.assertIn("FAILED", subject)
        self.assertIn("Committed: no", body)

    def run_main(self, env):
        with mock.patch.dict(ne.os.environ, env, clear=True), \
             mock.patch.object(ne, "read_health", return_value=HEALTH), \
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


if __name__ == "__main__":
    unittest.main()
