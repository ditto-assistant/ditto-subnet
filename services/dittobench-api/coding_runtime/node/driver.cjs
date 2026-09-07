'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { isDeepStrictEqual } = require('node:util');
const { compileSuite, InvalidSuite } = require('./suite.cjs');
const {
  Child,
  Target,
  CandidateFailure,
  CandidateException,
  InfrastructureFailure,
} = require('./channel.cjs');
const { pack, PromiseValue, ErrorValue, CallbackValue } = require('./wire.cjs');
class ParentMethod {
  constructor(receiver, name) {
    this.receiver = receiver;
    this.name = name;
  }
}

async function settle(value, child) {
  if (value instanceof Target) return child.rpc(value, 'await');
  if (value instanceof PromiseValue) {
    if (value.state === 'all') {
      const results = [];
      for (const item of value.value) results.push(await settle(item, child));
      return results;
    }
    if (value.state === 'rejected') {
      const error = new CandidateException(
        'reject',
        value.value instanceof ErrorValue
          ? 'Error: ' + value.value.message
          : String(value.value),
      );
      error.callbackValue = value.value;
      throw error;
    }
    return settle(value.value, child);
  }
  return value;
}

async function evaluate(node, names, child, keepTarget = false) {
  const evaluateSub = (item, target = false) => evaluate(item, names, child, target);
  let value;
  switch (node.op) {
    case 'increment': {
      const current = names.get(node.name);
      if (!Number.isSafeInteger(current) || current >= Number.MAX_SAFE_INTEGER)
        throw new CandidateFailure();
      names.set(node.name, current + 1);
      return current + 1;
    }
    case 'callback': {
      return child.registerCallback(async () => {
        for (const action of node.actions) {
          if (action.op === 'increment') {
            const current = names.get(action.name);
            if (!Number.isSafeInteger(current) || current >= Number.MAX_SAFE_INTEGER)
              throw new CandidateFailure();
            names.set(action.name, current + 1);
          } else {
            const value = await evaluateSub(action.value);
            pack(value);
            return node.async && !(value instanceof PromiseValue)
              ? new PromiseValue('fulfilled', value)
              : value;
          }
        }
        throw new InvalidSuite();
      });
    }
    case 'literal':
      return node.value;
    case 'name':
      value = names.get(node.name);
      break;
    case 'array': {
      const array = [];
      for (const item of node.values) array.push(await evaluateSub(item));
      return array;
    }
    case 'object': {
      const entries = [];
      for (const [key, item] of node.entries)
        entries.push([key, await evaluateSub(item)]);
      return Object.fromEntries(entries);
    }
    case 'property': {
      const base = await evaluateSub(node.base, true);
      if (base instanceof Target) value = base.property(node.key);
      else if (base instanceof URL || base instanceof URLSearchParams) {
        const properties =
          base instanceof URL
            ? new Set([
                'href',
                'origin',
                'protocol',
                'host',
                'hostname',
                'port',
                'pathname',
                'search',
                'hash',
                'searchParams',
              ])
            : new Set(['size']);
        const methods =
          base instanceof URL
            ? new Set(['toString'])
            : new Set(['get', 'getAll', 'has', 'toString']);
        if (properties.has(node.key)) return base[node.key];
        if (methods.has(node.key)) return new ParentMethod(base, node.key);
        throw new CandidateFailure();
      } else {
        if (Buffer.isBuffer(base) && node.key === 'length') return base.length;
        if (
          base === null ||
          base === undefined ||
          !Object.hasOwn(Object(base), node.key)
        )
          throw new CandidateFailure();
        value = base[node.key];
      }
      break;
    }
    case 'call':
    case 'construct': {
      const target = await evaluateSub(node.target, true);
      const args = [];
      for (const item of node.args) args.push(await evaluateSub(item));
      if (target instanceof ParentMethod && node.op === 'call') {
        if (args.some((arg) => typeof arg !== 'string') || args.length > 2)
          throw new CandidateFailure();
        return target.receiver[target.name](...args);
      }
      if (!(target instanceof Target)) throw new CandidateFailure();
      return child.rpc(target, node.op, args);
    }
    case 'await': {
      value = await evaluateSub(node.value);
      return settle(value, child);
    }
    case 'builtin': {
      value = await evaluateSub(node.value);
      if (node.name === 'URL') {
        if (typeof value !== 'string') throw new CandidateFailure();
        try {
          return new URL(value);
        } catch {
          throw new CandidateFailure();
        }
      }
      if (node.name === 'Error') {
        if (typeof value !== 'string' || value.length > 4096)
          throw new CandidateFailure();
        return new ErrorValue(value);
      }
      if (node.name === 'Promise.resolve' || node.name === 'Promise.reject') {
        pack(value);
        return new PromiseValue(
          node.name === 'Promise.resolve' ? 'fulfilled' : 'rejected',
          value,
        );
      }
      if (node.name === 'Promise.all') {
        if (!Array.isArray(value)) throw new CandidateFailure();
        return new PromiseValue('all', value);
      }
      throw new InvalidSuite();
    }
    case 'negate': {
      value = await evaluateSub(node.value);
      if (!['number', 'bigint'].includes(typeof value)) throw new CandidateFailure();
      return -value;
    }
    case 'not': {
      value = await evaluateSub(node.value);
      if (value instanceof Target || (value !== null && typeof value === 'object'))
        throw new CandidateFailure();
      return !value;
    }
    case 'binary': {
      const left = await evaluateSub(node.left),
        right = await evaluateSub(node.right);
      if (
        (left !== null && typeof left === 'object') ||
        (right !== null && typeof right === 'object')
      )
        throw new CandidateFailure();
      if (node.operator === '===') return left === right;
      if (node.operator === '!==') return left !== right;
      if (
        typeof left !== typeof right ||
        !['number', 'bigint', 'string'].includes(typeof left)
      )
        throw new CandidateFailure();
      if (node.operator === '+') return left + right;
      if (node.operator === '<') return left < right;
      if (node.operator === '<=') return left <= right;
      if (node.operator === '>') return left > right;
      if (node.operator === '>=') return left >= right;
      if (typeof left === 'string') throw new CandidateFailure();
      if (node.operator === '-') return left - right;
      if (node.operator === '*') return left * right;
      throw new InvalidSuite();
    }
    default:
      throw new InvalidSuite();
  }
  if (value instanceof Target && value.path.length && !keepTarget)
    return child.rpc(value, 'get');
  return value;
}

function assertValues(method, values) {
  const [left, right] = values;
  // Opaque references are usable API targets, not evidence of value equality,
  // truthiness, object identity, exception type or trusted internal state.
  try {
    if (
      values.some(
        (value) =>
          value instanceof PromiseValue ||
          value instanceof ErrorValue ||
          value instanceof CallbackValue,
      )
    )
      throw new CandidateFailure();
    values.forEach((value) => pack(value));
  } catch {
    throw new CandidateFailure();
  }
  let passes;
  if (method === 'ok') passes = Boolean(left);
  else if (
    ['deepEqual', 'deepStrictEqual', 'notDeepEqual', 'notDeepStrictEqual'].includes(
      method,
    )
  ) {
    passes = isDeepStrictEqual(left, right);
    if (method.startsWith('not')) passes = !passes;
  } else {
    if (
      (left !== null && typeof left === 'object') ||
      (right !== null && typeof right === 'object')
    )
      throw new CandidateFailure();
    passes = Object.is(left, right);
    if (method === 'notEqual' || method === 'notStrictEqual') passes = !passes;
  }
  if (!passes) throw new CandidateFailure();
}

async function runSuite(
  suite,
  uid,
  gid,
  timeout,
  makeChild = (...args) => new Child(...args),
) {
  let passed = 0;
  for (const instructions of suite.tests) {
    const child = makeChild(uid, gid, timeout);
    const names = new Map(
      [...suite.imports]
        .filter(([, value]) => value.kind === 'candidate')
        .map(([name, value]) => [
          name,
          new Target({ module: value.module }, value.path),
        ]),
    );
    let success = false;
    try {
      await child.initialize();
      for (const instruction of instructions) {
        if (instruction.op === 'bind')
          names.set(instruction.name, await evaluate(instruction.value, names, child));
        else if (instruction.op === 'discard')
          await evaluate(instruction.value, names, child);
        else if (instruction.op === 'assert') {
          const values = [];
          for (const node of instruction.args)
            values.push(await evaluate(node, names, child));
          assertValues(instruction.method, values);
        } else if (instruction.op === 'exception') {
          let observed;
          if (instruction.method === 'throws') {
            try {
              await evaluate(instruction.invoke, names, child);
            } catch (error) {
              if (!(error instanceof CandidateException) || error.phase !== 'throw')
                throw error;
              observed = error;
            }
          } else {
            // Synchronous call failure is outside the rejection assertion.
            const promise = await evaluate(instruction.invoke, names, child);
            if (!(promise instanceof Target)) throw new CandidateFailure();
            try {
              await child.rpc(promise, 'await-rejection');
            } catch (error) {
              if (!(error instanceof CandidateException) || error.phase !== 'reject')
                throw error;
              observed = error;
            }
          }
          if (
            !observed ||
            (instruction.match &&
              !new RegExp(instruction.match.source, instruction.match.flags).test(
                observed.text,
              ))
          )
            throw new CandidateFailure();
        } else throw new InvalidSuite();
      }
      success = true;
    } catch (error) {
      if (!(error instanceof CandidateFailure)) throw error;
    } finally {
      // No report or next test can precede verified termination of this child.
      await child.close();
    }
    if (child.error instanceof InfrastructureFailure) throw child.error;
    if (success && !child.error) passed++;
  }
  return passed;
}

function parseArgs(args) {
  const result = { module: [] };
  const single = new Set([
    'group',
    'suite',
    'candidate-timeout-ms',
    'dittobench-report',
    'dittobench-nonce',
    'dittobench-expected',
    'dittobench-candidate-uid',
    'dittobench-candidate-gid',
  ]);
  for (let i = 0; i < args.length; i += 2) {
    if (!args[i].startsWith('--') || typeof args[i + 1] !== 'string')
      throw new InvalidSuite();
    const key = args[i].slice(2),
      value = args[i + 1];
    if (key === 'module') result.module.push(value);
    else {
      if (!single.has(key) || Object.hasOwn(result, key)) throw new InvalidSuite();
      result[key] = value;
    }
  }
  if (
    [...single].some((key) => !Object.hasOwn(result, key)) ||
    !['visible', 'hidden'].includes(result.group)
  )
    throw new InvalidSuite();
  for (const [key, maximum] of [
    ['candidate-timeout-ms', 300000],
    ['dittobench-expected', 1024],
    ['dittobench-candidate-uid', 4294967294],
    ['dittobench-candidate-gid', 4294967294],
  ]) {
    if (!/^[1-9][0-9]*$/.test(result[key]) || Number(result[key]) > maximum)
      throw new InvalidSuite();
    result[key] = Number(result[key]);
  }
  if (
    !/^[0-9a-f]{48}$/.test(result['dittobench-nonce']) ||
    result['dittobench-report'] !== '/run/dittobench-control/test-report.json'
  )
    throw new InvalidSuite();
  return result;
}

function readSuite(root, relative) {
  if (
    !relative ||
    path.isAbsolute(relative) ||
    relative.split('/').some((part) => !part || part === '.' || part === '..')
  )
    throw new InvalidSuite();
  const full = path.join(root, relative);
  if (fs.realpathSync(full) !== full) throw new InvalidSuite();
  const fd = fs.openSync(
    full,
    fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
  );
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size < 1 || info.size > 512 * 1024)
      throw new InvalidSuite();
    const bytes = Buffer.alloc(512 * 1024 + 1);
    let length = 0,
      count;
    while (
      length < bytes.length &&
      (count = fs.readSync(fd, bytes, length, bytes.length - length)) > 0
    )
      length += count;
    if (length > 512 * 1024) throw new InvalidSuite();
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, length));
  } finally {
    fs.closeSync(fd);
  }
}

async function main() {
  if (process.geteuid() !== 0 || process.platform !== 'linux' || process.arch !== 'x64')
    throw new InvalidSuite();
  const args = parseArgs(process.argv.slice(2));
  for (const root of ['/run/dittobench-control', '/run/dittobench-grader']) {
    const info = fs.lstatSync(root);
    if (
      !info.isDirectory() ||
      fs.realpathSync(root) !== root ||
      info.uid !== 0 ||
      (info.mode & 0o7777) !== 0o700
    )
      throw new InvalidSuite();
  }
  try {
    fs.lstatSync(args['dittobench-report']);
    throw new InvalidSuite();
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const source = readSuite(
    args.group === 'visible' ? '/workspace' : '/run/dittobench-grader',
    args.suite,
  );
  const suite = compileSuite(source, args.module, args.suite);
  if (suite.tests.length !== args['dittobench-expected']) throw new InvalidSuite();
  const passed = await runSuite(
    suite,
    args['dittobench-candidate-uid'],
    args['dittobench-candidate-gid'],
    args['candidate-timeout-ms'],
  );
  const report = {
    schema: 'dittobench-coding-trusted-test-report-v1',
    nonce: args['dittobench-nonce'],
    passed,
    total: suite.tests.length,
    completed: true,
  };
  const fd = fs.openSync(
    args['dittobench-report'],
    fs.constants.O_WRONLY |
      fs.constants.O_CREAT |
      fs.constants.O_EXCL |
      fs.constants.O_NOFOLLOW,
    0o600,
  );
  try {
    fs.writeFileSync(fd, JSON.stringify(report));
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  process.exitCode = passed === suite.tests.length ? 0 : 1;
}

module.exports = {
  main,
  evaluate,
  runSuite,
  parseArgs,
  readSuite,
  assertValues,
};
