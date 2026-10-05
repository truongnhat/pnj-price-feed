# pnj-price-feed

## Tóm tắt (tiếng Việt)

Feed giá thị trường công khai, tự cập nhật **mỗi giờ** (phút 17, dự phòng phút 47; có thể thêm trigger ngoài bằng Google Apps Script, xem [`apps_script/`](apps_script/README.md)) và ngay sau mỗi lần merge code, bằng GitHub Actions và xuất ra file CSV để AI agent, Power BI hay script đọc trực tiếp từ link raw GitHub.

**Nội dung**
- **Giá vàng trong nước:** SJC, PNJ, DOJI, Bảo Tín Minh Châu (`BTMC`), Bảo Tín Mạnh Hải (`BTMH`) và Phú Quý. Lấy vàng miếng và nhẫn 999.9, thêm các dòng "nguyên liệu", "vàng thị trường" và "thương hiệu khác" nếu nguồn có. Giá luôn quy về **VND/lượng**.
- **Tỷ giá:** Vietcombank (tiền mặt, chuyển khoản) và tỷ giá trung tâm USD/VND của NHNN (`price_type=central`).
- **Vàng/bạc thế giới:** XAU, XAG (USD/oz).
- **Cổ phiếu PNJ (HOSE):** ngày phiên, giá mở cửa, cao nhất, thấp nhất, đóng cửa, khối lượng khớp lệnh, khối ngoại mua/bán (`category=stock`). Giá trị khối ngoại chỉ được ghi khi khớp với giá đóng cửa (sai lệch không quá 20%).

**Các file**

| File | Dùng để |
|---|---|
| `data/latest.csv` | Giá mới nhất, mỗi sản phẩm 1 dòng. **Agent nên đọc file này** |
| `data/prices.csv` | Lịch sử: chỉ thêm dòng mới khi giá thay đổi |
| `data/health.csv` | Tình trạng từng nguồn: `ok`, `error` hoặc `blocked_non_vn` (nguồn chặn IP nước ngoài), kèm `source_stale` và `last_checked` (giờ chạy gần nhất, cập nhật mọi lần chạy kể cả khi giá không đổi) |

**Quy tắc đọc dữ liệu**
- Schema 12 cột cố định. `timestamp` là lần đầu thấy mức giá đó; `last_checked` là lần gần nhất nguồn xác nhận giá còn đúng; `source_updated_at` là giờ cập nhật do **chính nguồn công bố**. Nguồn không công bố giờ thì cột này để trống, không bao giờ lấy giờ chạy thay vào.
- `health.csv` có cột `source_stale = true` khi giờ cập nhật của nguồn không thuộc ngày chạy (giờ VN). Cuối tuần hoặc ngày lễ, cổ phiếu và tỷ giá trung tâm báo `true` là bình thường.
- Dòng có `last_checked` cũ hơn 2 giờ là **dữ liệu cũ**: nguồn đang lỗi, xem `health.csv`.
- Mỗi nhóm có nhiều nguồn dự phòng; cột `source` ghi nguồn đã trả dữ liệu. Nếu cùng một sản phẩm có dòng từ nhiều nguồn, lấy dòng có `last_checked` mới nhất.
- **Không bao giờ bịa số.** Nguồn lỗi thì không ghi dòng giá nào, chỉ ghi lỗi vào `health.csv`.

Link: `https://raw.githubusercontent.com/truongnhat/pnj-price-feed/main/data/latest.csv`

---

A public, machine-readable feed of Vietnamese market prices (domestic gold by brand, international precious metals, VND exchange rates, the SBV central rate and PNJ's share price), refreshed hourly by GitHub Actions and published as a CSV file.

It's built so other tools and AI agents can read it straight from the raw GitHub URL. The schema stays the same over time and contains **only publicly available market data**. It holds no private PNJ data (only prices PNJ and others publish on their public sites and public market data), and the repository stores no credentials.

## Data sources

Where several providers are listed, they are tried in order and the **first one that returns data is used**. The `source` column names that provider.

| Group (`health.csv` name) | category / price_type | Providers, in order | Notes |
|---|---|---|---|
| `SJC (sjc.com.vn)` | `gold` / `retail` | `sjc.com.vn` | Blocks non-Vietnam IPs; shows `blocked_non_vn` on GitHub-hosted runners |
| `SJC via vnappmob.com` | `gold` / `retail` | `api.vnappmob.com/api/v2/gold/sjc` | Free public API; a short-lived token is requested on each run and never stored |
| `PNJ gold`, `DOJI gold`, `BTMC gold`, `BTMH gold`, `Phú Quý gold` | `gold` / `retail` | `vnappmob.com` → `giavang.org/trong-nuoc/<brand>/` → brand site (PNJ: `edge-api.pnj.io`, `giavang.pnj.com.vn`; DOJI: XML feed) | Rings and bars (including the brand's own bullion lines such as Kim Bảo, Phúc Lộc Tài, Kim Gia Bảo), plus raw-material / market / other-brand lines. Jewelry, gifts, coins and silver are left out. `item` always starts with the brand: `PNJ …`, `DOJI …`, `BTMC …`, `BTMH …`, `Phú Quý …` |
| `Vietcombank` | `fx` / `cash`, `transfer` | `vietcombank.com.vn` JSON → legacy XML | |
| `SBV central rate` | `fx` / `central` | `sbv.gov.vn` home page (vi, then en) → `tygiausd.org` | USD/VND central rate; `buy` = `sell` = the rate |
| `Metals spot` | `gold`, `silver` / `spot` | `gold-api.com` → `goldprice.org` → `stooq.com` | XAU, XAG in USD per troy oz |
| `PNJ stock` | `stock` / `session`, `session_date` | Vietcap → TCBS → VNDirect → CafeF | Latest HOSE session (see below). During trading hours the latest bar is the session in progress |
| `PNJ foreign trading` | `stock` / `foreign`, `session_date` | Vietcap price board → VNDirect | Foreign investors' buy/sell (see below) |

## Files

| File | Content |
|---|---|
| `data/prices.csv` | **Price history.** A new row is added only when a price differs from the last recorded row for the same key. If the price is unchanged, only that row's `last_checked` is updated. |
| `data/latest.csv` | Latest row per key, with the same schema. Use this when you only need current prices. |
| `data/health.csv` | One row per source (see [health.csv](#healthcsv)). |

## CSV schema

The files are UTF-8 (no BOM), comma-separated, with `\n` line endings and a header row. Fields are quoted only when needed: item names can contain commas, so use a real CSV parser.

| Column | Type | Description | Example |
|---|---|---|---|
| `timestamp` | ISO-8601, `+07:00` | Vietnam time of the run that **first observed** this price | `2026-10-03T10:17:05+07:00` |
| `source` | string | Data provider | `vnappmob.com`, `giavang.org`, `pnj.com.vn`, `Vietcombank`, `sbv.gov.vn`, `gold-api.com`, `tcbs.com.vn`, `cafef.vn`, … |
| `category` | string | Asset class | `gold`, `silver`, `fx`, `stock` |
| `price_type` | string | Kind of quote | `retail` (domestic gold), `cash` / `transfer` / `central` (FX), `spot` (international), `session` / `session_date` / `foreign` (stock) |
| `item` | string | Product or instrument | `SJC 1L, 10L, 1KG`, `PNJ Nhẫn Trơn PNJ 999.9`, `USD`, `XAU`, `PNJ đóng cửa` |
| `buy` | decimal or empty | Price the dealer/bank **buys** at (plain number: no thousands separator, `.` as the decimal point) | `119000000` |
| `sell` | decimal or empty | Price the dealer/bank **sells** at | `121000000` |
| `currency` | ISO 4217 or empty | Currency of `buy`/`sell`; empty for counts and dates | `VND`, `USD`, `` |
| `unit` | string | Quantity the price refers to | `luong` (1 lượng = 37.5 g), `1 USD`, `troy_oz`, `1 cp`, `cp`, `yyyymmdd` |
| `status` | string | `ok` = buy and sell both present; `partial` = only one of them (e.g. a bank does not buy that currency in cash) | `ok` |
| `last_checked` | ISO-8601, `+07:00` | Vietnam time of the **most recent successful check** of the source that returned this price. Updated on every run where the source responds, even if the price did not change | `2026-10-03T14:17:04+07:00` |
| `source_updated_at` | ISO-8601, `+07:00`, or empty | The update time **stated by the source itself**, for the latest successful check. **Empty when the source states none: the run time is never substituted.** A source that only gives a date (e.g. the SBV central rate "áp dụng cho ngày …") gives 00:00 of that date | `2026-10-03T15:20:00+07:00` |

Notes:
- **Key** = (`source`, `category`, `price_type`, `item`). `latest.csv` has exactly one row per key.
- Domestic gold (`category=gold`, `currency=VND`) is always converted to **VND per lượng**, whatever unit the source quotes in (VND or thousand VND, per lượng or per chỉ). Values are rounded to whole VND, and values outside 30M–1B VND/lượng are dropped as implausible.
- For `spot` prices, `buy` and `sell` both hold the same mid price. Because of the provider fallback, `latest.csv` can hold an `XAU` row from more than one provider. Use the row with the newest `last_checked`.
- `timestamp` = when this exact price was **first observed**. It never changes for a given row.
- `last_checked` = when the source **last confirmed** this price. In `prices.csv`, a row covers the interval `[timestamp, last_checked]`. Older rows keep the `last_checked` of the last run before the price changed.
- **Stale or unchanged?** The workflow runs hourly. For a row in `latest.csv`:
  - `last_checked` within roughly the last 2 hours: the source is healthy. If `timestamp` is old, the price simply hasn't changed.
  - `last_checked` older than that: the source was unreachable or failed to parse (or stopped publishing that item), so treat the price as **stale**.
  - Example: `age_hours = (now - last_checked).total_seconds() / 3600; stale = age_hours > 2`.
- Where `source_updated_at` comes from:

  | Provider | Field read |
  |---|---|
  | `giavang.org` | The brand page's "Cập nhật lúc …" line |
  | `vnappmob.com` | `results[0].datetime` (epoch) |
  | `Vietcombank` | JSON `UpdatedDate`; legacy XML `<DateTime>` (month/day/year) |
  | `sbv.gov.vn` (and mirrors) | Date (and time, if shown) printed near the "Tỷ giá trung tâm" label |
  | `vietcap.com.vn` | Session rows: the latest bar's timestamp (the session date). Foreign rows: the board's newest time field (e.g. `receivedTime`), if present |
  | `gold-api.com` | `updatedAt` |
  | Other providers | Empty |

  Like `last_checked`, it moves forward on an unchanged price and is not part of the change check, so it never creates a new history row by itself.
- `buy`/`sell` hold a number or are empty. Nothing is ever estimated or filled in: a source that fails writes no rows.
- New columns will only ever be **appended at the end**. Existing columns will not be renamed or reordered.

### PNJ stock rows (`category=stock`)

The schema has no date column, so the session date is a row of its own.

| item | price_type | buy | sell | currency | unit |
|---|---|---|---|---|---|
| `PNJ ngày phiên` | `session_date` | session date as `yyyymmdd`, e.g. `20261002` | same | empty | `yyyymmdd` |
| `PNJ mở cửa`, `PNJ cao nhất`, `PNJ thấp nhất`, `PNJ đóng cửa` | `session` | price | same | `VND` | `1 cp` (per share) |
| `PNJ KL khớp lệnh` | `session` | matched volume | same | empty | `cp` (shares) |
| `PNJ NN ngày phiên` | `session_date` | foreign-trading session date `yyyymmdd` | same | empty | `yyyymmdd` |
| `PNJ NN KL mua/bán` | `foreign` | foreign **buy** volume | foreign **sell** volume | empty | `cp` |
| `PNJ NN GT mua/bán` | `foreign` | foreign **buy** value | foreign **sell** value | `VND` | `VND` |

Prices quoted in thousand VND are converted to VND per share.

**Foreign trading values are cross-checked.** Value ÷ volume is what foreign investors paid per share, so after converting the provider's unit (VND, thousand, million or billion VND) it must be within 20% of the same run's `PNJ đóng cửa`. If no close was fetched in the run, or no unit fits, the `PNJ NN GT mua/bán` row is left out and the run logs the raw values. The volume row is still written. `PNJ NN ngày phiên` is written only when the provider states the session date.

### health.csv

| Column | Description |
|---|---|
| `source` | Group name, as in the sources table |
| `status` | Result of the latest run: `ok`; `error` (failed, see `error_msg`); `blocked_non_vn` (every provider answered HTTP 403/451, which normally means it refuses non-Vietnam IPs such as GitHub's runners) |
| `last_success` | Vietnam time of the last run where the source returned data |
| `last_error` | Vietnam time of the last failed run |
| `error_msg` | Message from that last failure, listing each provider tried (it can be older than `last_success`) |
| `source_stale` | `true` if the newest `source_updated_at` among the source's rows is not on the run's date (Vietnam time); `false` if it is; empty if the source states no time. Set on successful runs; after a failure it keeps describing the rows still in `latest.csv`. Expect `true` for the stock and the central rate on weekends and holidays |
| `last_checked` | Vietnam time of the latest run, stamped for every source on every run whatever its status, even when no price changed. A heartbeat: if it is more than about 2 hours old, workflow runs are being missed |

`blocked_non_vn` sources don't create GitHub Actions warnings; other errors do. A host that doesn't accept a connection (connect timeout, typical of firewalls that drop foreign traffic) is skipped for the rest of the run instead of being retried.

### Data corrections

Rows known to be wrong are dropped when the CSV is read (`DATA_CORRECTIONS` in `scripts/fetch_prices.py`), so they vanish from `prices.csv` and `latest.csv` on the next run. So far that covers the 2026-10-03 15:20 CafeF foreign-trading rows: the value unit was misread, and the volumes (22,400 / 67,800) did not match the exchange (foreign buy 4,200,700 shares, checked on a broker app). CafeF is no longer used for foreign trading. Gold rows that no longer pass the product filter are dropped the same way. Moving the workflow to a self-hosted runner in Vietnam would unblock them.

### Schema changes (backward compatibility)

| Version | Change |
|---|---|
| v3.1 | `health.csv` gained a last column, `last_checked`. `prices.csv` and `latest.csv` are unchanged. An older `health.csv` is upgraded on the next run. |
| v3 | Added `source_updated_at` as the 12th (last) column. The first 11 columns are unchanged, in the same order and with the same meaning. A file with the v2 (11-column) or v1 (10-column) header is migrated automatically on the next run, with `source_updated_at` left empty for existing rows. `health.csv` gained a last column, `source_stale`. |
| v2 | Added `last_checked` as the 11th (last) column. The first 10 columns are unchanged. Readers that select columns by name, or by position 1–10, keep working. A file with the old 10-column header is migrated automatically on the next run, with `last_checked` set to `timestamp`. |
| v1 | Initial 10 columns. |

## Run locally

```bash
python -m pip install -r requirements.txt
python scripts/fetch_prices.py               # updates data/prices.csv, latest.csv, health.csv
python -m unittest discover -s tests         # offline tests (no network)
```

Exit code is `0` if at least one source succeeded and `1` if every source failed. A source that fails is logged and skipped, and the other sources are still written. Each HTTP call has a 10 s connect and 25 s read timeout and up to 3 attempts. 4xx responses and certificate errors are not retried. Writes are atomic (temp file, then rename), so a failed run never leaves a half-written CSV.

## How GitHub Actions works

The workflow is `.github/workflows/update_prices.yml`:

1. **Triggers:** runs hourly at minute 17 UTC (`cron: "17 * * * *"`) with a backup slot at minute 47 (`cron: "47 * * * *"`), because GitHub drops many scheduled runs under load. It also runs right after a change to `scripts/`, `tests/`, `requirements.txt` or the workflow is merged into `main`, so fixes show up in `data/` immediately. You can also run it by hand from **Actions → Update prices → Run workflow** (`workflow_dispatch`), or from an external scheduler: [`apps_script/`](apps_script/README.md) has a Google Apps Script that calls `workflow_dispatch` every hour. The bot's own data commits don't trigger it.
2. Checks out the repository, sets up Python 3.12 and installs `requirements.txt`.
3. Runs the offline unit tests. If they fail, nothing is fetched or committed.
4. Runs `scripts/fetch_prices.py`.
5. Commits `data/prices.csv`, `data/latest.csv` and `data/health.csv` **only if they changed**, then pushes. If the push is rejected, it rebases and retries. Because `health.csv`'s `last_checked` is stamped on every run, even when no price changed or every source failed, each run makes one small commit. If every source fails, the prices' `last_checked` values stop moving; that is the signal. If `health.csv`'s `last_checked` is more than about 2 hours old, runs are being missed.
6. Emails a short run report (result, time in UTC+7, sources ok/failed, link to the run) after **every** run, even a failed one. See [Email report](#email-report).

Permissions and safety:
- The only permission is `contents: write`, granted to the built-in `GITHUB_TOKEN` so the workflow can push. Fetching prices needs no secrets or API keys; only the optional email report below uses GitHub secrets.
- `concurrency: update-prices` stops two runs from pushing at the same time.
- If the repository's default branch is protected so that direct pushes are blocked, allow `github-actions[bot]` to push, or change the workflow to open PRs instead.
- GitHub turns off scheduled workflows in public repositories after 60 days with no repository activity. If that happens, re-enable the workflow from the Actions tab.

### Email report

The last step (`scripts/notify_email.py`) emails a plain-text report after each run, in Vietnam time (UTC+7). Subject example: `[pnj-price-feed] OK 2026-10-05 17:47 (UTC+7) - 11/12 sources ok`. Setup with Gmail:

1. Turn on 2-Step Verification for the Google account, then create an **App Password** at <https://myaccount.google.com/apppasswords>.
2. In the repository: **Settings → Secrets and variables → Actions → New repository secret**, add `MAIL_USERNAME` (the Gmail address), `MAIL_PASSWORD` (the App Password) and `MAIL_TO` (recipient; defaults to `MAIL_USERNAME`). The address is kept in secrets, not in the workflow file, so it stays out of this public repository and its logs.
3. Optional: under **Variables**, set `EMAIL_NOTIFY` to `failure` (only failed runs) or `off`. The default is `always`.

Without the secrets the report is only printed in the run log. A failed send shows as a warning and never fails the run. Expect up to about 3 emails an hour (cron at minutes 17 and 47, plus the Apps Script trigger); a Gmail filter on `[pnj-price-feed] OK` keeps them out of the inbox.

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

Add a parser (a pure function that turns the payload into rows, with a fixture in `tests/fixtures/` and a test) and a fetcher in `scripts/fetch_prices.py`, then register it in `FETCHERS`. For several providers of the same data, wrap them with `first_working()`. Use only public endpoints that need no credentials. The fixtures are hand-written samples of each provider's format; replace one with a captured response when you fix a parser after a live run.

## Disclaimer

The prices come from third-party public sources for information only. They may be delayed or wrong. This is not an official PNJ price list.
