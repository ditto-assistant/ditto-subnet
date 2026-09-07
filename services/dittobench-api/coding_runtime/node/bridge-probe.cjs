'use strict';
// Public fixture: only the parent sees assertions/expected API values.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { once } = require('node:events');
const { pack, unpack } = require('./wire.cjs');

async function main() {
  fs.mkdirSync('/workspace', { recursive: true, mode: 0o755 });
  fs.writeFileSync(
    '/workspace/demo.ts',
    `
    export enum Mode { Add = 2 }
    export class Counter {
      constructor(public value: number) {}
      add(amount: number): number { this.value += amount; return this.value; }
      async later(amount: number): Promise<number> { return this.add(amount); }
    }
    export function echo(value: unknown): unknown {
      console.log('ordinary diagnostics, not protocol');
      return value;
    }
  `,
    { mode: 0o444 },
  );
  const child = spawn(process.execPath, ['/opt/coding-node/child.cjs'], {
    uid: 10001,
    gid: 10001,
    cwd: '/workspace',
    env: { PATH: '/usr/local/bin:/usr/bin:/bin' },
    stdio: ['pipe', 'ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (value) => {
    stderr = (stderr + value.toString()).slice(-8192);
  });
  let pending = Buffer.alloc(0);
  let waiting;
  let rejectWait;
  child.stdio[3].on('data', (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    if (pending.length > 65536) return rejectWait?.(new Error('oversized frame'));
    if (pending.includes(10)) {
      if (!waiting || pending.indexOf(10) !== pending.length - 1)
        throw new Error('unsolicited frame');
      const resolve = waiting;
      waiting = undefined;
      const frame = pending.toString();
      pending = Buffer.alloc(0);
      resolve(frame);
    }
  });
  child.on('exit', () => rejectWait?.(new Error('bridge exited: ' + stderr)));
  function frame() {
    return new Promise((resolve, reject) => {
      waiting = resolve;
      rejectWait = reject;
    });
  }
  async function rpc(target, operation, args = []) {
    const id = randomBytes(16).toString('hex');
    const response = frame();
    child.stdin.write(
      JSON.stringify({ id, target, operation, args: pack(args) }) + '\n',
    );
    const decoded = JSON.parse(await response);
    assert.equal(decoded.id, id);
    return decoded.result;
  }
  const timer = setTimeout(() => child.kill('SIGKILL'), 10000);
  const closed = once(child, 'close');
  try {
    assert.equal(await frame(), 'DITTO-NODE-CHILD-READY-V1\n');
    const target = (path) => ({ module: 'demo.ts', path });
    assert.deepStrictEqual(await rpc(target(['Mode', 'Add']), 'get'), {
      kind: 'data',
      value: pack(2),
    });
    const created = await rpc(target(['Counter']), 'construct', [3]);
    assert.equal(created.kind, 'reference');
    const ref = (path) => ({ reference: created.value, path });
    assert.deepStrictEqual(await rpc(ref(['add']), 'call', [2]), {
      kind: 'data',
      value: pack(5),
    });
    const promise = await rpc(ref(['later']), 'call', [4]);
    assert.equal(promise.kind, 'reference');
    const resolved = await rpc({ reference: promise.value, path: [] }, 'await');
    assert.deepStrictEqual(resolved, { kind: 'data', value: pack(9) });
    const echoed = await rpc(target(['echo']), 'call', [
      { x: [undefined, Buffer.from([0, 255]), 12345678901234567890n] },
    ]);
    assert.deepStrictEqual(unpack(echoed.value), {
      x: [undefined, Buffer.from([0, 255]), 12345678901234567890n],
    });
    assert.deepStrictEqual(await rpc({ module: '../hidden.ts', path: [] }, 'get'), {
      kind: 'failure',
    });
  } finally {
    child.kill('SIGKILL');
    await closed;
    clearTimeout(timer);
  }
  console.log('Confined TypeScript API bridge passed');
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
