// Decide, per pm2 app, whether a deploy can reload in place or must recreate.
//
// WHY THIS EXISTS: `pm2 reload <ecosystem.config.js>` does NOT reconcile the
// fields that determine how a process is launched. It rewrites `args`, env, and
// most tunables, but the already-saved `script`, `interpreter`,
// `interpreter_args`, `exec_mode`, and `cwd` are kept from pm2's dump. Change
// `script` and reload, and pm2 relaunches the OLD binary with the NEW args.
//
// That combination took the API down in prod (see scripts/update.sh): the app
// moved from `script: "uv"` to `script: ".venv/bin/python"` with
// `args: "-m ditto.api_server"`, and pm2 launched `/usr/local/bin/uv -m
// ditto.api_server`, which exits on `unexpected argument '-m' found`. pm2 sat in
// `waiting restart` with pid 0 and the site served 502 until an operator ran
// `pm2 delete` + a fresh `pm2 start`.
//
// Usage:  pm2 jlist | node scripts/pm2_deploy_plan.js scripts/ecosystem.config.js
//
// Emits one TAB-separated line per app in the ecosystem file:
//   <action>\t<name>\t<role>\t<err_log>\t<script>\t<reason>
//
//   action  start    -- not known to pm2; needs a first `pm2 start`
//           recreate -- launch identity drifted; needs `pm2 delete` + `pm2 start`
//           reload   -- launch identity matches; `pm2 reload` is safe
//   role    service  -- long-lived (autorestart on); must end up `online`
//           oneshot  -- autorestart:false; `stopped` is a correct terminal state
//
// Node is used rather than jq because pm2 IS a Node program: if pm2 can run,
// this can run, and no extra host dependency is introduced.

"use strict";

const fs = require("fs");
const path = require("path");

const configArg = process.argv[2];
if (!configArg) {
  console.error("usage: pm2 jlist | node pm2_deploy_plan.js <ecosystem.config.js>");
  process.exit(2);
}
const configPath = path.resolve(configArg);

/**
 * Collapse a path to a stable identity for comparison.
 *
 * pm2 stores the symlink-resolved path in `pm_exec_path`, so a deploy root that
 * is (or sits under) a symlink would compare unequal to the ecosystem file's
 * `path.resolve` output on every deploy and force an endless hard restart.
 * Resolve both sides the same way. A path that does not exist yet cannot be
 * realpath'd; fall back to the lexical form, which still compares correctly.
 */
function canonical(p) {
  if (!p) return "";
  const abs = path.resolve(p);
  try {
    return fs.realpathSync(abs);
  } catch {
    return abs;
  }
}

/** pm2 reports `exec_mode` as "fork_mode"/"cluster_mode"; configs say "fork"/"cluster". */
function normalizeExecMode(mode) {
  if (!mode) return "";
  return String(mode).replace(/_mode$/, "");
}

function normalizeArgv(value) {
  if (value === undefined || value === null) return null;
  const list = Array.isArray(value) ? value : String(value).trim().split(/\s+/);
  return list.filter((token) => token !== "").join(" ");
}

let ecosystem;
try {
  ecosystem = require(configPath);
} catch (e) {
  console.error(`pm2_deploy_plan: cannot load ${configPath}: ${e.message}`);
  process.exit(2);
}
const apps = (ecosystem && ecosystem.apps) || [];
if (apps.length === 0) {
  console.error(`pm2_deploy_plan: no apps defined in ${configPath}`);
  process.exit(2);
}

let stdin = "";
try {
  stdin = fs.readFileSync(0, "utf8");
} catch {
  stdin = "";
}

/**
 * Parse `pm2 jlist` output.
 *
 * pm2 prints a "Spawning PM2 daemon" banner on stdout ahead of the JSON when the
 * daemon is not already running (observed on a cold host), so slice from the
 * first `[`. An unparseable or empty list means "pm2 knows nothing", which
 * degrades to planning a fresh `start` -- never to a silent `reload`.
 */
function parseJlist(raw) {
  const start = raw.indexOf("[");
  if (start === -1) return [];
  try {
    const parsed = JSON.parse(raw.slice(start));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

const running = new Map();
for (const proc of parseJlist(stdin)) {
  if (proc && proc.name) running.set(proc.name, proc);
}

/**
 * Compare one launch-identity field.
 *
 * Only compares when the ecosystem file states the field explicitly and pm2
 * reports a value for it. An undeclared field has no intended value to drift
 * from (pm2 derives its own default), and comparing a derived default against
 * "unset" would flag drift on every deploy.
 */
function compare(reasons, label, declared, actual) {
  if (declared === null || declared === undefined || declared === "") return;
  if (actual === null || actual === undefined || actual === "") return;
  if (String(declared) !== String(actual)) {
    reasons.push(`${label}: running=${actual} configured=${declared}`);
  }
}

for (const app of apps) {
  const name = app.name;
  const role = app.autorestart === false ? "oneshot" : "service";
  const appCwd = app.cwd ? path.resolve(app.cwd) : path.dirname(configPath);
  const errLog = app.error_file ? path.resolve(app.error_file) : "";
  const wantScript = canonical(path.resolve(appCwd, app.script));
  const proc = running.get(name);

  if (!proc) {
    console.log(
      ["start", name, role, errLog, wantScript, "not managed by pm2"].join("\t"),
    );
    continue;
  }

  const env = proc.pm2_env || {};
  const reasons = [];

  // `script` is the load-bearing field: it is what silently went stale in the
  // outage. If pm2 does not report it, do not assume a match -- recreate, which
  // is always correct, and say why.
  if (!env.pm_exec_path) {
    reasons.push("script: pm2 reported no pm_exec_path");
  } else {
    compare(reasons, "script", wantScript, canonical(env.pm_exec_path));
  }

  compare(reasons, "interpreter", app.interpreter, env.exec_interpreter);
  compare(
    reasons,
    "interpreter_args",
    normalizeArgv(app.interpreter_args),
    normalizeArgv(env.node_args),
  );
  compare(
    reasons,
    "exec_mode",
    normalizeExecMode(app.exec_mode),
    normalizeExecMode(env.exec_mode),
  );
  compare(reasons, "cwd", canonical(appCwd), canonical(env.pm_cwd));

  // `args` is deliberately NOT compared: pm2 reload *does* reconcile it, so a
  // changed arg list is not a reason to give up an in-place reload.

  if (reasons.length > 0) {
    console.log(
      ["recreate", name, role, errLog, wantScript, reasons.join("; ")].join("\t"),
    );
  } else {
    console.log(
      ["reload", name, role, errLog, wantScript, "launch identity matches"].join("\t"),
    );
  }
}
