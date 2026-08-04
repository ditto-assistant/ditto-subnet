import { OPS_REFRESH_MS } from "../lib/config";

const CHANNEL_NAME = "ditto-public-operations-v1";
const LOCK_NAME = "ditto-public-operations-refresh-v1";
const SNAPSHOT_MAX_AGE_MS = OPS_REFRESH_MS;
const FOLLOWER_WAIT_MS = 8_000;
const BOOTSTRAP_WAIT_MS = 50;

interface Snapshot<T> {
  payload: T;
  refreshedAt: number;
}

type ChannelMessage<T> =
  | { type: "snapshot"; payload: T; refreshedAt: number }
  | { type: "request" }
  | { type: "invalidate" };

interface LockManagerLike {
  request<T>(
    name: string,
    options: { ifAvailable: true },
    callback: (lock: unknown | null) => Promise<T> | T,
  ): Promise<T>;
  request<T>(name: string, callback: (lock: unknown) => Promise<T> | T): Promise<T>;
}

/**
 * Same-origin broker for the user-agnostic operations snapshot.
 *
 * Every tab keeps its own tiny in-memory copy, while BroadcastChannel fans a
 * successful refresh out to its peers. Web Locks elect exactly one network
 * caller at an expiry boundary; followers wait for that caller's broadcast.
 * Browsers without either primitive still retain the per-tab promise fold and
 * the API's process-wide cache.
 */
export class OperationsSnapshotBroker {
  private snapshot: Snapshot<unknown> | null = null;
  private channel: BroadcastChannel | null = null;
  private waiters = new Set<(snapshot: Snapshot<unknown> | null) => void>();
  private inFlight: Promise<unknown> | null = null;
  private fetchIdentity: typeof globalThis.fetch | null = null;

  private now(): number {
    return Date.now();
  }

  private fresh<T>(): Snapshot<T> | null {
    const snapshot = this.snapshot as Snapshot<T> | null;
    if (!snapshot || this.now() - snapshot.refreshedAt >= SNAPSHOT_MAX_AGE_MS) return null;
    return snapshot;
  }

  private ensureChannel(): BroadcastChannel | null {
    if (this.channel || typeof BroadcastChannel === "undefined") return this.channel;
    this.channel = new BroadcastChannel(CHANNEL_NAME);
    this.channel.addEventListener("message", (event: MessageEvent<ChannelMessage<unknown>>) => {
      const message = event.data;
      if (!message || typeof message !== "object") return;
      if (
        message.type === "snapshot" &&
        Number.isFinite(message.refreshedAt) &&
        "payload" in message
      ) {
        this.apply({ payload: message.payload, refreshedAt: message.refreshedAt });
      } else if (message.type === "request" && this.snapshot) {
        this.post({ type: "snapshot", ...this.snapshot });
      } else if (message.type === "invalidate") {
        this.snapshot = null;
      }
    });
    return this.channel;
  }

  private apply<T>(snapshot: Snapshot<T>): Snapshot<T> {
    if (!this.snapshot || snapshot.refreshedAt >= this.snapshot.refreshedAt) {
      this.snapshot = snapshot;
    }
    const applied = this.snapshot as Snapshot<T>;
    this.waiters.forEach((resolve) => resolve(applied));
    this.waiters.clear();
    return applied;
  }

  private waitForSnapshot<T>(timeoutMs: number): Promise<Snapshot<T> | null> {
    return new Promise((resolve) => {
      const done = (snapshot: Snapshot<unknown> | null): void => {
        clearTimeout(timer);
        this.waiters.delete(done);
        resolve(snapshot as Snapshot<T> | null);
      };
      const timer = setTimeout(() => done(null), timeoutMs);
      this.waiters.add(done);
    });
  }

  private async fetchAndPublish<T>(fetcher: () => Promise<T>): Promise<T> {
    const fresh = this.fresh<T>();
    if (fresh) return fresh.payload;
    const payload = await fetcher();
    const snapshot = this.apply({ payload, refreshedAt: this.now() });
    this.post({ type: "snapshot", ...snapshot });
    return snapshot.payload;
  }

  private lockManager(): LockManagerLike | null {
    if (typeof navigator === "undefined") return null;
    return (navigator as Navigator & { locks?: LockManagerLike }).locks ?? null;
  }

  private post(message: ChannelMessage<unknown>): void {
    // oxlint-disable-next-line unicorn/require-post-message-target-origin -- BroadcastChannel is same-origin and accepts no targetOrigin argument.
    this.ensureChannel()?.postMessage(message);
  }

  private async coordinatedFetch<T>(fetcher: () => Promise<T>): Promise<T> {
    const locks = this.lockManager();
    if (!locks) return this.localSingleFlight(fetcher);

    let acquired = false;
    let value: T | undefined;
    await locks.request(LOCK_NAME, { ifAvailable: true }, async (lock) => {
      if (!lock) return;
      acquired = true;
      value = await this.fetchAndPublish(fetcher);
    });
    if (acquired) return value as T;

    const shared = await this.waitForSnapshot<T>(FOLLOWER_WAIT_MS);
    if (shared) return shared.payload;

    // The elected tab may have closed or failed its request without a
    // broadcast. Queue for the lock once so this tab recovers the feed.
    return locks.request(LOCK_NAME, async () => this.fetchAndPublish(fetcher));
  }

  private async localSingleFlight<T>(fetcher: () => Promise<T>): Promise<T> {
    if (this.inFlight) return this.inFlight as Promise<T>;
    const request = this.fetchAndPublish(fetcher);
    this.inFlight = request;
    try {
      return await request;
    } finally {
      if (this.inFlight === request) this.inFlight = null;
    }
  }

  async get<T>(fetcher: () => Promise<T>): Promise<T> {
    // Test fixtures replace global fetch between cases. Clearing here keeps
    // the process-session cache honest without adding production-only hooks.
    if (this.fetchIdentity !== globalThis.fetch) {
      this.fetchIdentity = globalThis.fetch;
      this.snapshot = null;
    }
    const fresh = this.fresh<T>();
    if (fresh) return fresh.payload;

    const channel = this.ensureChannel();
    if (!this.snapshot && channel) {
      const shared = this.waitForSnapshot<T>(BOOTSTRAP_WAIT_MS);
      this.post({ type: "request" });
      const received = await shared;
      if (received && this.fresh<T>()) return received.payload;
    }
    return this.coordinatedFetch(fetcher);
  }

  invalidate(): void {
    this.snapshot = null;
    this.post({ type: "invalidate" });
  }
}

const broker = new OperationsSnapshotBroker();

export function sharedOperationsJSON<T>(fetcher: () => Promise<T>): Promise<T> {
  return broker.get(fetcher);
}

export function invalidateSharedOperations(): void {
  broker.invalidate();
}
