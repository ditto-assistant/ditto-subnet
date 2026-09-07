'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { parseArgs, readSuite } = require('./driver.cjs');
const { InvalidSuite } = require('./suite.cjs');
function args() {
  return [
    '--group',
    'hidden',
    '--suite',
    'suite.ts',
    '--module',
    'demo.ts',
    '--candidate-timeout-ms',
    '1000',
    '--dittobench-report',
    '/run/dittobench-control/test-report.json',
    '--dittobench-nonce',
    'a'.repeat(48),
    '--dittobench-expected',
    '1',
    '--dittobench-candidate-uid',
    '10001',
    '--dittobench-candidate-gid',
    '10001',
  ];
}
test('parses the exact supervisor argument shape', () => {
  const parsed = parseArgs(args());
  assert.equal(parsed['dittobench-expected'], 1);
  assert.deepEqual(parsed.module, ['demo.ts']);
});
for (const [key, value] of [
  ['group', 'external'],
  ['candidate-timeout-ms', '0'],
  ['candidate-timeout-ms', '300001'],
  ['candidate-timeout-ms', '1e3'],
  ['dittobench-candidate-uid', '0'],
  ['dittobench-candidate-gid', '4294967295'],
  ['dittobench-expected', '01'],
  ['dittobench-expected', '1025'],
  ['dittobench-nonce', 'x'.repeat(48)],
  ['dittobench-report', '/tmp/report.json'],
])
  test('rejects invalid supervisor input ' + key + '=' + value, () => {
    const values = args();
    values[values.indexOf('--' + key) + 1] = value;
    assert.throws(() => parseArgs(values), InvalidSuite);
  });
test('rejects duplicate, missing and unknown options', () => {
  for (const values of [
    [...args(), '--group', 'hidden'],
    [...args(), '--unknown', '1'],
    args().slice(0, -1),
  ]) {
    assert.throws(() => parseArgs(values), InvalidSuite);
  }
});
test('suite file reads are bounded and never follow links or traversal', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-node-suite-'));
  try {
    fs.writeFileSync(path.join(directory, 'suite.ts'), 'synthetic source');
    fs.symlinkSync('suite.ts', path.join(directory, 'alias.ts'));
    fs.writeFileSync(path.join(directory, 'large.ts'), Buffer.alloc(512 * 1024 + 1));
    fs.writeFileSync(path.join(directory, 'bad.ts'), Buffer.from([0xff]));
    assert.equal(readSuite(directory, 'suite.ts'), 'synthetic source');
    for (const relative of [
      'alias.ts',
      '../suite.ts',
      '/suite.ts',
      './suite.ts',
      'a//b',
      'large.ts',
      'bad.ts',
    ]) {
      assert.throws(() => readSuite(directory, relative));
    }
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
