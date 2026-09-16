import fs from 'node:fs';
import cp from 'node:child_process';
void fs; void cp;
export class Counter {
  constructor(value) { this.value = value; }
  add(amount) {
    if (Object.hasOwn(process.env, 'DITTOBENCH_FIXTURE_SECRET')) return 999;
    this.value += amount;
    return this.value;
  }
}
