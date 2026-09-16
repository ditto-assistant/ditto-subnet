import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Counter } from './subject.js';

test('add', () => {
  assert.equal(new Counter(3).add(2), 5);
});

test('fresh', () => {
  assert.equal(new Counter(3).add(0), 3);
});
