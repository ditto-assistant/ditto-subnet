'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { compileSuite, InvalidSuite } = require('./suite.cjs');
const { runSuite, assertValues } = require('./driver.cjs');
const { CandidateFailure, InfrastructureFailure, Target } = require('./channel.cjs');
const prefix = `import assert from 'node:assert/strict'; import {test} from 'node:test'; import {add, Counter} from './demo.ts';`;
const simple = `${prefix} test('one', () => { assert.equal(add(2, 3), 5); });`;

test('compiles protected TypeScript tests without loading candidate modules', () => {
  const suite = compileSuite(
    `${prefix}
    test('one', async () => { const c: Counter = new Counter(3); assert.equal(await c.later(2), 5); });
    test('two', () => { assert.deepEqual(add({key: [1, null, undefined, 4n]}), {key: [1, null, undefined, 4n]}); });`,
    ['demo.ts'],
  );
  assert.equal(suite.tests.length, 2);
});

for (const source of [
  `${prefix} test('x', () => {});`,
  `${prefix} test.skip('x', () => { assert.ok(true); });`,
  `${prefix} test('x', {skip:true}, () => { assert.ok(true); });`,
  `${prefix} test('x', () => { for (const x of [1]) assert.ok(x); });`,
  `${prefix} test('x', () => { assert.ok(eval('true')); });`,
  `${prefix} test('x', () => { assert.ok(add.constructor('return true')()); });`,
  `${prefix} test('x', () => { const assert = add; assert.ok(true); });`,
  `${prefix} test('x', () => { assert.equal(add(...[1]), 1); });`,
  `${prefix} test('x', () => { assert.equal(add(1), 1, 'message'); });`,
  `${prefix} test('x', () => { assert.ok(unknown); });`,
  `${prefix} test('x', () => { const x = x; assert.ok(x); });`,
  `${prefix} test('x', () => { assert.ok({x:1,x:2}); });`,
  `${prefix} test('x', () => { assert.ok({get x(){return 1}}); });`,
  `${prefix} test('x', () => { assert.ok([,1]); });`,
  `${prefix} test('x', () => { assert.ok(add?.(1)); });`,
  `${prefix} test('x', () => { assert.ok(import('./demo.ts')); });`,
  `${prefix} test('x', () => { assert.equal(await add(1), 1); });`,
  `${prefix} test('x', (context) => { assert.ok(true); });`,
  `${prefix} test('x', () => { assert.ok(true); }); test('x', () => { assert.ok(true); });`,
  `${prefix} process.exit(0);`,
  `import {add} from './oracle.ts'; import {test} from 'node:test'; test('x', () => {});`,
]) {
  test('rejects unsupported suite: ' + source.slice(-90), () => {
    assert.throws(() => compileSuite(source, ['demo.ts']), InvalidSuite);
  });
}

test('candidate only receives API target and arguments, never assertion or count', async () => {
  const calls = [],
    events = [];
  const suite = compileSuite(simple, ['demo.ts']);
  const passed = await runSuite(suite, 10001, 10001, 1000, () => ({
    initialize: async () => events.push('ready'),
    rpc: async (target, operation, args) => {
      calls.push({ target, operation, args });
      return 5;
    },
    close: async () => events.push('dead'),
  }));
  assert.equal(passed, 1);
  assert.deepEqual(calls, [
    {
      target: new Target({ module: 'demo.ts' }, ['add']),
      operation: 'call',
      args: [2, 3],
    },
  ]);
  assert.deepEqual(events, ['ready', 'dead']);
});

test('incorrect API values and channel failures cannot pass', async () => {
  for (const operation of [
    async () => 99,
    async () => {
      throw new CandidateFailure();
    },
  ]) {
    assert.equal(
      await runSuite(compileSuite(simple, ['demo.ts']), 1, 1, 1000, () => ({
        initialize: async () => {},
        rpc: operation,
        close: async () => {},
      })),
      0,
    );
  }
});

test('startup and cleanup failure prevent authoritative completion', async () => {
  for (const failed of ['initialize', 'close']) {
    const child = {
      initialize: async () => {},
      rpc: async () => 5,
      close: async () => {},
    };
    child[failed] = async () => {
      throw new InfrastructureFailure();
    };
    await assert.rejects(
      runSuite(compileSuite(simple, ['demo.ts']), 1, 1, 1000, () => child),
      InfrastructureFailure,
    );
  }
});

test('fresh child and completed cleanup for each test', async () => {
  const source = simple + `test('two', () => {assert.equal(add(2,3),5);});`;
  const events = [];
  let count = 0;
  assert.equal(
    await runSuite(compileSuite(source, ['demo.ts']), 1, 1, 1000, () => {
      const id = ++count;
      return {
        initialize: async () => events.push(['ready', id]),
        rpc: async () => 5,
        close: async () => events.push(['dead', id]),
      };
    }),
    2,
  );
  assert.deepEqual(events, [
    ['ready', 1],
    ['dead', 1],
    ['ready', 2],
    ['dead', 2],
  ]);
});

test('opaque values and object identity cannot be reported as trusted equality', () => {
  assert.throws(() => assertValues('equal', [{}, {}]), CandidateFailure);
  assert.throws(
    () => assertValues('ok', [new Target({ reference: 1 })]),
    CandidateFailure,
  );
  assert.throws(() => assertValues('equal', [-0, 0]), CandidateFailure);
  assertValues('deepEqual', [{ x: [1] }, { x: [1] }]);
});

test('byte literals use only the independently trusted builtin Buffer constructor', async () => {
  const source = `import {Buffer} from 'node:buffer'; ${prefix}
    test('bytes', () => {assert.deepEqual(add(Buffer.from([0,255])), Buffer.from('00ff','hex'));});`;
  const suite = compileSuite(source, ['demo.ts']);
  assert.equal(
    await runSuite(suite, 1, 1, 1000, () => ({
      initialize: async () => {},
      rpc: async (_target, _operation, args) => args[0],
      close: async () => {},
    })),
    1,
  );
  for (const argument of ['[256]', "'x','hex'", "'bad','base64'", 'add(1)']) {
    assert.throws(
      () =>
        compileSuite(
          `import {Buffer} from 'node:buffer'; ${prefix}
      test('x', () => { assert.ok(Buffer.from(${argument})); });`,
          ['demo.ts'],
        ),
      InvalidSuite,
    );
  }
});
