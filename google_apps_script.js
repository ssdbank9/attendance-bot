/**
 * AKU Attendance Bot - Google Apps Script (LAST-RESORT FALLBACK)
 *
 * WHY THIS WAS REWRITTEN
 * The previous version randomized with Utilities.sleep(30-40 min). Apps Script
 * caps a single execution at 6 minutes (consumer accounts), so that call always
 * threw "Exceeded maximum execution time" and the attendance POST never ran.
 * Randomization here is done with a self-scheduling ONE-TIME trigger instead:
 * a scheduler function fires on the hour, picks a random instant later in the
 * window, and installs a one-shot trigger for the actual POST. Nothing sleeps.
 *
 * SETUP
 * 1. script.google.com -> your project.
 * 2. Project Settings (gear) -> Time zone -> "(GMT+05:00) Karachi".
 * 3. Project Settings -> Script Properties -> add:
 *      USER_ID     your portal user id
 *      PASSWORD    your portal password
 *      SYNC_TOKEN  any long random string (only if you use the doPost sync)
 *      GH_TOKEN    optional; only if the attendance repo becomes private
 * 4. Triggers (clock icon) -> delete EVERY existing trigger, then add exactly:
 *      scheduleTimeIn   Time-driven, Day timer, 10am to 11am
 *      scheduleTimeOut  Time-driven, Day timer, 10pm to 11pm
 *    Do NOT put triggers on runTimeIn / runTimeOut - those are installed
 *    automatically and removed after each run.
 *
 * HIERARCHY - this script is the LAST resort:
 *   1. Local desktop bot    08:45-09:05 / 20:00-21:30
 *   2. GitHub Actions       09:05-09:15 / 21:30-21:40
 *   3. This script          10:00-10:55 / 22:00-22:55  <- always after both
 *
 * Unlike the old version, this one honours holidays.json, blackout.json
 * (leave/sick days) and bot_config.json (paused) pulled from the attendance
 * repo, so it can no longer mark you present on a holiday or while on leave.
 */

var API_URL = "https://portalservice.aku.edu/Service1.svc/json/TimeInTimeOut/";
var RAW_BASE = "https://raw.githubusercontent.com/ssdbank9/attendance-bot/main/";
var TZ = "Asia/Karachi";

// Randomized firing windows, local (Karachi) time. Keep these AFTER the local
// bot and the GitHub Actions fallback, or this script stops being last-resort.
var WINDOWS = {
  timein:  { startHour: 10, startMin: 0, spanMin: 55 },
  timeout: { startHour: 22, startMin: 0, spanMin: 55 }
};

// Matches the desktop bot: most days land early in the window, some late.
var PRIMARY_WEIGHT = 0.85;
var PRIMARY_FRACTION = 0.75;

// ---------------------------------------------------------------- credentials

function getCredentials() {
  var p = PropertiesService.getScriptProperties();
  var id = p.getProperty("USER_ID");
  var pw = p.getProperty("PASSWORD");
  if (!id || !pw) {
    throw new Error("USER_ID / PASSWORD not set. Add them in Project Settings -> Script Properties.");
  }
  return { USER_ID: id, PASSWORD: pw };
}

/** Web-app hook for dashboard credential sync. Requires a matching SYNC_TOKEN:
 *  without it, anyone who learns the deployment URL could overwrite the stored
 *  credentials, since the web app must be deployed with "Access: Anyone". */
function doPost(e) {
  var out = function (o) {
    return ContentService.createTextOutput(JSON.stringify(o))
      .setMimeType(ContentService.MimeType.JSON);
  };
  try {
    var props = PropertiesService.getScriptProperties();
    var expected = props.getProperty("SYNC_TOKEN");
    if (!expected) return out({ status: "error", message: "SYNC_TOKEN not configured" });

    var data = JSON.parse(e.postData.contents);
    if (data.token !== expected) return out({ status: "error", message: "unauthorized" });

    var updated = [];
    if (data.user_id) { props.setProperty("USER_ID", data.user_id); updated.push("USER_ID"); }
    if (data.password) { props.setProperty("PASSWORD", data.password); updated.push("PASSWORD"); }
    return out({ status: "ok", message: "Updated: " + (updated.join(", ") || "nothing") });
  } catch (err) {
    return out({ status: "error", message: err.message });
  }
}

// ------------------------------------------------------------- skip decisions

function fetchJson(name) {
  var opts = { muteHttpExceptions: true };
  var token = PropertiesService.getScriptProperties().getProperty("GH_TOKEN");
  if (token) opts.headers = { Authorization: "token " + token };
  var res = UrlFetchApp.fetch(RAW_BASE + name + "?cb=" + new Date().getTime(), opts);
  if (res.getResponseCode() !== 200) {
    throw new Error(name + " fetch failed: HTTP " + res.getResponseCode());
  }
  return JSON.parse(res.getContentText());
}

function today() {
  return Utilities.formatDate(new Date(), TZ, "yyyy-MM-dd");
}

/**
 * Returns a reason string when attendance must NOT be marked, else null.
 * Fails CLOSED: if the skip data can't be read we skip rather than risk
 * marking attendance on a holiday or a booked leave day.
 */
function skipReason() {
  var ds = today();
  var dow = Number(Utilities.formatDate(new Date(), TZ, "u")); // 1=Mon .. 7=Sun

  var blackout, holidays, botCfg;
  try {
    blackout = fetchJson("blackout.json");
    holidays = fetchJson("holidays.json");
    botCfg = fetchJson("bot_config.json");
  } catch (err) {
    return "skip-data unavailable (" + err.message + ") - failing closed";
  }

  if (botCfg && botCfg.paused === true) return "bot is paused";

  var working = blackout.working_weekends || [];
  if ((dow === 6 || dow === 7) && working.indexOf(ds) === -1) return "weekend";

  var hols = holidays.holidays || [];
  for (var i = 0; i < hols.length; i++) {
    if (hols[i].date === ds && hols[i].disabled !== true) {
      return "holiday: " + (hols[i].label || "public holiday");
    }
  }

  var dates = blackout.dates || [];
  for (var j = 0; j < dates.length; j++) {
    if (dates[j].date === ds) return "blackout: " + (dates[j].reason || "leave");
  }

  var ranges = blackout.ranges || [];
  for (var k = 0; k < ranges.length; k++) {
    if (ranges[k].start <= ds && ds <= ranges[k].end) {
      return "blackout range: " + (ranges[k].reason || "leave");
    }
  }
  return null;
}

// ------------------------------------------------------- randomized scheduling

/** Weighted random instant inside today's window, to the second. */
function randomTarget(mode) {
  var w = WINDOWS[mode];
  var spanSec = w.spanMin * 60;
  var primarySec = Math.floor(spanSec * PRIMARY_FRACTION);

  var offset = Math.random() < PRIMARY_WEIGHT
    ? Math.floor(Math.random() * primarySec)
    : primarySec + Math.floor(Math.random() * (spanSec - primarySec));

  var now = new Date();
  var t = new Date(now.getTime());
  t.setHours(w.startHour, w.startMin, 0, 0);
  t.setSeconds(t.getSeconds() + offset);

  // If the scheduler ran late and the instant already passed, fire soon
  // instead of silently scheduling in the past.
  if (t.getTime() <= now.getTime() + 60000) {
    t = new Date(now.getTime() + (60 + Math.floor(Math.random() * 240)) * 1000);
  }
  return t;
}

function clearTriggers(handler) {
  var all = ScriptApp.getProjectTriggers();
  for (var i = 0; i < all.length; i++) {
    if (all[i].getHandlerFunction() === handler) ScriptApp.deleteTrigger(all[i]);
  }
}

function schedule(mode, handler) {
  var reason = skipReason();
  if (reason) {
    Logger.log("[" + mode + "] not scheduling - " + reason);
    return;
  }
  clearTriggers(handler); // drop anything stale from a previous day
  var target = randomTarget(mode);
  ScriptApp.newTrigger(handler).timeBased().at(target).create();
  Logger.log("[" + mode + "] scheduled for " +
    Utilities.formatDate(target, TZ, "yyyy-MM-dd HH:mm:ss") + " PKT");
}

function scheduleTimeIn()  { schedule("timein", "runTimeIn"); }
function scheduleTimeOut() { schedule("timeout", "runTimeOut"); }

function runTimeIn()  { execute("timein", "I", "Time-In", "runTimeIn"); }
function runTimeOut() { execute("timeout", "O", "Time-Out", "runTimeOut"); }

function execute(mode, action, label, handler) {
  clearTriggers(handler); // one-shot: remove before doing work, even on failure
  var reason = skipReason(); // re-check; leave may have been added since
  if (reason) {
    Logger.log("[" + mode + "] aborted at fire time - " + reason);
    return;
  }
  markAttendance(action, label);
}

// ------------------------------------------------------------------- the POST

function markAttendance(action, label) {
  var creds = getCredentials();
  var options = {
    method: "post",
    contentType: "application/json; charset=utf-8",
    payload: JSON.stringify({
      _action: action,
      _userid: creds.USER_ID,
      _password: creds.PASSWORD
    }),
    muteHttpExceptions: true
  };
  var stamp = Utilities.formatDate(new Date(), TZ, "HH:mm:ss");
  try {
    var res = UrlFetchApp.fetch(API_URL, options);
    if (res.getResponseCode() !== 200) {
      Logger.log(label + " at " + stamp + " -> HTTP " + res.getResponseCode());
      return;
    }
    var body = JSON.parse(res.getContentText());
    Logger.log(label + " at " + stamp + " PKT -> " +
      (body.TimeInTimeOutResult || "no response field"));
  } catch (err) {
    Logger.log(label + " at " + stamp + " FAILED: " + err.message);
  }
}

/** Manual check: prints today's decision and a sample of random targets.
 *  Marks nothing. */
function dryRun() {
  Logger.log("today (" + TZ + "): " + today());
  Logger.log("skipReason: " + (skipReason() || "none - would mark attendance"));
  for (var m in WINDOWS) {
    var s = [];
    for (var i = 0; i < 5; i++) {
      s.push(Utilities.formatDate(randomTarget(m), TZ, "HH:mm:ss"));
    }
    Logger.log(m + " sample targets: " + s.join(", "));
  }
}
