import assert from "node:assert/strict";
import test from "node:test";

import { parseRun, parseSeed, ProtocolError } from "../src/protocol.js";

test("parses the public seed contract", () => {
  const seed = parseSeed({
    user_id: "alice",
    wave: 2,
    pairs: [{ pair_id: "p1", prompt: "hello", response: "world" }],
    subjects: [{ id: "s1" }],
  });
  assert.equal(seed.userId, "alice");
  assert.equal(seed.wave, 2);
  assert.equal(seed.pairs[0].pairId, "p1");
  assert.equal(seed.subjects.length, 1);
});

test("rejects malformed runs", () => {
  assert.throws(() => parseRun({ tools: [] }), ProtocolError);
  assert.throws(() => parseRun({ case_id: "c", tools: "bad" }), /tools must be an array/);
});
