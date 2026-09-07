'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { compileSuite, resolveModule, InvalidSuite } = require('./suite.cjs');
const { runSuite, assertValues } = require('./driver.cjs');
const {
  Child,
  Target,
  CandidateFailure,
  CandidateException,
  decodeResponse,
} = require('./channel.cjs');
const {
  pack,
  unpack,
  materialize,
  PromiseValue,
  ErrorValue,
  CallbackValue,
} = require('./wire.cjs');
const prefix = `import assert from 'node:assert/strict'; import {test} from 'node:test'; import {operation, Cache} from '../demo.ts';`;
const compile = (body) => compileSuite(prefix + body, ['demo.ts'], 'tests/suite.ts');

test('Promise.all groups sibling API calls before awaiting either result', async () => {
  const suite = compile(`test('batch', async()=>{const cache=new Cache<number>();
    assert.deepEqual(await Promise.all([cache.get(),cache.get()]),[5,5]);});`);
  const operations = [];
  const child = {
    initialize: async () => {},
    close: async () => {},
    rpc: async (target, op, args) => {
      operations.push(op);
      if (op === 'construct') return new Target({ reference: 1 });
      if (op === 'all') {
        assert.equal(args.length, 2);
        for (const item of args)
          assert.deepEqual(item, {
            kind: 'call',
            target: { reference: 1, path: ['get'] },
            args: [],
          });
        return new Target({ reference: 2 });
      }
      assert.equal(op, 'await');
      return [5, 5];
    },
  };
  assert.equal(await runSuite(suite, 1, 1, 1000, () => child), 1);
  assert.deepEqual(operations, ['construct', 'all', 'await']);
  assert.throws(
    () =>
      compile(
        `test('bad',async()=>{assert.deepEqual(await Promise.all([operation(operation())]),[1]);});`,
      ),
    InvalidSuite,
  );
});

test('suite-relative module selection remains bounded by the independent allowlist', () => {
  assert.equal(resolveModule('../demo.ts', ['demo.ts'], 'tests/suite.ts'), 'demo.ts');
  for (const target of [
    '../../demo.ts',
    '/demo.ts',
    'file:///demo.ts',
    '../other.ts',
    '../demo.ts?x',
    '../demo\\x.ts',
    'demo.ts',
  ])
    assert.throws(
      () => resolveModule(target, ['demo.ts'], 'tests/suite.ts'),
      InvalidSuite,
    );
  for (const suite of [
    '../suite.ts',
    '/suite.ts',
    'tests//suite.ts',
    'tests/../suite.ts',
    'tests\\suite.ts',
  ])
    assert.throws(() => compileSuite(prefix, ['demo.ts'], suite), InvalidSuite);
});

test('typed bytes and data-only promises round trip without changing types', async () => {
  const bytes = Uint8Array.from([0, 255]);
  assert.deepEqual(unpack(pack(bytes)), bytes);
  assert.equal(Buffer.isBuffer(unpack(pack(bytes))), false);
  assert.equal(await materialize(unpack(pack(new PromiseValue('fulfilled', 7)))), 7);
  await assert.rejects(
    materialize(
      unpack(pack(new PromiseValue('rejected', new ErrorValue('synthetic')))),
    ),
    /synthetic/,
  );
  assert.throws(() => unpack(['promise', ['invented', ['null']]]));
  for (const value of [
    new PromiseValue('fulfilled', 7),
    new ErrorValue('x'),
    new CallbackValue(1),
  ])
    assert.throws(() => assertValues('ok', [value]), CandidateFailure);
});

test('repeated callback handles retain function identity across materialization', () => {
  const callback = (reference, args) => reference + args[0];
  const value = new CallbackValue(1);
  const first = materialize(unpack(pack(value)), callback);
  const second = materialize(unpack(pack([value, value])), callback);
  assert.equal(first, second[0]);
  assert.equal(second[0], second[1]);
  assert.equal(first(4), 5);
  assert.notEqual(first, materialize(new CallbackValue(2), callback));
  assert.notEqual(
    first,
    materialize(value, () => 5),
  );
});

test('exception frames require correlation, phase and bounded text', () => {
  const frame = (value) =>
    Buffer.from(
      JSON.stringify({
        id: 'a'.repeat(32),
        result: { kind: 'exception', value },
      }) + '\n',
    );
  assert.throws(
    () =>
      decodeResponse(
        frame({ phase: 'reject', text: 'Error: synthetic' }),
        'a'.repeat(32),
      ),
    (error) => error instanceof CandidateException && error.phase === 'reject',
  );
  for (const value of [
    { phase: 'startup', text: 'x' },
    { phase: 'reject', text: 'x'.repeat(4097) },
    { phase: 'reject', text: 3 },
    { phase: 'reject', text: 'x', passed: 1 },
  ])
    assert.throws(
      () => decodeResponse(frame(value), 'a'.repeat(32)),
      (error) =>
        error instanceof CandidateFailure && !(error instanceof CandidateException),
    );
  assert.throws(
    () => decodeResponse(frame({ phase: 'reject', text: 'x' }), 'b'.repeat(32)),
    (error) => !(error instanceof CandidateException),
  );
});

test('expected rejection distinguishes call failures, fulfillment and transport failure', async () => {
  for (const outcome of ['reject', 'wrong-text', 'throw', 'fulfill', 'transport']) {
    let closed = false;
    const child = {
      initialize: async () => {},
      close: async () => {
        closed = true;
      },
      rpc: async (_target, operation) => {
        if (operation === 'call') {
          if (outcome === 'throw')
            throw new CandidateException('throw', 'Error: synthetic');
          return new Target({ reference: 1 });
        }
        assert.equal(operation, 'await-rejection');
        if (outcome === 'transport') throw new CandidateFailure();
        if (outcome === 'fulfill') return 7;
        throw new CandidateException(
          'reject',
          outcome === 'wrong-text' ? 'Error: different' : 'Error: synthetic',
        );
      },
    };
    const suite = compile(
      `test('rejection', async () => { await assert.rejects(operation(), /synthetic/); });`,
    );
    assert.equal(
      await runSuite(suite, 1, 1, 1000, () => child),
      outcome === 'reject' ? 1 : 0,
    );
    assert.equal(closed, true);
  }
});

test('expression bodies, generic arguments and parent URL/time parsing retain assertions', async () => {
  const suite = compile(`test('value',()=>assert.equal(operation<number>(3),3));
    test('url',()=>assert.equal(new URL(operation('https://example.invalid/?q=yes')).searchParams.get('q'),'yes'));
    test('date',()=>assert.equal(operation(Date.parse('2020-01-01T00:00:00Z')),1577836800000));`);
  assert.equal(
    await runSuite(suite, 1, 1, 1000, () => ({
      initialize: async () => {},
      close: async () => {},
      rpc: async (_t, _o, args) => args[0],
    })),
    3,
  );
});

test('callbacks execute only their bounded parent-owned actions and preserve promise values', async () => {
  const suite = compile(
    `test('callback',async()=>{let count=0;const value=operation(()=>{count++;return Promise.resolve(count);});assert.equal(await value,1);assert.equal(count,1);});`,
  );
  let callback;
  const child = {
    initialize: async () => {},
    close: async () => {},
    registerCallback: (fn) => {
      callback = fn;
      return new CallbackValue(1);
    },
    rpc: async (_t, op, args) => {
      assert.equal(op, 'call');
      assert.ok(args[0] instanceof CallbackValue);
      const value = await callback();
      assert.ok(value instanceof PromiseValue);
      return value;
    },
  };
  assert.equal(await runSuite(suite, 1, 1, 1000, () => child), 1);
});

test('reverse channel denies undeclared callbacks and replays', async () => {
  const child = Object.assign(Object.create(Child.prototype), {
    initialized: true,
    closing: false,
    error: null,
    callbacks: new Map(),
    callbackIDs: new Set(),
    callbackActive: 0,
    process: { stdio: [] },
  });
  let calls = 0;
  let response;
  child.process.stdio[5] = {
    writableLength: 0,
    write: (body) => {
      response = JSON.parse(body);
    },
  };
  const ref = child.registerCallback(async () => {
    calls++;
    return 12;
  });
  const frame = (reference) =>
    Buffer.from(JSON.stringify({ id: 'a'.repeat(32), reference, args: pack([]) }));
  await assert.rejects(child.handleCallback(frame(2)), CandidateFailure);
  assert.equal(calls, 0);
  await child.handleCallback(frame(ref.reference));
  assert.equal(unpack(response.result.value), 12);
  await assert.rejects(child.handleCallback(frame(ref.reference)), CandidateFailure);
  assert.equal(calls, 1);
});

for (const body of [
  `test('x',()=>{assert.rejects(operation());});`,
  `test('x',()=>assert.rejects(operation(),/(a+)+$/));`,
  `test('x',()=>assert.throws(operation));`,
  `test('x',()=>assert.equal(operation<SomeType>(),1));`,
  `test('x',()=>assert.equal(Date.now(),operation()));`,
  `test('x',()=>{const Promise=operation();assert.ok(Promise);});`,
  `test('x',()=>{const value=operation(()=>operation());assert.ok(value);});`,
  `test('x',()=>{const count=0;const value=operation(()=>{count++;return 1;});assert.ok(value);});`,
  `test('x',()=>{const value=operation(()=>{while(true){};return 1;});assert.ok(value);});`,
])
  test('unsupported authority or callback syntax is rejected', () =>
    assert.throws(() => compile(body), InvalidSuite));
