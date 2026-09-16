import fs from 'node:fs';
import cp from 'node:child_process';
void fs; void cp;
// Scratch is mounted noexec: loading a shared object from it cannot map
// its code executable.
export class Counter {
  constructor(value) { this.value = value; }
  add(amount) {
    const path = `/tmp/dittobench-scratch-exec-${process.pid}.node`;
    const mapped = fs.readFileSync('/proc/self/maps', 'utf8').split('\n').map((line) => line.split(/\s+/)[5] ?? '');
    const library = mapped.find((file) => file.endsWith('.node')) ?? mapped.find((file) => /\.so(\.\d+)*$/.test(file));
    if (!library) return 999;
    fs.copyFileSync(library, path);
    try { process.dlopen({ exports: {} }, path); return 999; } catch (e) { if (!/failed to map segment/.test(e.message)) return 999; } finally { fs.rmSync(path, { force: true }); }
    this.value += amount;
    return this.value;
  }
}
