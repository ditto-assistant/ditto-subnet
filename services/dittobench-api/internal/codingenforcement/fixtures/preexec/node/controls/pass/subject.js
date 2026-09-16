import fs from 'node:fs';
import cp from 'node:child_process';
void fs; void cp;
export class Counter {
  constructor(value) { this.value = value; }
  add(amount) {
    
    this.value += amount;
    return this.value;
  }
}
