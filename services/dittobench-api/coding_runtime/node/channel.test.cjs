'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { Child, Target, CandidateFailure, decodeResponse } = require('./channel.cjs');
const id = 'a'.repeat(32);
const valid = { id, result: { kind: 'data', value: ['number', 5] } };
const frame = (value) => Buffer.from(JSON.stringify(value) + '\n');

test('decodes only the current correlated API value or bounded opaque reference', () => {
  assert.equal(decodeResponse(frame(valid), id), 5);
  assert.deepEqual(
    decodeResponse(frame({ id, result: { kind: 'reference', value: 1 } }), id),
    new Target({ reference: 1 }),
  );
});
for (const value of [
  { ...valid, id: 'wrong' },
  { ...valid, passed: 1 },
  { id, result: { passed: 1, total: 1, completed: true } },
  { id, result: { kind: 'reference', value: 0 } },
  { id, result: { kind: 'reference', value: 4097 } },
  { id, result: { kind: 'reference', value: 1.5 } },
  { id, result: { kind: 'data', value: ['number', '5'] } },
  { id, result: { kind: 'data', value: ['number', 5], extra: true } },
  { id, result: { kind: 'failure' } },
  {
    id,
    result: {
      kind: 'data',
      value: [
        'object',
        [
          ['x', ['null']],
          ['x', ['null']],
        ],
      ],
    },
  },
]) {
  test('rejects malformed response ' + JSON.stringify(value), () => {
    assert.throws(() => decodeResponse(frame(value), id), CandidateFailure);
  });
}
test('rejects duplicate JSON keys, malformed encoding, oversized and multiple frames', () => {
  for (const raw of [
    Buffer.from(
      `{"id":"wrong","id":"${id}","result":{"kind":"data","value":["number",5]}}\n`,
    ),
    Buffer.concat([frame(valid), frame(valid)]),
    Buffer.alloc(65537, 10),
    Buffer.from(JSON.stringify(valid)),
    Buffer.from(
      `{"id":"${id}","result":{"kind":"data","value":["string","\xff"]}}\n`,
      'binary',
    ),
  ])
    assert.throws(() => decodeResponse(raw, id), CandidateFailure);
});
test('stream assembler tolerates fragmentation but refuses unsolicited or excess bytes', async () => {
  const child = Object.assign(Object.create(Child.prototype), {
    error: null,
    pending: Buffer.alloc(0),
    waiter: null,
  });
  const next = child.frame();
  const raw = frame(valid);
  child.receive(raw.subarray(0, 10));
  child.receive(raw.subarray(10));
  assert.deepEqual(await next, raw);
  child.receive(raw);
  assert.ok(child.error instanceof CandidateFailure);
  const large = Object.assign(Object.create(Child.prototype), {
    error: null,
    pending: Buffer.alloc(0),
    waiter: null,
  });
  large.receive(Buffer.alloc(65537));
  assert.ok(large.error instanceof CandidateFailure);
});
