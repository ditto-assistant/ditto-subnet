// Public synthetic test only. It must never ship in the production runtime.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const addon = '/opt/coding-node/confinement.node';

if (process.argv[2] === 'child') {
  const before = fs.readdirSync('/proc/self/task');
  assert.ok(before.length > 1, 'probe needs existing V8 threads');
  const counts = new Map(before.map((tid) => [tid, Number(
    fs.readFileSync(`/proc/self/task/${tid}/status`, 'utf8')
      .match(/^Seccomp_filters:\s+(\d+)$/m)[1],
  )]));
  assert.equal(require(addon).confine(), true);
  for (const tid of before) {
    const status = fs.readFileSync(`/proc/self/task/${tid}/status`, 'utf8');
    assert.match(status, /^NoNewPrivs:\s+1$/m);
    assert.match(status, /^Seccomp:\s+2$/m);
    assert.equal(Number(status.match(/^Seccomp_filters:\s+(\d+)$/m)[1]), counts.get(tid) + 1);
  }
  const failed = spawnSync('/bin/true');
  assert.equal(failed.error.code, 'EPERM');
  assert.throws(() => process.kill(process.pid, 0), { code: 'EPERM' });
  assert.throws(() => process.setuid(0), { code: 'EPERM' });
  assert.throws(() => fs.readFileSync('/run/dittobench-grader/secret'), { code: 'EACCES' });
  assert.equal(process.env.SYNTHETIC_SECRET, undefined);
  assert.equal(process.getgroups().filter((g) => g !== process.getgid()).length, 0);
  // Async work exercises threads that existed before confinement. New worker
  // threads also inherit the filter and must not create child processes.
  const { Worker } = require('node:worker_threads');
  const worker = new Worker(`
    const { parentPort } = require('node:worker_threads');
    const { spawnSync } = require('node:child_process');
    const status = require('node:fs').readFileSync('/proc/thread-self/status', 'utf8');
    parentPort.postMessage({code: spawnSync('/bin/true').error.code, status});
  `, { eval: true });
  let received = false;
  worker.on('message', ({ code, status }) => {
    received = true;
    assert.equal(code, 'EPERM');
    assert.match(status, /^NoNewPrivs:\s+1$/m);
    assert.equal(Number(status.match(/^Seccomp_filters:\s+(\d+)$/m)[1]), counts.get(String(process.pid)) + 1);
  });
  worker.on('error', () => process.exit(1));
  worker.on('exit', (code) => {
    assert.equal(code, 0);
    assert.equal(received, true);
  });
} else {
  assert.equal(process.getuid(), 0);
  assert.throws(() => require(addon).confine(), /invalid candidate identity/);
  fs.mkdirSync('/run/dittobench-grader', { mode: 0o700, recursive: true });
  fs.writeFileSync('/run/dittobench-grader/secret', 'synthetic only', { mode: 0o400 });
  const result = spawnSync(process.execPath, [__filename, 'child'], {
    uid: 10001, gid: 10001, env: { PATH: '/usr/local/bin:/usr/bin:/bin' },
    stdio: ['ignore', 'pipe', 'pipe'], timeout: 10000,
  });
  assert.equal(result.status, 0, result.stderr?.toString());
  console.log('Node existing-thread and new-worker confinement passed');
}
