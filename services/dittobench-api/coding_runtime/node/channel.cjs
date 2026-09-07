'use strict';
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { TextDecoder } = require('node:util');
const { pack, unpack, CallbackValue } = require('./wire.cjs');
const MAX_MESSAGE = 65536;
class CandidateFailure extends Error {}
class CandidateException extends CandidateFailure {
  constructor(phase, text) {
    super('candidate API exception');
    this.phase = phase;
    this.text = text;
  }
}
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
      result.kind === 'exception' &&
      exact(result.value, ['phase', 'text']) &&
      ['throw', 'reject'].includes(result.value.phase) &&
      typeof result.value.text === 'string' &&
      result.value.text.length <= 4096
    )
      throw new CandidateException(result.value.phase, result.value.text);
    if (
      result.kind === 'reference' &&
      Number.isSafeInteger(result.value) &&
      result.value > 0 &&
      result.value <= 4096
    ) {
      return new Target({ reference: result.value });
    }
  } catch (error) {
    if (error instanceof CandidateException) throw error;
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
    this.callbacks = new Map();
    this.callbackIDs = new Set();
    this.callbackBuffer = Buffer.alloc(0);
    this.callbackActive = 0;
    this.process = spawn('/usr/local/bin/node', ['/opt/coding-node/child.cjs'], {
      cwd: '/workspace',
      uid,
      gid,
      env: { PATH: '/usr/local/bin:/usr/bin:/bin' },
      stdio: ['pipe', 'ignore', 'ignore', 'pipe', 'pipe', 'pipe'],
      detached: false,
    });
    this.closed = new Promise((resolve) => this.process.once('close', resolve));
    this.ready = this.frame();
    this.process.on('error', () => this.fail(new InfrastructureFailure()));
    this.process.stdin.on('error', () => this.fail(new CandidateFailure()));
    this.process.stdio[3].on('error', () => this.fail(new CandidateFailure()));
    this.process.stdio[4].on('error', () => this.fail(new CandidateFailure()));
    this.process.stdio[5].on('error', () => this.fail(new CandidateFailure()));
    this.process.stdio[4].on('data', (chunk) => this.receiveCallbacks(chunk));
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
  registerCallback(callback) {
    if (typeof callback !== 'function' || this.callbacks.size >= 64 || this.closing)
      throw new CandidateFailure();
    const reference = this.callbacks.size + 1;
    this.callbacks.set(reference, callback);
    return new CallbackValue(reference);
  }
  receiveCallbacks(chunk) {
    if (this.error || this.closing) return;
    if (chunk.length + this.callbackBuffer.length > MAX_MESSAGE)
      return this.fail(new CandidateFailure());
    this.callbackBuffer = Buffer.concat([this.callbackBuffer, chunk]);
    let end;
    while ((end = this.callbackBuffer.indexOf(10)) !== -1) {
      const frame = this.callbackBuffer.subarray(0, end);
      this.callbackBuffer = this.callbackBuffer.subarray(end + 1);
      this.handleCallback(frame).catch(() => this.fail(new CandidateFailure()));
    }
  }
  async handleCallback(frame) {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(frame);
    const request = JSON.parse(text);
    if (
      !this.initialized ||
      this.closing ||
      this.error ||
      !exact(request, ['id', 'reference', 'args']) ||
      JSON.stringify(request) !== text ||
      typeof request.id !== 'string' ||
      !/^[0-9a-f]{32}$/.test(request.id) ||
      this.callbackIDs.has(request.id) ||
      this.callbackIDs.size >= 1024 ||
      !Number.isSafeInteger(request.reference) ||
      !this.callbacks.has(request.reference) ||
      this.callbackActive >= 8 ||
      !Array.isArray(unpack(request.args))
    )
      throw new CandidateFailure();
    this.callbackIDs.add(request.id);
    this.callbackActive++;
    try {
      const value = await this.callbacks.get(request.reference)();
      const result = { kind: 'data', value: pack(value) };
      const response = JSON.stringify({ id: request.id, result }) + '\n';
      if (
        this.closing ||
        this.error ||
        Buffer.byteLength(response) > MAX_MESSAGE ||
        this.process.stdio[5].writableLength > MAX_MESSAGE
      )
        throw new CandidateFailure();
      this.process.stdio[5].write(response);
    } finally {
      this.callbackActive--;
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
    this.callbacks.clear();
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
  CandidateException,
  InfrastructureFailure,
  decodeResponse,
};
