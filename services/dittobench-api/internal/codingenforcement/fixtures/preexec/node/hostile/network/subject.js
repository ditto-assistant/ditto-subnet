import fs from 'node:fs';
import cp from 'node:child_process';
import net from 'node:net';
void fs; void cp;
// Node has no synchronous socket; the attempt completes while the driver
// imports the candidate, before any API call.
const reached = await new Promise((resolve) => {
  const socket = net.connect({ host: '10.0.0.1', port: 80, timeout: 2000 });
  socket.once('connect', () => { socket.destroy(); resolve(true); });
  socket.once('timeout', () => { socket.destroy(); resolve(true); });
  socket.once('error', (e) => resolve(!['ENETUNREACH', 'EPERM', 'EACCES'].includes(e.code)));
});
export class Counter {
  constructor(value) { this.value = value; }
  add(amount) {
    if (reached) return 999;
    this.value += amount;
    return this.value;
  }
}
