#!/usr/bin/env python3
"""Email a short report after each "Update prices" run (called by the workflow).

Settings come from environment variables; nothing is stored in the repository.
  MAIL_USERNAME, MAIL_PASSWORD : SMTP login (for Gmail: the address and an App Password)
  MAIL_TO                      : recipient(s), comma-separated; defaults to MAIL_USERNAME
  MAIL_SMTP_HOST, MAIL_SMTP_PORT : default smtp.gmail.com, 465 (SSL)
  EMAIL_NOTIFY                 : "always" (default), "failure" or "off"
  RUN_URL, EVENT, TESTS, FETCH, COMMIT, COMMITTED, NEW_ROWS : set by the workflow

Without credentials the report is only printed, so the workflow never fails
because email is not configured. A failed send is a warning, not an error:
the data run itself succeeded or failed on its own.
"""

from __future__ import annotations

import csv
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

VN_TZ = timezone(timedelta(hours=7))
HEALTH_CSV = Path(__file__).resolve().parent.parent / "data" / "health.csv"


def read_health(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def build_message(env: dict, health: list[dict], now: datetime) -> tuple[str, str, bool]:
    """Return (subject, body, ok). ok is False when any step did not succeed."""
    steps = {"Tests": env.get("TESTS", ""), "Fetch": env.get("FETCH", ""),
             "Commit": env.get("COMMIT", "")}
    ok = all(v == "success" for v in steps.values())
    when = now.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
    by_status: dict[str, list[str]] = {}
    for row in health:
        by_status.setdefault(row.get("status", ""), []).append(row["source"])
    n_ok = len(by_status.get("ok", []))

    subject = (f"[pnj-price-feed] {'OK' if ok else 'FAILED'} {when} (UTC+7) "
               f"- {n_ok}/{len(health)} sources ok")
    lines = [
        f"Run: {when} (UTC+7), trigger: {env.get('EVENT', '?')}",
        f"Result: {'success' if ok else 'FAILED'}",
        "",
        "Steps: " + ", ".join(f"{k} {v or 'skipped'}" for k, v in steps.items()),
        f"Committed: {'yes' if env.get('COMMITTED') == 'true' else 'no'}, "
        f"new price rows: {env.get('NEW_ROWS') or 0}",
        "",
        f"Sources ok: {n_ok}/{len(health)}",
    ]
    for status in sorted(s for s in by_status if s != "ok"):
        lines.append(f"  {status}: {', '.join(by_status[status])}")
    if health:
        lines.append(f"health.csv last_checked: {health[0].get('last_checked') or '-'}")
    lines += ["", f"Details: {env.get('RUN_URL', '')}"]
    return subject, "\n".join(lines) + "\n", ok


def main() -> int:
    env = dict(os.environ)
    mode = env.get("EMAIL_NOTIFY", "").strip().lower() or "always"
    subject, body, ok = build_message(env, read_health(HEALTH_CSV), datetime.now(timezone.utc))
    print(subject)
    print(body)
    if mode == "off" or (mode == "failure" and ok):
        print(f"EMAIL_NOTIFY={mode}: no email sent.")
        return 0
    user, password = env.get("MAIL_USERNAME", ""), env.get("MAIL_PASSWORD", "")
    if not user or not password:
        print("::notice title=Email not configured::Set the MAIL_USERNAME and MAIL_PASSWORD "
              "secrets to receive run reports by email.")
        return 0

    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = env.get("MAIL_TO", "").strip() or user
    msg.set_content(body)
    host = env.get("MAIL_SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(env.get("MAIL_SMTP_PORT", "").strip() or 465)
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"::warning title=Email not sent::{type(exc).__name__}: {exc}")
        return 0
    print("Email sent.")  # recipient not logged: run logs of a public repo are public
    return 0


if __name__ == "__main__":
    sys.exit(main())
