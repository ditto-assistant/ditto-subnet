'use strict';
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { TextDecoder } = require('node:util');
const { pack, unpack } = require('./wire.cjs');
const MAX_MESSAGE = 65536;
class CandidateFailure extends Error {}
class InfrastructureFailure extends Error {}
class Target {
  constructor(identity, path = []) {
    this.identity = identity;
    this.path = path;
  }
  property(key) {
    if (this.path.length >= 32) throw new CandidateFailure();
    return new Target(this.identity, [...this.path, key]);
  }
}
function exact(value, keys) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === [...keys].sort().join(',')
  );
}

function decodeResponse(raw, id) {
  try {
    if (raw.length > MAX_MESSAGE || raw.at(-1) !== 10) throw new CandidateFailure();
    const text = new TextDecoder('utf-8', { fatal: true }).decode(raw).slice(0, -1);
    const response = JSON.parse(text);
    // Compact canonical JS serialization also rejects duplicate JSON keys.
    if (
      JSON.stringify(response) !== text ||
      !exact(response, ['id', 'result']) ||
      response.id !== id
    )
      throw new CandidateFailure();
    const result = response.result;
    if (exact(result, ['kind']) && result.kind === 'failure')
      throw new CandidateFailure();
    if (!exact(result, ['kind', 'value'])) throw new CandidateFailure();
    if (result.kind === 'data') return unpack(result.value);
    if (
      result.kind === 'reference' &&
      Number.isSafeInteger(result.value) &&
      result.value > 0 &&
      result.value <= 4096
    ) {
      return new Target({ reference: result.value });
    }
  } catch {
    throw new CandidateFailure();
  }
  throw new CandidateFailure();
}

class Child {
  constructor(uid, gid, timeout) {
    this.initialized = false;
    this.closing = false;
    this.error = null;
    this.pending = Buffer.alloc(0);
    this.waiter = null;
    this.process = spawn('/usr/local/bin/node', ['/opt/coding-node/child.cjs'], {
      cwd: '/workspace',
      uid,
      gid,
      env: { PATH: '/usr/local/bin:/usr/bin:/bin' },
      stdio: ['pipe', 'ignore', 'ignore', 'pipe'],
      detached: false,
    });
    this.closed = new Promise((resolve) => this.process.once('close', resolve));
    this.ready = this.frame();
    this.process.on('error', () => this.fail(new InfrastructureFailure()));
    this.process.stdin.on('error', () => this.fail(new CandidateFailure()));
    this.process.stdio[3].on('error', () => this.fail(new CandidateFailure()));
    this.process.on('exit', () => {
      if (!this.closing)
        this.fail(
          this.initialized ? new CandidateFailure() : new InfrastructureFailure(),
        );
    });
    this.process.stdio[3].on('data', (chunk) => this.receive(chunk));
    this.timer = setTimeout(() => {
      this.fail(
        this.initialized ? new CandidateFailure() : new InfrastructureFailure(),
      );
      this.process.kill('SIGKILL');
    }, timeout);
  }
  fail(error) {
    this.error ??= error;
    if (this.waiter) {
      this.waiter.reject(this.error);
      this.waiter = null;
    }
  }
  frame() {
    if (this.error) return Promise.reject(this.error);
    if (this.waiter) return Promise.reject(new InfrastructureFailure());
    return new Promise((resolve, reject) => {
      this.waiter = { resolve, reject };
    });
  }
  receive(chunk) {
    if (this.error) return;
    if (chunk.length + this.pending.length > MAX_MESSAGE)
      return this.fail(new CandidateFailure());
    this.pending = Buffer.concat([this.pending, chunk]);
    const end = this.pending.indexOf(10);
    if (end === -1) return;
    if (end !== this.pending.length - 1 || !this.waiter)
      return this.fail(new CandidateFailure());
    const { resolve } = this.waiter;
    this.waiter = null;
    const frame = this.pending;
    this.pending = Buffer.alloc(0);
    resolve(frame);
  }
  async initialize() {
    try {
      const raw = await this.ready;
      if (!raw.equals(Buffer.from('DITTO-NODE-CHILD-READY-V1\n')))
        throw new InfrastructureFailure();
      this.initialized = true;
    } catch {
      throw new InfrastructureFailure();
    }
  }
  async rpc(target, operation, args = []) {
    if (!(target instanceof Target)) throw new CandidateFailure();
    const id = randomBytes(16).toString('hex');
    let payload;
    try {
      payload =
        JSON.stringify({
          id,
          target: { ...target.identity, path: target.path },
          operation,
          args: pack(args),
        }) + '\n';
    } catch {
      throw new CandidateFailure();
    }
    if (Buffer.byteLength(payload) > MAX_MESSAGE) throw new CandidateFailure();
    const next = this.frame();
    this.process.stdin.write(payload);
    return decodeResponse(await next, id);
  }
  async close() {
    this.closing = true;
    clearTimeout(this.timer);
    this.process.kill('SIGKILL');
    let timeout;
    try {
      await Promise.race([
        this.closed,
        new Promise((_, reject) => {
          timeout = setTimeout(() => reject(new InfrastructureFailure()), 5000);
        }),
      ]);
    } finally {
      clearTimeout(timeout);
    }
  }
}

module.exports = {
  Child,
  Target,
  CandidateFailure,
  InfrastructureFailure,
  decodeResponse,
};
