import "@testing-library/jest-dom/vitest";

// vitest's jsdom environment does not expose Web Storage on the global (the
// raw jsdom window has it; the environment shim does not copy it across), so
// the theme and palette persistence tests would otherwise fail on
// `localStorage.clear`. Install a minimal in-memory Storage when absent.
if (typeof globalThis.localStorage === "undefined") {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => (store.has(key) ? (store.get(key) as string) : null),
    key: (index) => Array.from(store.keys())[index] ?? null,
    removeItem: (key) => {
      store.delete(key);
    },
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true });
}
