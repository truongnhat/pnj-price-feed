#!/usr/bin/env python3
"""Email a short report after each "Update prices" run (called by the workflow).

Settings come from environment variables; nothing is stored in the repository.
  MAIL_USERNAME, MAIL_PASSWORD : SMTP login (for Gmail: the address and an App Password)
  MAIL_TO                      : recipient(s), comma-separated; defaults to MAIL_USERNAME
  MAIL_SMTP_HOST, MAIL_SMTP_PORT : default smtp.gmail.com, 465 (SSL)
  EMAIL_NOTIFY                 : "always" (default), "failure" or "off"
  RUN_URL, EVENT, TESTS, FETCH, COMMIT, COMMITTED, NEW_ROWS : set by the workflow

The email lists the price rows this run inserted into data/prices.csv as a
table (HTML, with a plain-text fallback), with the change from the previous
price of the same item. Those are the rows whose timestamp is this run's time,
which the run also stamps into health.csv's last_checked.

Without credentials the report is only printed, so the workflow never fails
because email is not configured. A failed send is a warning, not an error:
the data run itself succeeded or failed on its own.
"""

from __future__ import annotations

import csv
import html
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path

VN_TZ = timezone(timedelta(hours=7))
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HEALTH_CSV = DATA_DIR / "health.csv"
PRICES_CSV = DATA_DIR / "prices.csv"
KEY_COLUMNS = ("source", "category", "price_type", "item")  # same as fetch_prices.py
MAX_TABLE_ROWS = 300  # keeps the email readable (and under Gmail's clipping size)
# Columns of prices.csv deliberately NOT shown in the email table, with the reason.
# Every other column must appear in table_rows(); tests/test_schema_contract.py
# fails when a new column is added to fetch_prices.COLUMNS and forgotten here.
EMAIL_HIDDEN_COLUMNS = {
    "timestamp": "same for every row of a run; the run time is in the summary",
    "status": "ok/partial is visible from empty Buy or Sell cells",
    "last_checked": "equals the run time for every row in the table",
    "fx_vn": "the rate used for Buy VND / Sell VND; kept in the CSV for audit",
}
TABLE_HEADERS = ("Item", "Source", "Type", "Buy", "Sell", "Δ Buy", "Δ Sell", "Unit",
                 "Buy VND", "Sell VND", "Unit VN",
                 "Source time")


def read_health(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_new_rows(path: Path, run_ts: str) -> list[dict]:
    """Rows first recorded by the run at run_ts, each with the previous row of
    the same item under "_prev" (None for an item seen for the first time)."""
    if not run_ts or not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        history = list(csv.DictReader(fh))
    latest: dict[tuple, dict] = {}
    new = []
    for row in history:
        key = tuple(row.get(c, "") for c in KEY_COLUMNS)
        if row.get("timestamp") == run_ts:
            new.append(row | {"_prev": latest.get(key)})
        latest[key] = row
    return new


def _num(value: str | None) -> Decimal | None:
    try:
        return Decimal(value) if value else None
    except InvalidOperation:
        return None


def _fmt(number: Decimal | None, sign: bool = False) -> str:
    if number is None:
        return ""
    # Integers as is; decimals with 2 to 4 places, as precise as the source.
    exponent = number.normalize().as_tuple().exponent
    places = 0 if exponent >= 0 else min(4, max(2, -exponent))
    text = f"{number:{'+' if sign else ''},.{places}f}"
    return "0" if sign and number == 0 else text


# Quotes that have a single value (spot mid, central rate, a share price or
# volume, a date): the CSV repeats it in buy and sell, the email shows it once,
# under Buy, and leaves the Sell columns empty.
SINGLE_VALUE_TYPES = {"spot", "central", "session", "session_date", "peer_session",
                      "peer_session_date"}


def table_rows(new_rows: list[dict]) -> list[tuple[str, ...]]:
    out = []
    for row in new_rows:
        single = (row.get("price_type") in SINGLE_VALUE_TYPES
                  and row.get("buy", "") == row.get("sell", ""))
        prev = row.get("_prev")
        deltas = []
        for col in ("buy", "sell"):
            now_v, prev_v = _num(row.get(col)), _num(prev.get(col)) if prev else None
            if prev is None:
                deltas.append("new")
            elif now_v is None or prev_v is None:
                deltas.append("")
            else:
                deltas.append(_fmt(now_v - prev_v, sign=True))
        source_time = (row.get("source_updated_at") or "").replace("T", " ")[:16]
        sell, sell_vn = _fmt(_num(row.get("sell"))), _fmt(_num(row.get("sell_vn")))
        if single:
            sell, deltas[1], sell_vn = "", "", ""
        out.append((row.get("item", ""), row.get("source", ""),
                    f"{row.get('category', '')}/{row.get('price_type', '')}",
                    _fmt(_num(row.get("buy"))), sell, *deltas,
                    f"{row.get('currency', '')} / {row.get('unit', '')}",
                    _fmt(_num(row.get("buy_vn"))), sell_vn,
                    row.get("unit_vn", ""), source_time))
    return out


def text_table(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(TABLE_HEADERS)]
    numeric = {3, 4, 5, 6, 8, 9}
    def line(cells):
        return " | ".join(c.rjust(w) if i in numeric else c.ljust(w)
                          for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    return "\n".join([line(TABLE_HEADERS), "-+-".join("-" * w for w in widths)]
                     + [line(r) for r in rows])


def html_table(rows: list[tuple[str, ...]]) -> str:
    cell = "border:1px solid #ccc;padding:3px 6px;"
    head = "".join(f'<th style="{cell}background:#f2f2f2;text-align:left">{html.escape(h)}</th>'
                   for h in TABLE_HEADERS)
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            style = cell + ("text-align:right;" if i in (3, 4, 5, 6, 8, 9) else "")
            if i in (5, 6) and c.startswith("+"):
                style += "color:#1a7f37;"
            elif i in (5, 6) and c.startswith("-"):
                style += "color:#cf222e;"
            tds.append(f'<td style="{style}">{html.escape(c)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return ('<table style="border-collapse:collapse;font:13px Arial,sans-serif">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>")


def build_message(env: dict, health: list[dict], now: datetime,
                  new_rows: list[dict] | None = None) -> tuple[str, str, str, bool]:
    """Return (subject, text body, HTML body, ok). ok is False when any step did
    not succeed."""
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
    run_url = env.get("RUN_URL", "")
    summary = "\n".join(lines)

    new_rows = new_rows or []
    rows = table_rows(new_rows[:MAX_TABLE_ROWS])
    more = len(new_rows) - len(rows)
    title = f"New price rows inserted: {len(new_rows)}"
    note = f"(first {len(rows)} shown, {more} more in data/prices.csv)" if more else ""
    text = [summary, "", title + (f" {note}" if note else "")]
    page = [f'<pre style="font:13px Consolas,monospace">{html.escape(summary)}</pre>',
            f'<h3 style="font:bold 14px Arial,sans-serif">{html.escape(title)}</h3>']
    if rows:
        text += [text_table(rows)]
        page += [html_table(rows)]
        if note:
            page += [f'<p style="font:12px Arial,sans-serif">{html.escape(note)}</p>']
    else:
        text += ["No new rows: every price was unchanged or no source was fetched."]
        page += ['<p style="font:13px Arial,sans-serif">No new rows: every price was '
                 "unchanged or no source was fetched.</p>"]
    text += ["", f"Details: {run_url}"]
    page += [f'<p style="font:13px Arial,sans-serif">Details: '
             f'<a href="{html.escape(run_url)}">{html.escape(run_url)}</a></p>']
    return subject, "\n".join(text) + "\n", "\n".join(page), ok


def main() -> int:
    env = dict(os.environ)
    mode = env.get("EMAIL_NOTIFY", "").strip().lower() or "always"
    health = read_health(HEALTH_CSV)
    # Only a run whose fetch step ran stamped last_checked; otherwise health.csv
    # still holds an earlier run's time and its rows are not this run's.
    run_ts = health[0].get("last_checked", "") if health else ""
    new_rows = (read_new_rows(PRICES_CSV, run_ts)
                if env.get("FETCH") in ("success", "failure") else [])
    subject, body, html_body, ok = build_message(env, health, datetime.now(timezone.utc),
                                                 new_rows)
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
    msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype="html")
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
