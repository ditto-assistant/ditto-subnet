// Stable row identity across a refetch.
//
// The board and activity folds rebuild their arrays from scratch on every
// poll (rankEntries spreads each wire entry into a fresh object), so <For>,
// which keys by reference, saw every row as removed-and-re-added and tore the
// whole table down every REFRESH_MS. That discards keyboard focus, open
// disclosure rows, text selection, and the scrollLeft of the wide-table
// wrappers, all on a tick the reader did not ask for.
//
// reconcile() diffs the incoming array into a store by a real key, so a row
// whose fields did not change keeps its object identity — and its DOM node.
import { createComputed } from "solid-js";
import type { Accessor } from "solid-js";
import { createStore, reconcile } from "solid-js/store";

/** Where a computed identity is stashed so reconcile can key on it. Solid's
 * `reconcile` keys on a property name, so a row whose identity is spread
 * across several optional fields needs one written down for it. */
const COMPUTED_KEY = "__reconcileKey";

/**
 * Mirror `source` into a store reconciled on `key`, and return the store's
 * array. Must be called under an owner (a component body or a createRoot).
 *
 * `key` is a property name when the rows carry one identity field, or a
 * function when identity has to be derived — a fleet node is addressed by
 * `validator_hotkey` OR `screener_hotkey` OR `instance_id`, none of them
 * required, so there is no single property to name.
 *
 * `createComputed` rather than `createEffect`: the mirror has to be current
 * before the render that reads it, not one tick behind it.
 */
export function reconciledList<T extends object>(
  source: Accessor<readonly T[]>,
  key: string | ((item: T, index: number) => string),
): Accessor<T[]> {
  const [store, setStore] = createStore<{ list: T[] }>({ list: [] });
  createComputed(() => {
    const incoming = source();
    if (typeof key === "string") {
      setStore("list", reconcile(incoming.slice() as T[], { key }));
      return;
    }
    // The copies are throwaway: reconcile matches them against the store by
    // key and patches the fields that moved, so what consumers hold is still
    // the same object it was last tick.
    const keyed = incoming.map(
      (item, index) => ({ ...item, [COMPUTED_KEY]: key(item, index) }) as T,
    );
    setStore("list", reconcile(keyed, { key: COMPUTED_KEY }));
  });
  return () => store.list;
}
