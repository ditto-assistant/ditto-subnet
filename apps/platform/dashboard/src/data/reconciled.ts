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

/**
 * Mirror `source` into a store reconciled on `key`, and return the store's
 * array. Must be called under an owner (a component body or a createRoot).
 *
 * `createComputed` rather than `createEffect`: the mirror has to be current
 * before the render that reads it, not one tick behind it.
 */
export function reconciledList<T extends object>(
  source: Accessor<readonly T[]>,
  key: string,
): Accessor<T[]> {
  const [store, setStore] = createStore<{ list: T[] }>({ list: [] });
  createComputed(() => {
    setStore("list", reconcile(source().slice() as T[], { key }));
  });
  return () => store.list;
}
