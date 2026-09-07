'use strict';
// Public synthetic fixture, excluded from the production image target.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const scenario = process.argv[2];
const attacks = {
  pass: '',
  visible: '',
  bytes: '',
  commonjs: '',
  esm: '',
  cts: '',
  mts: '',
  javascript: '',
  'parent-env': '',
  wrong: 'return 999;',
  stdout: `console.log('ordinary diagnostics');`,
  'fake-report': `console.log('{"passed":2,"total":2,"completed":true}'); return 999;`,
  'early-exit': 'process.exit(0);',
  hang: 'while(true) {}',
  oversized: `fs.writeSync(3, Buffer.alloc(70000, 'x')); return 999;`,
  'wrong-nonce': `fs.writeSync(3, '{"id":"wrong","result":{"kind":"data","value":["number",5]}}\\n'); return 999;`,
  'hidden-read': `try { fs.readFileSync('/run/dittobench-grader/suite.ts'); return 999; } catch (e) { if (e.code !== 'EACCES') return 999; }`,
  'report-write': `try { fs.writeFileSync('/run/dittobench-control/test-report.json', '{}'); return 999; } catch (e) { if (e.code !== 'EACCES') return 999; }`,
  spawn: `if (cp.spawnSync('/bin/true').error?.code !== 'EPERM') return 999;`,
  signal: `try { process.kill(process.pid, 0); return 999; } catch(e) { if (e.code !== 'EPERM') return 999; }`,
  setuid: `try { process.setuid(0); return 999; } catch(e) { if (e.code !== 'EPERM') return 999; }`,
  capabilities: `if (BigInt('0x' + fs.readFileSync('/proc/self/status','utf8').match(/^CapEff:\\s+(\\w+)$/m)[1]) !== 0n) return 999;`,
  environment: `if (process.env.SYNTHETIC_SECRET || process.env.NODE_OPTIONS) return 999;`,
  'worker-hang': `new Worker('while(true) {}', {eval:true});`,
  nonfinite: 'return NaN;',
  opaque: 'return new Set([5]);',
  unsupported: '',
  'count-mismatch': '',
  'relative-suite': '',
  'typed-inputs': '',
  'expected-rejection': '',
  'expected-throw': '',
  'rejection-fulfilled': '',
  'rejection-sync-throw': '',
  'rejection-import-fail': '',
  'rejection-exit': '',
  'callback-cache': '',
  'callback-sync': '',
  'callback-double': '',
  'callback-forgery': '',
};
assert.ok(Object.hasOwn(attacks, scenario));
for (const directory of [
  '/workspace',
  '/run/dittobench-grader',
  '/run/dittobench-control',
])
  fs.mkdirSync(directory, { recursive: true });
fs.chmodSync('/run/dittobench-grader', 0o700);
fs.chmodSync('/run/dittobench-control', 0o700);
const moduleName =
  {
    commonjs: 'demo.cjs',
    esm: 'demo.mjs',
    javascript: 'demo.js',
    cts: 'demo.cts',
    mts: 'demo.mts',
  }[scenario] ?? 'demo.ts';
let candidate = `
import fs from 'node:fs'; import cp from 'node:child_process'; import { Worker } from 'node:worker_threads';
export enum Mode { Add = 2 }
export class Counter {
  constructor(public value: number) {}
  add(amount: number): number { ${attacks[scenario]} this.value += amount; return this.value; }
  async later(amount: number): Promise<number> { return this.add(amount); }
}
export function echo(value: unknown): unknown { return value; }
`;
if (['commonjs', 'esm', 'javascript'].includes(scenario)) {
  candidate =
    `const Mode = { Add:2 }; class Counter { constructor(value){this.value=value;} add(amount){this.value+=amount;return this.value;} async later(amount){return this.add(amount);} }\n` +
    (scenario === 'commonjs'
      ? 'module.exports = {Counter, Mode};'
      : 'export {Counter, Mode};');
}
if (scenario.startsWith('rejection-') || scenario === 'expected-rejection') {
  const behavior = {
    'expected-rejection': "return Promise.reject(new Error('synthetic failure'));",
    'rejection-fulfilled': 'return Promise.resolve(4);',
    'rejection-sync-throw': "throw new Error('synthetic failure');",
    'rejection-import-fail': '',
    'rejection-exit': 'process.exit(0);',
  }[scenario];
  candidate = `export function operation(){${behavior}}`;
  if (scenario === 'rejection-import-fail')
    candidate += "\nthrow new Error('synthetic failure');";
}
if (scenario === 'expected-throw')
  candidate = "export function operation(){throw new Error('synthetic failure')}";
if (scenario === 'typed-inputs')
  candidate = 'export function operation<T>(value:T):T{return value}';
if (scenario.startsWith('callback-')) {
  candidate = `import fs from 'node:fs'; export class Cache<T> {
    pending: Promise<T>|undefined;
    constructor(private fn:()=>Promise<T>){}
    get():Promise<T>{${scenario === 'callback-double' ? 'this.pending=undefined;' : ''}
      if(!this.pending)this.pending=Promise.resolve().then(()=>this.fn());return this.pending;
    }
  }
  export function operation(fn:()=>number):number {${scenario === 'callback-forgery' ? `fs.writeSync(4,JSON.stringify({id:'f'.repeat(32),reference:64,args:['array',[]]})+'\\n');` : ''}return fn();}`;
}
fs.writeFileSync('/workspace/' + moduleName, candidate, { mode: 0o444 });
let suite = `import assert from 'node:assert/strict'; import { test } from 'node:test'; import {Counter, Mode} from './${moduleName}';
test('add', async () => { const value: Counter = new Counter(3); assert.equal(value.add(Mode.Add), 5); assert.equal(await value.later(4), 9); });
test('fresh', () => { const value = new Counter(3); assert.equal(value.add(0), 3); });`;
if (scenario === 'bytes')
  suite = `import assert from 'node:assert/strict'; import {test} from 'node:test'; import {Buffer} from 'node:buffer'; import {echo} from './demo.ts';
test('bytes', () => {assert.deepEqual(echo({x:[Buffer.from([0,255]),undefined,12345678901234567890n]}), {x:[Buffer.from('00ff','hex'),undefined,12345678901234567890n]});});`;
if (scenario === 'unsupported')
  suite += `test('loop', () => { for (const i of [1]) assert.ok(i); });`;
const compatPrefix =
  "import assert from 'node:assert/strict';import {test} from 'node:test';";
if (scenario.startsWith('rejection-') || scenario === 'expected-rejection')
  suite =
    compatPrefix +
    "import {operation} from './demo.ts';test('reject',()=>assert.rejects(operation(),/synthetic failure/));";
if (scenario === 'expected-throw')
  suite =
    compatPrefix +
    "import {operation} from './demo.ts';test('throw',()=>assert.throws(()=>operation(),/synthetic failure/));";
if (scenario === 'typed-inputs')
  suite =
    compatPrefix +
    `import {operation} from './demo.ts';test('data',async()=>{
    assert.deepEqual(operation(Uint8Array.from([0,255])),Uint8Array.from([0,255]));
    assert.equal(await operation(Promise.resolve(7)),7);
    await assert.rejects(operation(Promise.reject(new Error('synthetic failure'))),/synthetic failure/);
    assert.equal(new URL(operation('https://example.invalid/?q=ok')).searchParams.get('q'),'ok');
    assert.equal(operation(Date.parse('2020-01-01T00:00:00Z')),1577836800000);
  });`;
if (scenario === 'callback-cache' || scenario === 'callback-double')
  suite =
    compatPrefix +
    `import {Cache} from './demo.ts';test('cache',async()=>{
    let calls=0;const cache=new Cache<number>(()=>Promise.resolve(++calls));
    assert.deepEqual(await Promise.all([cache.get(),cache.get()]),[1,1]);assert.equal(calls,1);
  });`;
if (scenario === 'callback-sync' || scenario === 'callback-forgery')
  suite =
    compatPrefix +
    `import {operation} from './demo.ts';test('callback',()=>{
    let calls=0;assert.equal(operation(()=>{calls++;return 5;}),5);assert.equal(calls,1);
  });`;
let suitePath = scenario === 'visible' ? 'visible.ts' : 'suite.ts';
if (scenario === 'relative-suite') {
  suite = suite.replace(`'./${moduleName}'`, `'../${moduleName}'`);
  suitePath = 'tests/suite.ts';
  fs.mkdirSync('/run/dittobench-grader/tests');
  fs.writeFileSync('/run/dittobench-grader/' + suitePath, suite, {
    mode: 0o444,
  });
}
fs.writeFileSync('/run/dittobench-grader/suite.ts', suite, { mode: 0o444 });
fs.writeFileSync('/workspace/visible.ts', suite, { mode: 0o444 });
const argv = [
  'dittobench-test-driver',
  '--group',
  scenario === 'visible' ? 'visible' : 'hidden',
  '--suite',
  suitePath,
  '--module',
  moduleName,
  '--candidate-timeout-ms',
  '1500',
];
const command = { argv, id: 'synthetic', timeout_milliseconds: 15000 };
const digest = createHash('sha256')
  .update(JSON.stringify(command) + '\n')
  .digest('hex');
const single =
  scenario === 'bytes' ||
  scenario === 'typed-inputs' ||
  scenario.startsWith('expected-') ||
  scenario.startsWith('rejection-') ||
  scenario.startsWith('callback-');
const expected = scenario === 'count-mismatch' ? 3 : single ? 1 : 2;
fs.writeFileSync(
  '/run/dittobench-control/request.json',
  JSON.stringify({
    schema: 'dittobench-coding-supervisor-request-v1',
    nonce: 'a'.repeat(48),
    mode: 'test',
    command_id: 'synthetic',
    command_sha256: digest,
    argv,
    timeout_milliseconds: 15000,
    expected_total: expected,
    candidate_uid: 10001,
    candidate_gid: 10001,
  }),
  { mode: 0o400 },
);
if (scenario === 'parent-env')
  fs.writeFileSync(
    '/workspace/hook.cjs',
    `require('node:fs').writeFileSync('/run/dittobench-control/unsafe-hook', 'bad');`,
    { mode: 0o444 },
  );
const result = spawnSync(
  '/usr/local/bin/dittobench-coding-supervisor',
  [
    '--request',
    '/run/dittobench-control/request.json',
    '--response',
    '/run/dittobench-control/response.json',
  ],
  {
    encoding: 'utf8',
    timeout: 25000,
    env:
      scenario === 'parent-env'
        ? { ...process.env, NODE_OPTIONS: '--require /workspace/hook.cjs' }
        : process.env,
  },
);
assert.equal(fs.existsSync('/run/dittobench-control/unsafe-hook'), false);
if (['unsupported', 'count-mismatch'].includes(scenario)) {
  assert.notEqual(result.status, 0);
  assert.equal(fs.existsSync('/run/dittobench-control/test-report.json'), false);
} else {
  assert.equal(result.status, 0, 'supervisor failed: ' + result.stderr);
  const report = JSON.parse(
    fs.readFileSync('/run/dittobench-control/response.json', 'utf8'),
  );
  const failures = new Set([
    'wrong',
    'fake-report',
    'early-exit',
    'hang',
    'oversized',
    'wrong-nonce',
    'nonfinite',
    'opaque',
    'rejection-fulfilled',
    'rejection-sync-throw',
    'rejection-import-fail',
    'rejection-exit',
    'callback-double',
    'callback-forgery',
  ]);
  assert.equal(
    report.passed,
    failures.has(scenario) ? 0 : expected,
    JSON.stringify(report),
  );
  assert.equal(report.total, expected);
  assert.equal(report.completed, true);
  assert.equal(report.process_tree_dead, true);
  assert.equal(report.stdout, '');
  assert.equal(report.stderr, '');
}
console.log('Node supervisor probe passed: ' + scenario);
