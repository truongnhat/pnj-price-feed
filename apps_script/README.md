# Backup trigger with Google Apps Script

GitHub drops many scheduled runs (since 2026-10-03, only 21 of about 50 expected runs happened). `workflow_dispatch` requests are not dropped, so `trigger.gs` calls the workflow from Google's servers every hour, alongside the workflow's own `cron` schedules (minutes 17 and 47). The workflow's `concurrency` group makes overlapping runs wait their turn, so extra calls are harmless.

The script lives outside `scripts/`, so changing it never triggers a workflow run.

## Setup (5 steps)

1. **Create a GitHub token.** GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token. Repository access: **Only select repositories** → `truongnhat/pnj-price-feed`. Permissions: **Actions: Read and write** (nothing else). Pick an expiry date and put a reminder in your calendar to renew it.
2. **Create the Apps Script project.** Open <https://script.google.com> → **New project**, name it `pnj-price-feed trigger`, then replace the contents of `Code.gs` with [`trigger.gs`](trigger.gs) and save.
3. **Store the token.** Project Settings (gear icon) → **Script Properties** → Add script property: Property `GITHUB_TOKEN`, Value = the token from step 1 → Save. Never paste the token into the code.
4. **Test once.** In the editor, select `dispatchWorkflow` → **Run** → accept the permission prompt (it asks to connect to an external service). The log should say `Dispatched update_prices.yml on main`, and a new run (event `workflow_dispatch`) appears under the repository's **Actions → Update prices**.
5. **Schedule it.** Select `installHourlyTrigger` → **Run**. Check **Triggers** (clock icon): one hourly trigger for `dispatchWorkflow`. Google runs it within about ±15 minutes of minute 17. Failures are emailed to you by Apps Script.

To stop it, run `removeTriggers`. If the token expires or is revoked, the runs fail with HTTP 401 and you get an email: create a new token and replace the `GITHUB_TOKEN` property (step 3).

## Check it works

`data/health.csv` has a `last_checked` column that every run updates, even when no price changed. If it is more than about 2 hours old, runs are being missed.
