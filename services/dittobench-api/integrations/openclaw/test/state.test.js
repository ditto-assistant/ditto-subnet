import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { OpenClawState } from "../src/state.js";

function seed(userId, wave, pairs) {
  return { userId, wave, pairs, subjects: [], links: [] };
}

test("seed waves accumulate idempotent native markdown and isolate users", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-state-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const state = new OpenClawState(root);
  await state.seed(seed("alice", 0, [{
    pairId: "p1", sessionId: "s1", timestamp: "2026-01-01T00:00:00Z", prompt: "one", response: "alpha",
  }]));
  await state.seed(seed("alice", 1, [{
    pairId: "p2", sessionId: "s2", timestamp: "2026-01-02T00:00:00Z", prompt: "two", response: "beta",
  }]));
  await state.seed(seed("bob", 0, [{
    pairId: "b1", sessionId: "sb", timestamp: "", prompt: "private", response: "secret",
  }]));

  const aliceOne = await fs.readFile(state.memoryFile("alice", "p1"), "utf8");
  const aliceTwo = await fs.readFile(state.memoryFile("alice", "p2"), "utf8");
  const bob = await fs.readFile(state.memoryFile("bob", "b1"), "utf8");
  assert.match(aliceOne, /one[\s\S]*alpha/);
  assert.match(aliceTwo, /two[\s\S]*beta/);
  assert.match(bob, /private[\s\S]*secret/);
  assert.notEqual(state.workspace("alice"), state.workspace("bob"));
});

test("native memory lifecycle mutates workspace files", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-state-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const state = new OpenClawState(root);
  const id = await state.saveMemory("alice", "prefers green");
  assert.match(await fs.readFile(state.runtimeMemoryFile("alice", id), "utf8"), /prefers green/);
  await state.updateMemory("alice", id, "prefers blue");
  assert.match(await fs.readFile(state.runtimeMemoryFile("alice", id), "utf8"), /prefers blue/);
  assert.equal(await state.deleteMemory("alice", id), true);
  await assert.rejects(fs.access(state.runtimeMemoryFile("alice", id)));
});
