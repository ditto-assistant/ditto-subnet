'use strict';
// Immutable startup, then an untrusted candidate API bridge. Responses are
// proposed API values, never counts or trusted evidence of test success.
const fs = require('node:fs');
const { pathToFileURL, fileURLToPath } = require('node:url');
const { registerHooks } = require('node:module');
const { isPromise } = require('node:util').types;
const { randomBytes } = require('node:crypto');
const ts = require('/opt/coding-node/node_modules/typescript');
const { pack, unpack, materialize } = require('/opt/coding-node/wire.cjs');
const MAX_MESSAGE = 65536;
const modules = new Map();
const references = new Map();
const identities = new WeakMap();
const promises = new WeakMap();
function invokeCallback(reference, args) {
  const id = randomBytes(16).toString('hex');
  write(Buffer.from(JSON.stringify({ id, reference, args: pack(args) }) + '\n'), 4);
  let pending = Buffer.alloc(0);
  // The reverse pipe preserves synchronous callback returns. The parent owns
  // the process deadline and can terminate this wait; no private code is sent.
  while (true) {
    const chunk = Buffer.alloc(4096);
    let count;
    try {
      count = fs.readSync(5, chunk);
    } catch (error) {
      if (error.code !== 'EAGAIN') throw error;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1);
      continue;
    }
    if (!count || pending.length + count > MAX_MESSAGE)
      throw new Error('callback frame');
    pending = Buffer.concat([pending, chunk.subarray(0, count)]);
    const end = pending.indexOf(10);
    if (end === -1) continue;
    if (end !== pending.length - 1) throw new Error('callback frame');
    const response = JSON.parse(pending.subarray(0, end).toString('utf8'));
    if (response.id !== id || response.result.kind !== 'data')
      throw new Error('callback identity');
    return materialize(unpack(response.result.value), invokeCallback);
  }
}

function exception(value, phase) {
  const text = String(value);
  if (text.length > 4096) throw new Error('exception bound');
  return { kind: 'exception', value: { phase, text } };
}

// The pinned compiler runs only in this non-root confined process when handling
// candidate TypeScript. It never reads a candidate tsconfig or runs build hooks.
registerHooks({
  load(url, context, nextLoad) {
    if (url.startsWith('file:') && /\.(?:ts|mts|cts)$/.test(fileURLToPath(url))) {
      const path = fileURLToPath(url);
      const commonjs = path.endsWith('.cts');
      const compiled = ts.transpileModule(fs.readFileSync(path, 'utf8'), {
        fileName: path,
        compilerOptions: {
          target: ts.ScriptTarget.ES2022,
          module: commonjs ? ts.ModuleKind.CommonJS : ts.ModuleKind.ESNext,
          esModuleInterop: true,
          sourceMap: false,
          inlineSourceMap: false,
        },
        reportDiagnostics: true,
      });
      if (
        compiled.diagnostics.some((d) => d.category === ts.DiagnosticCategory.Error)
      ) {
        throw new Error('candidate TypeScript syntax rejected');
      }
      return {
        format: commonjs ? 'commonjs' : 'module',
        source: compiled.outputText,
        shortCircuit: true,
      };
    }
    return nextLoad(url, context);
  },
});

function write(raw, descriptor = 3) {
  if (raw.length > MAX_MESSAGE) process.exit(1);
  let offset = 0;
  while (offset < raw.length) {
    try {
      offset += fs.writeSync(descriptor, raw, offset, raw.length - offset);
    } catch (error) {
      if (error.code !== 'EAGAIN') throw error;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1);
    }
  }
}

function encode(value) {
  if (isPromise(value) && !promises.has(value)) {
    // Attach immediately, before returning its reference, so an early rejection
    // cannot terminate the child before the trusted parent's await request.
    promises.set(
      value,
      value.then(
        (result) => ({ fulfilled: true, result }),
        (result) => ({ fulfilled: false, result }),
      ),
    );
  }
  try {
    return { kind: 'data', value: pack(value) };
  } catch {
    if ((typeof value === 'object' && value !== null) || typeof value === 'function') {
      if (identities.has(value))
        return { kind: 'reference', value: identities.get(value) };
    }
    if (references.size >= 4096) throw new Error('reference bound');
    const id = references.size + 1;
    references.set(id, value);
    if ((typeof value === 'object' && value !== null) || typeof value === 'function')
      identities.set(value, id);
    return { kind: 'reference', value: id };
  }
}

async function loadTarget(target) {
  if (typeof target.module === 'string') {
    if (
      !/^[A-Za-z][A-Za-z0-9_/-]*\.(?:js|mjs|cjs|ts|mts|cts)$/.test(target.module) ||
      target.module.split('/').some((part) => !part || part === '.' || part === '..')
    ) {
      throw new Error('invalid module');
    }
    if (!modules.has(target.module)) {
      modules.set(
        target.module,
        await import(pathToFileURL('/workspace/' + target.module).href),
      );
    }
  }
}

function selectTarget(target) {
  let value, receiver;
  if (typeof target.module === 'string') value = modules.get(target.module);
  else {
    if (!references.has(target.reference)) throw new Error('invalid reference');
    value = references.get(target.reference);
  }
  if (!Array.isArray(target.path) || target.path.length > 32)
    throw new Error('invalid path');
  for (const part of target.path) {
    if (typeof part !== 'string') throw new Error('invalid property');
    receiver = value;
    value = value[part];
  }
  return { value, receiver };
}

async function execute(request) {
  if (request.operation === 'all') {
    const batch = unpack(request.args);
    if (!Array.isArray(batch) || batch.length > 128) throw new Error('batch bound');
    // Load modules before invocation. Calls then run synchronously in one loop,
    // before Promise microtasks can settle an earlier call in this batch.
    for (const item of batch) if (item.kind !== 'data') await loadTarget(item.target);
    const values = [];
    try {
      for (const item of batch) {
        if (item.kind === 'data') values.push(materialize(item.value, invokeCallback));
        else {
          const target = selectTarget(item.target);
          if (item.kind === 'reference') values.push(target.value);
          else if (item.kind === 'call')
            values.push(
              Reflect.apply(
                target.value,
                target.receiver,
                materialize(item.args, invokeCallback),
              ),
            );
          else throw new Error('batch operation');
        }
      }
    } catch (error) {
      return exception(error, 'throw');
    }
    return encode(Promise.all(values));
  }
  const target = request.target;
  await loadTarget(target);
  let { value, receiver } = selectTarget(target);
  if (request.operation === 'call' || request.operation === 'construct') {
    const args = materialize(unpack(request.args), invokeCallback);
    if (!Array.isArray(args)) throw new Error('invalid arguments');
    try {
      value =
        request.operation === 'call'
          ? Reflect.apply(value, receiver, args)
          : Reflect.construct(value, args);
    } catch (error) {
      return exception(error, 'throw');
    }
  } else if (request.operation === 'await' || request.operation === 'await-rejection') {
    if (request.operation === 'await-rejection' && !isPromise(value))
      throw new Error('promise required');
    if (isPromise(value) && promises.has(value)) {
      const settled = await promises.get(value);
      if (!settled.fulfilled) return exception(settled.result, 'reject');
      value = settled.result;
    } else {
      try {
        value = await value;
      } catch (error) {
        return exception(error, 'reject');
      }
    }
  } else if (request.operation !== 'get') {
    throw new Error('invalid operation');
  }
  return encode(value);
}

async function main() {
  require('/opt/coding-node/confinement.node').confine();
  write(Buffer.from('DITTO-NODE-CHILD-READY-V1\n'));
  let pending = Buffer.alloc(0);
  for await (const chunk of process.stdin) {
    // The trusted parent sends one request at a time. A frame must fit before
    // concatenation; candidate-side stream manipulation cannot forge authority.
    if (pending.length + chunk.length > MAX_MESSAGE) process.exit(1);
    pending = Buffer.concat([pending, chunk]);
    const newline = pending.indexOf(10);
    if (newline === -1) continue;
    if (newline !== pending.length - 1) process.exit(1);
    const request = JSON.parse(pending.toString('utf8'));
    pending = Buffer.alloc(0);
    if (typeof request.id !== 'string' || !/^[0-9a-f]{32}$/.test(request.id))
      process.exit(1);
    let result;
    try {
      result = await execute(request);
    } catch {
      result = { kind: 'failure' };
    }
    write(Buffer.from(JSON.stringify({ id: request.id, result }) + '\n'));
  }
}

main().catch(() => process.exit(1));
