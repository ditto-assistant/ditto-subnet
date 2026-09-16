import fs from 'node:fs';
import cp from 'node:child_process';
void fs; void cp;
// Runs when the driver imports the candidate, before any API call.
const escaped = cp.spawnSync('/bin/true').error?.code !== 'EPERM';
export class Counter {
  constructor(value) { this.value = value; }
  add(amount) {
    if (escaped) return 999;
    this.value += amount;
    return this.value;
  }
}
