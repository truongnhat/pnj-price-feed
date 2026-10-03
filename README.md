# pnj-price-feed

A public, machine-readable feed of Vietnamese market prices (domestic gold, international precious metals, VND exchange rates), refreshed hourly by GitHub Actions and published as a CSV file.

It's built so other tools and AI agents can read it straight from the raw GitHub URL. The schema stays the same over time and contains **only publicly available market data**. It holds no private PNJ data and the repository stores no credentials.

## Data sources

| source | category | What it is | Endpoint (public, no auth) |
|---|---|---|---|
| `SJC` | `gold` | SJC domestic gold buy/sell prices, Ho Chi Minh City branch. **Blocks non-Vietnam IPs, so it currently fails on GitHub-hosted runners**; kept in case that changes | `sjc.com.vn/GoldPrice/Services/PriceService.ashx` |
| `vnappmob.com` | `gold` | SJC gold prices (bars, rings, jewelry) through a free public API. A short-lived token is requested on every run and never stored | `api.vnappmob.com/api/v2/gold/sjc` |
| `DOJI` | `gold` | DOJI domestic gold buy/sell prices (bars, rings, jewelry) | `update.giavang.doji.vn/banggia/doji_92411/92411` (XML) |
| `Vietcombank` | `fx` | VND exchange rates (cash and transfer) | `vietcombank.com.vn/api/exchangerates` (falls back to the legacy XML feed) |
| `gold-api.com` / `goldprice.org` / `stooq.com` | `gold`, `silver` | International spot prices, XAU and XAG. Providers are tried in this order and the first that works is used; `source` names that provider | `api.gold-api.com`, `data-asg.goldprice.org`, `stooq.com/q/l/` |

## Files

| File | Content |
|---|---|
| `data/prices.csv` | **Price history.** A new row is added only when a price differs from the last recorded row for the same key. If the price is unchanged, only that row's `last_checked` is updated. |
| `data/latest.csv` | Latest row per key, with the same schema. Use this when you only need current prices. |

## CSV schema

The files are UTF-8 (no BOM), comma-separated, with `\n` line endings and a header row. Fields are quoted only when needed: item names can contain commas, so use a real CSV parser.

| Column | Type | Description | Example |
|---|---|---|---|
| `timestamp` | ISO-8601, `+07:00` | Vietnam time of the run that **first observed** this price | `2026-10-03T10:17:05+07:00` |
| `source` | string | Data provider | `SJC`, `vnappmob.com`, `DOJI`, `Vietcombank`, `gold-api.com`, `goldprice.org`, `stooq.com` |
| `category` | string | Asset class | `gold`, `silver`, `fx` |
| `price_type` | string | Kind of quote | `retail` (SJC), `cash` / `transfer` (FX), `spot` (international) |
| `item` | string | Product or instrument | `Vàng SJC 1L, 10L, 1KG (Hồ Chí Minh)`, `USD`, `XAU` |
| `buy` | decimal or empty | Price the dealer/bank **buys** at (plain number: no thousands separator, `.` as the decimal point) | `119000000` |
| `sell` | decimal or empty | Price the dealer/bank **sells** at | `121000000` |
| `currency` | ISO 4217 | Currency of `buy`/`sell` | `VND`, `USD` |
| `unit` | string | Quantity the price refers to | `luong` (1 lượng = 37.5 g), `1 USD`, `troy_oz` |
| `status` | string | `ok` = buy and sell both present; `partial` = only one of them (e.g. a bank does not buy that currency in cash) | `ok` |
| `last_checked` | ISO-8601, `+07:00` | Vietnam time of the **most recent successful check** of the source that returned this price. Updated on every run where the source responds, even if the price did not change | `2026-10-03T14:17:04+07:00` |

Notes:
- **Key** = (`source`, `category`, `price_type`, `item`). `latest.csv` has exactly one row per key.
- Domestic gold (`category=gold`, `currency=VND`) is always converted to **VND per lượng**, whatever unit the source quotes in (VND or thousand VND, per lượng or per chỉ). Values outside 30M–1B VND/lượng are dropped as implausible.
- For `spot` prices, `buy` and `sell` both hold the same mid price. Because of the provider fallback, `latest.csv` can hold an `XAU` row from more than one provider. Use the row with the newest `last_checked`.
- `timestamp` = when this exact price was **first observed**. It never changes for a given row.
- `last_checked` = when the source **last confirmed** this price. In `prices.csv`, a row covers the interval `[timestamp, last_checked]`. Older rows keep the `last_checked` of the last run before the price changed.
- **Stale or unchanged?** The workflow runs hourly. For a row in `latest.csv`:
  - `last_checked` within roughly the last 2 hours: the source is healthy. If `timestamp` is old, the price simply hasn't changed.
  - `last_checked` older than that: the source was unreachable or failed to parse (or stopped publishing that item), so treat the price as **stale**.
  - Example: `age_hours = (now - last_checked).total_seconds() / 3600; stale = age_hours > 2`.
- New columns will only ever be **appended at the end**. Existing columns will not be renamed or reordered.

### Schema changes (backward compatibility)

| Version | Change |
|---|---|
| v2 | Added `last_checked` as the 11th (last) column. The first 10 columns are unchanged. Readers that select columns by name, or by position 1–10, keep working. A file with the old 10-column header is migrated automatically on the next run, with `last_checked` set to `timestamp`. |
| v1 | Initial 10 columns. |

## Run locally

```bash
python -m pip install -r requirements.txt
python scripts/fetch_prices.py               # updates data/prices.csv and data/latest.csv
python -m unittest discover -s tests         # offline tests (no network)
```

Exit code is `0` if at least one source succeeded and `1` if every source failed. A source that fails is logged and skipped, and the other sources are still written. Each HTTP call has a 30 s timeout and 3 attempts. Writes are atomic (temp file, then rename), so a failed run never leaves a half-written CSV.

## How GitHub Actions works

The workflow is `.github/workflows/update_prices.yml`:

1. **Triggers:** runs hourly at minute 17 UTC (`cron: "17 * * * *"`). You can also run it by hand from **Actions → Update prices → Run workflow** (`workflow_dispatch`).
2. Checks out the repository, sets up Python 3.12 and installs `requirements.txt`.
3. Runs the offline unit tests. If they fail, nothing is fetched or committed.
4. Runs `scripts/fetch_prices.py`.
5. Commits `data/prices.csv` and `data/latest.csv` **only if they changed**, then pushes. If the push is rejected, it rebases and retries. Because `last_checked` moves forward on every successful run, expect one small commit per hourly run. If every source fails, the files are unchanged and nothing is committed; the stale `last_checked` values are themselves the signal.

Permissions and safety:
- The only permission is `contents: write`, granted to the built-in `GITHUB_TOKEN` so the workflow can push. It needs no secrets or API keys.
- `concurrency: update-prices` stops two runs from pushing at the same time.
- If the repository's default branch is protected so that direct pushes are blocked, allow `github-actions[bot]` to push, or change the workflow to open PRs instead.
- GitHub turns off scheduled workflows in public repositories after 60 days with no repository activity. If that happens, re-enable the workflow from the Actions tab.

## Access the raw CSV

```
https://raw.githubusercontent.com/truongnhat/pnj-price-feed/main/data/prices.csv
https://raw.githubusercontent.com/truongnhat/pnj-price-feed/main/data/latest.csv
```

raw.githubusercontent.com caches responses for up to about 5 minutes.

```python
import pandas as pd
url = "https://raw.githubusercontent.com/truongnhat/pnj-price-feed/main/data/latest.csv"
df = pd.read_csv(url, encoding="utf-8", dtype={"buy": "float64", "sell": "float64"})
```

In Power BI, use **Get Data → Web** with the URL above and set the file origin to `65001: Unicode (UTF-8)`.

## Adding a source

Add a parser (a pure function that turns the payload into rows, with fixture tests in `tests/`) and a fetcher in `scripts/fetch_prices.py`, then register it in `FETCHERS`. Use only public endpoints that need no credentials.

## Disclaimer

The prices come from third-party public sources for information only. They may be delayed or wrong. This is not an official PNJ price list.
