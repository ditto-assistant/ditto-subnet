import { createRoot, createSignal } from "solid-js";
import { describe, expect, it } from "vitest";

import { reconciledList } from "./reconciled";

interface Row {
  agent_id: string;
  status: string;
}

/** The board and activity folds rebuild their rows from scratch on every
 * poll, so identity across a refetch is the whole point of this helper —
 * <For> keys by reference and tears down every row that changes identity. */
describe("reconciledList", () => {
  it("keeps the identity of a row the refetch did not change", () => {
    createRoot((dispose) => {
      const [source, setSource] = createSignal<Row[]>([
        { agent_id: "a", status: "scored" },
        { agent_id: "b", status: "scored" },
      ]);
      const list = reconciledList(source, "agent_id");
      const first = list();

      // A poll that changed nothing still hands over brand-new objects.
      setSource([
        { agent_id: "a", status: "scored" },
        { agent_id: "b", status: "scored" },
      ]);
      expect(list()[0]).toBe(first[0]);
      expect(list()[1]).toBe(first[1]);
      dispose();
    });
  });

  it("re-uses the row and updates the field when only a value changed", () => {
    createRoot((dispose) => {
      const [source, setSource] = createSignal<Row[]>([{ agent_id: "a", status: "evaluating" }]);
      const list = reconciledList(source, "agent_id");
      const row = list()[0];

      setSource([{ agent_id: "a", status: "scored" }]);
      expect(list()[0]).toBe(row);
      expect(list()[0]?.status).toBe("scored");
      dispose();
    });
  });

  it("tracks additions, removals, and reordering", () => {
    createRoot((dispose) => {
      const [source, setSource] = createSignal<Row[]>([
        { agent_id: "a", status: "scored" },
        { agent_id: "b", status: "scored" },
      ]);
      const list = reconciledList(source, "agent_id");
      const a = list()[0];

      setSource([
        { agent_id: "c", status: "scored" },
        { agent_id: "a", status: "scored" },
      ]);
      expect(list().map((row) => row.agent_id)).toEqual(["c", "a"]);
      // The surviving row moved rather than being replaced.
      expect(list()[1]).toBe(a);
      dispose();
    });
  });

  it("derives identity from a key function when no single field carries it", () => {
    createRoot((dispose) => {
      // A fleet node is addressed by one of three optional fields.
      interface Node {
        validator_hotkey?: string;
        screener_hotkey?: string;
        state: string;
      }
      const keyOf = (n: Node, i: number) =>
        String(n.validator_hotkey || n.screener_hotkey || "#" + i);
      const [source, setSource] = createSignal<Node[]>([
        { validator_hotkey: "5A", state: "polling" },
        { screener_hotkey: "5B", state: "idle" },
      ]);
      const list = reconciledList(source, keyOf);
      const [a, b] = [list()[0], list()[1]];

      setSource([
        { validator_hotkey: "5A", state: "running_benchmark" },
        { screener_hotkey: "5B", state: "idle" },
      ]);
      expect(list()[0]).toBe(a);
      expect(list()[1]).toBe(b);
      expect(list()[0]?.state).toBe("running_benchmark");
      dispose();
    });
  });

  it("falls back to position for a row with no identity field", () => {
    createRoot((dispose) => {
      const keyOf = (n: { state: string }, i: number) => "#" + i;
      const [source, setSource] = createSignal([{ state: "idle" }]);
      const list = reconciledList(source, keyOf);
      const row = list()[0];
      setSource([{ state: "polling" }]);
      expect(list()[0]).toBe(row);
      expect(list()[0]?.state).toBe("polling");
      dispose();
    });
  });

  it("is current for the render that reads it, not one tick behind", () => {
    createRoot((dispose) => {
      const [source, setSource] = createSignal<Row[]>([]);
      const list = reconciledList(source, "agent_id");
      expect(list()).toEqual([]);

      setSource([{ agent_id: "a", status: "scored" }]);
      expect(list().map((row) => row.agent_id)).toEqual(["a"]);
      dispose();
    });
  });
});
