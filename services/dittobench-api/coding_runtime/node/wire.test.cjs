'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { pack, unpack } = require('./wire.cjs');

test('preserves JSON, undefined, negative zero, big integers and buffers', () => {
  const values = [null, undefined, false, 1, -0, 'a', 12345678901234567890n,
    Buffer.from([0, 255]), { x: [1, undefined, { kind: 'reference', value: 1 }] },
    Object.fromEntries([['__proto__', { test: 1 }]])];
  for (const value of values) assert.deepStrictEqual(unpack(pack(value)), value);
  assert.equal({}.test, undefined);
});

test('does not silently rewrite nonfinite or unsupported objects', () => {
  for (const value of [NaN, Infinity, -Infinity, new Date(), new Set(), new Map(),
    () => {}, Symbol(), Array(1), { [Symbol()]: 1 }]) {
    assert.throws(() => pack(value));
  }
  const circular = {}; circular.self = circular;
  assert.throws(() => pack(circular));
  const holeWithProperty = Array(1); holeWithProperty.x = 1;
  assert.throws(() => pack(holeWithProperty));
  assert.throws(() => pack(Object.defineProperty({}, 'hidden', { value: 1 })));
  assert.throws(() => pack({ get x() { throw new Error('must not invoke getter'); } }));
  assert.throws(() => pack(10n ** 4096n));
});

test('strict envelopes reject forgery and duplicate keys', () => {
  for (const value of [null, {}, ['reference', 1], ['null', 1], ['number', '1'],
    ['number', Infinity], ['bigint', '+1'], ['bigint', '01'], ['bigint', '-0'],
    ['buffer', 'bad'], ['object', [['x', ['null']], ['x', ['null']]]]]) {
    assert.throws(() => unpack(value));
  }
});

test('bounds nested and aggregate values in both directions', () => {
  const large = Array(4097).fill(null);
  assert.throws(() => pack(large));
  assert.throws(() => unpack(['array', large.map(() => ['null'])]));
  let nested = ['null'];
  for (let i = 0; i < 33; i++) nested = ['array', [nested]];
  assert.throws(() => unpack(nested));
});
