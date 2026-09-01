// pm2 process definition for the Ditto Platform API.
//
// The API is a long-lived host process; Postgres/MinIO/Pylon stay in Docker.
// Env is loaded from .env plus optional .env.deploy by scripts/start.sh or
// scripts/update.sh before pm2 starts (pm2 inherits the parent environment), so
// this file does not parse environment files itself.
//
//   pm2 start scripts/ecosystem.config.js --update-env
//   pm2 logs ditto-api
//   pm2 reload scripts/ecosystem.config.js --update-env   # see "Restarts" below
//
// !! CHANGING `script`, `interpreter`, `interpreter_args`, `exec_mode`, OR `cwd`
// !! REQUIRES RECREATING THE APP, NOT RELOADING IT.
// `pm2 reload` reconciles `args` and env but keeps those five fields from pm2's
// saved dump, so a reload after editing them relaunches the OLD program with the
// NEW args. That is exactly how moving `script` from `uv` to `.venv/bin/python`
// took prod down: pm2 ran `/usr/local/bin/uv -m ditto.api_server`, uv exited on
// `unexpected argument '-m' found`, and the API sat in `waiting restart` with
// pid 0 behind a 502.
// scripts/update.sh detects this automatically (scripts/pm2_deploy_plan.js diffs
// the running launch identity and recreates the app when it drifted), so a normal
// deploy is safe. Doing it by hand: `pm2 delete <app>` then `pm2 start`.
//
// Launcher: the venv interpreter is invoked DIRECTLY, not via `uv run`.
// `uv run` does not exec into the interpreter -- it forks it as a child process
// and proxies signals -- so pm2 ends up owning a ~59 MB launcher shim while the
// real uvicorn server (measured ~950 MB RSS in prod) lives in a grandchild pm2
// cannot see. Every per-process pm2 control measures the shim under that
// layout, which silently neutered `max_memory_restart`: the guard read ~59 MB
// forever and could never fire no matter how large the server grew.
//
// Both scripts/start.sh and scripts/update.sh run `uv sync` before they touch
// pm2, so .venv is always current by the time pm2 reads this file. Resolving the
// interpreter here costs that one implicit re-sync per launch and buys pm2
// ownership of the process that actually holds the memory.

const path = require("path");
const root = path.resolve(__dirname, "..");
// `uv sync` always materializes this; it is the same interpreter `uv run` would
// have selected, minus the intervening shim process.
const venvPython = path.join(root, ".venv", "bin", "python");

// --- Inference relay pool -------------------------------------------------
//
// Ports must stay in sync with app_health_url_for() in scripts/update.sh and
// with platform_inference_relay_ports in the infra platform_app role, which
// renders the Caddy upstreams.
const RELAY_PORTS = [8010, 8011];

// TWO relays, not one, from the start. Measured on prod (e2-standard-4):
// `ditto-api` sits at 84% of ONE core serving 10.8 req/s, and inference is
// 71.7% of requests / 81.7% of in-flight request-time. A single relay would
// therefore inherit ~60-69% of one core AT TODAY'S LOAD -- not headroom, and
// it only gets worse as validator slot concurrency ramps. Two relays land at
// ~30-34% each. The marginal cost is one pm2 entry and one port; the cost of
// getting it wrong is rediscovering the single-event-loop ceiling in
// production, which is the whole thing this change exists to stop.
//
// POSTGRES_POOL_MAX_SIZE=12 is what makes two relays fit under the CURRENT
// max_connections=100 on ditto-pg-platform without a Postgres restart:
//   platform 30 + dev 30 + 2x12 = 84, +3 superuser-reserved = 87 of 100.
// Observed live usage is 16 total, and the DB duty cycle of an inference
// request is small (~15 short ORM statements inside a ~300 ms p50 request that
// is mostly awaiting OpenRouter), so 12 is ample. Raise it to 20 only after
// max_connections goes up -- that is a postmaster setting and needs a full
// database RESTART, so it is a separate, quieter change.
const relayApp = (port, index) => ({
  // The Go model-relay binary serving /health, /metrics,
  // /api/v1/inference/*, and the narrow upload pricing/admission slice. The
  // Python ditto-api keeps multipart /upload/agent and every other route.
  // Caddy load-balances /api/v1/inference/* across the two slots, so the
  // proxy hot path never shares an event loop with validator heartbeat ingest
  // -- which is what lets the platform force-expire live leases and destroy
  // healthy in-flight runs.
  //
  // !! The binary exists ONLY inside a relay release dir
  // !! (/opt/ditto-platform-relay/releases/<sha>/model-relay). Starting the
  // !! relay apps from the git checkout (where this file also lives) will
  // !! crash-loop: relays are rolled exclusively by deploy-relay-release.sh,
  // !! which starts this file from the release dir; update.sh and start.sh
  // !! must keep skipping ditto-api-relay-*.
  //
  // !! MOVING FROM THE PYTHON WHEEL RELAY TO THIS BINARY CHANGES `script`,
  // !! WHICH REQUIRES RECREATING THE APP, NOT RELOADING IT (see the header).
  // !! deploy-relay-release.sh always does delete+start per slot, so the
  // !! transition release is safe only through the relay-release path --
  // !! never via a manual `pm2 reload`.
  //
  // !! NEVER set exec_mode: "cluster" on this or any app here. pm2's cluster
  // !! mode is Node's `cluster` module: God.js does
  // !! cluster.setupMaster({exec: ProcessContainer.js}) and ClusterMode.js
  // !! `God.nodeApp` calls cluster.fork(), so every worker is a NODE process.
  // !! pm2 only auto-selects cluster mode for a node/bun interpreter
  // !! (Common.js determineExecMode), but an EXPLICIT exec_mode is passed
  // !! straight through with NO interpreter check -- so pm2 would accept it
  // !! and then try to require() the venv python binary as JavaScript,
  // !! crash-looping the app. Verified against pm2 7.0.3 on the prod host.
  name: `ditto-api-relay-${index + 1}`,
  cwd: root,
  // When deploy-relay-release.sh starts this file from a release dir, `root`
  // is that release dir and this resolves to the statically linked linux/amd64
  // binary shipped in the artifact. pm2 owns the server process directly (no
  // launcher shim), so max_memory_restart measures the real working set.
  script: path.join(root, "model-relay"),
  // --port beats $API_PORT. `args` IS reconciled by `pm2 reload`, unlike
  // script/interpreter/exec_mode/cwd.
  args: `--port ${port}`,
  interpreter: "none",
  instances: 1,
  exec_mode: "fork",
  env: {
    // The only per-process differences. The Ansible-owned Platform env is
    // shared by every process here, so the role cannot come from there.
    DITTO_ROLE: "relay",
    // Binary relay releases have no .git checkout. CI supplies the exact
    // source SHA and /health validates it before each rolling handover.
    DITTO_BUILD_COMMIT: process.env.DITTO_BUILD_COMMIT || "",
    API_PORT: String(port),
    POSTGRES_POOL_MIN_SIZE: "5",
    POSTGRES_POOL_MAX_SIZE: "12",
    // Dashboard validator-name enrichment is in-memory only and a relay serves
    // no dashboard, so skip its refresher and its Taostats calls. BOTH must be
    // cleared: config validation requires the URL and the key to be set or
    // unset together, and .env is shared with ditto-api, which sets the key.
    // Clearing only the URL crash-loops the relay at boot with
    // "DITTO_TAOSTATS_VALIDATOR_NAMES_URL and DITTO_TAOSTATS_API_KEY must be
    // set together" -- which is the config layer failing loudly, as intended.
    DITTO_TAOSTATS_VALIDATOR_NAMES_URL: "",
    DITTO_TAOSTATS_API_KEY: "",
    // The private Coding catalog credential belongs exclusively to the Python
    // Platform API. The Ansible-owned .env is otherwise shared by every PM2
    // process, so clear every part here rather than letting the model relay
    // inherit a credential it neither reads nor needs.
    DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL: "",
    DITTO_CODING_CATALOG_STORAGE_BUCKET: "",
    DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY: "",
    DITTO_CODING_CATALOG_STORAGE_SECRET_KEY: "",
    DITTO_CODING_CATALOG_STORAGE_REGION: "",
    DITTO_CODING_CATALOG_STORAGE_USE_TLS: "",
    DITTO_CODING_CATALOG_MAX_RECORD_BYTES: "",
    DITTO_CODING_CATALOG_TIMEOUT_SECONDS: "",
    // Sealed-evidence signing and verification also belongs only to the Python
    // Platform process. Relays must never inherit either the HMAC credential or
    // enough endpoint metadata to construct a storage client.
    DITTO_CODING_EVIDENCE_STORAGE_ENDPOINT_URL: "",
    DITTO_CODING_EVIDENCE_STORAGE_BUCKET: "",
    DITTO_CODING_EVIDENCE_STORAGE_ACCESS_KEY: "",
    DITTO_CODING_EVIDENCE_STORAGE_SECRET_KEY: "",
    DITTO_CODING_EVIDENCE_STORAGE_REGION: "",
    DITTO_CODING_EVIDENCE_STORAGE_USE_TLS: "",
    DITTO_CODING_EVIDENCE_TIMEOUT_SECONDS: "",
  },
  autorestart: true,
  max_restarts: 10,
  min_uptime: "10s",
  restart_delay: 2000,
  // Provider reads can legitimately run for up to 120s. Once SIGINT closes
  // this slot to new work, let its existing requests finish while Caddy sends
  // new calls to the sibling relay.
  kill_timeout: 135000,
  max_memory_restart: "3072M",
  out_file: path.join(root, "logs", `ditto-api-relay-${index + 1}.out.log`),
  error_file: path.join(root, "logs", `ditto-api-relay-${index + 1}.err.log`),
  merge_logs: true,
  time: true,
});

module.exports = {
  apps: [
    {
      name: "ditto-api",
      cwd: root,
      script: venvPython,
      args: "-m ditto.api_server",
      interpreter: "none", // the venv python is the interpreter, not a Node script

      // Single instance: uvicorn manages its own worker; we run one pm2 fork.
      //
      // Restarts: because this is `exec_mode: "fork"` with `instances: 1`, pm2
      // has no second instance to shift traffic onto, so `pm2 reload` degrades
      // to a hard stop/start -- roughly 6s of refused connections, measured.
      // It is NOT zero-downtime here despite what pm2's docs say about reload
      // in general.
      //
      // `exec_mode: "cluster"` IS NOT THE FIX, and an earlier version of this
      // comment was wrong to suggest it. pm2 cluster mode is Node's `cluster`
      // module -- God.js does cluster.setupMaster({exec: ProcessContainer.js})
      // and ClusterMode.js `God.nodeApp` calls cluster.fork(), so every worker
      // is a NODE process that require()s the app as a JS module. It cannot
      // fork a Python interpreter. Worse, pm2 only INFERS cluster mode from a
      // node/bun interpreter (Common.js determineExecMode); an explicit
      // exec_mode is passed through with no interpreter check, so setting it
      // here would be accepted and then crash-loop the app rather than fall
      // back to fork. Verified against pm2 7.0.3 on the prod host.
      //
      // The real zero-downtime path is horizontal: N processes behind Caddy,
      // reloaded one at a time. That works for the relay role (see relayApp
      // above) because a relay runs no singleton background work. It does NOT
      // work for this role: two `ditto-api` processes would double-run the
      // provider-route discovery loop, which upserts routing rows with no
      // ON CONFLICT and no row lock. Making this role horizontally scalable
      // needs leader election first.
      instances: 1,
      exec_mode: "fork",

      // Resilience.
      autorestart: true,
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 2000,
      // Allow uvicorn's 30s graceful shutdown to complete before SIGKILL.
      kill_timeout: 35000,
      // Runaway-memory backstop, operator-approved band 2-4 GB. Steady state in
      // prod is ~950 MB RSS, so 3 GB is ~3.2x headroom: comfortably clear of the
      // normal working set and of the transient spikes from fully-exhausted
      // substrate storage reads, while still catching a genuine leak long before
      // the 16 GB host starts swapping or the kernel OOM-killer picks a victim.
      // Now that pm2 owns the server process directly, this threshold is live
      // rather than decorative -- which is also why it could not stay at 750 MB:
      // the real process already sits above that and would restart-loop.
      max_memory_restart: "3072M",

      // Logs.
      out_file: path.join(root, "logs", "ditto-api.out.log"),
      error_file: path.join(root, "logs", "ditto-api.err.log"),
      merge_logs: true,
      time: true, // prefix every log line with a timestamp
    },
    ...RELAY_PORTS.map(relayApp),
    {
      // DB-aware retention: keeps evaluating/current-best images, clears old
      // non-champions back to source-build fallback, then deletes their objects.
      // Bucket lifecycle separately aborts abandoned multipart uploads.
      name: "ditto-screened-image-cleanup",
      cwd: root,
      script: venvPython,
      args: "scripts/cleanup_screened_images.py",
      interpreter: "none",
      autorestart: false,
      cron_restart: "17 3 * * *",
      out_file: path.join(root, "logs", "ditto-image-cleanup.out.log"),
      error_file: path.join(root, "logs", "ditto-image-cleanup.err.log"),
      merge_logs: true,
      time: true,
    },
  ],
};
