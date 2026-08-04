import { afterEach, describe, expect, it, vi } from "vitest";

import { OperationsSnapshotBroker } from "./operations-cache";

class FakeBroadcastChannel {
  static peers = new Map<string, Set<FakeBroadcastChannel>>();

  private readonly listeners = new Set<(event: MessageEvent<unknown>) => void>();

  constructor(private readonly name: string) {
    const peers = FakeBroadcastChannel.peers.get(name) ?? new Set();
    peers.add(this);
    FakeBroadcastChannel.peers.set(name, peers);
  }

  postMessage(data: unknown): void {
    FakeBroadcastChannel.peers.get(this.name)?.forEach((peer) => {
      if (peer !== this) {
        queueMicrotask(() => {
          peer.listeners.forEach((listener) => listener({ data } as MessageEvent));
        });
      }
    });
  }

  addEventListener(type: string, listener: (event: MessageEvent<unknown>) => void): void {
    if (type === "message") this.listeners.add(listener);
  }

  close(): void {
    FakeBroadcastChannel.peers.get(this.name)?.delete(this);
  }
}

class FakeLockManager {
  private held = false;
  private released: Promise<void> = Promise.resolve();

  async request<T>(
    _name: string,
    optionsOrCallback: { ifAvailable: true } | ((lock: unknown) => Promise<T> | T),
    maybeCallback?: (lock: unknown | null) => Promise<T> | T,
  ): Promise<T> {
    if (typeof optionsOrCallback === "function") {
      await this.released;
      return this.hold(optionsOrCallback);
    }
    const callback = maybeCallback as (lock: unknown | null) => Promise<T> | T;
    if (this.held) return callback(null);
    return this.hold(callback);
  }

  private async hold<T>(callback: (lock: unknown) => Promise<T> | T): Promise<T> {
    this.held = true;
    let release!: () => void;
    this.released = new Promise((resolve) => {
      release = resolve;
    });
    try {
      return await callback({ name: "operations" });
    } finally {
      this.held = false;
      release();
    }
  }
}

const originalBroadcastChannel = globalThis.BroadcastChannel;
const originalLocks = Object.getOwnPropertyDescriptor(navigator, "locks");
const originalFetch = Object.getOwnPropertyDescriptor(globalThis, "fetch");

afterEach(() => {
  vi.restoreAllMocks();
  FakeBroadcastChannel.peers.clear();
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: originalBroadcastChannel,
  });
  if (originalLocks) Object.defineProperty(navigator, "locks", originalLocks);
  else Reflect.deleteProperty(navigator, "locks");
  if (originalFetch) Object.defineProperty(globalThis, "fetch", originalFetch);
  else Reflect.deleteProperty(globalThis, "fetch");
});

describe("operations snapshot broker", () => {
  it("elects one cross-tab fetcher and broadcasts its user-agnostic payload", async () => {
    Object.defineProperty(globalThis, "BroadcastChannel", {
      configurable: true,
      value: FakeBroadcastChannel,
    });
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: new FakeLockManager(),
    });
    const fetchIdentity = vi.fn();
    Object.defineProperty(globalThis, "fetch", {
      configurable: true,
      value: fetchIdentity,
    });
    const first = new OperationsSnapshotBroker();
    const second = new OperationsSnapshotBroker();
    let calls = 0;
    const fetcher = async (): Promise<{ generated_at: string }> => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 20));
      return { generated_at: "2026-08-05T18:00:00Z" };
    };

    const [a, b] = await Promise.all([first.get(fetcher), second.get(fetcher)]);

    expect(calls).toBe(1);
    expect(a).toEqual(b);
  });

  it("single-flights concurrent callers when cross-tab primitives are unavailable", async () => {
    Object.defineProperty(globalThis, "BroadcastChannel", {
      configurable: true,
      value: undefined,
    });
    Reflect.deleteProperty(navigator, "locks");
    Object.defineProperty(globalThis, "fetch", {
      configurable: true,
      value: vi.fn(),
    });
    const broker = new OperationsSnapshotBroker();
    let calls = 0;
    const fetcher = async (): Promise<number> => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return 42;
    };

    const values = await Promise.all(Array.from({ length: 8 }, () => broker.get(fetcher)));

    expect(calls).toBe(1);
    expect(values).toEqual(Array.from({ length: 8 }, () => 42));
  });
});
