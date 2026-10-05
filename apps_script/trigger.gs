/**
 * External trigger for the "Update prices" GitHub Actions workflow.
 *
 * GitHub drops many scheduled (cron) runs under load; workflow_dispatch calls
 * are not dropped. This Apps Script calls workflow_dispatch every hour as a
 * backup to the workflow's own cron schedule.
 *
 * Setup: see apps_script/README.md. The token is read from Script Properties
 * (key GITHUB_TOKEN) and is never written in this file.
 */

var OWNER = 'truongnhat';
var REPO = 'pnj-price-feed';
var WORKFLOW_FILE = 'update_prices.yml';
var REF = 'main';
var TRIGGER_MINUTE = 17;
var HANDLER = 'dispatchWorkflow';

/** Calls POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches. */
function dispatchWorkflow() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('Script Property GITHUB_TOKEN is not set (Project Settings > Script Properties).');
  }
  var url = 'https://api.github.com/repos/' + OWNER + '/' + REPO +
      '/actions/workflows/' + WORKFLOW_FILE + '/dispatches';
  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ ref: REF }),
    muteHttpExceptions: true
  };

  // GitHub answers 204 No Content on success. Retry only server errors.
  for (var attempt = 1; attempt <= 3; attempt++) {
    var resp = UrlFetchApp.fetch(url, options);
    var code = resp.getResponseCode();
    if (code === 204) {
      console.log('Dispatched ' + WORKFLOW_FILE + ' on ' + REF);
      return;
    }
    if (code < 500 || attempt === 3) {
      // Throwing makes Apps Script record the failure and email the owner.
      throw new Error('Dispatch failed: HTTP ' + code + ' ' + resp.getContentText().slice(0, 300));
    }
    Utilities.sleep(attempt * 5000);
  }
}

/**
 * Creates the hourly trigger near minute 17. Run it once by hand.
 * Safe to re-run: it removes this script's previous dispatch triggers first.
 * Apps Script runs hourly triggers within about +/-15 minutes of nearMinute.
 */
function installHourlyTrigger() {
  removeTriggers();
  ScriptApp.newTrigger(HANDLER)
      .timeBased()
      .everyHours(1)
      .nearMinute(TRIGGER_MINUTE)
      .create();
  console.log('Installed hourly trigger for ' + HANDLER + ' near minute ' + TRIGGER_MINUTE);
}

/** Removes every trigger that calls dispatchWorkflow. */
function removeTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === HANDLER) {
      ScriptApp.deleteTrigger(t);
    }
  });
}
