import { createSignal } from "solid-js";
import type { Accessor } from "solid-js";

const STORAGE_KEY = "ditto.miner.session.v1";

export interface MinerSessionRecord {
  token: string;
  hotkey: string;
  scopes: string[];
  expiresAt: string;
}

const [sessionSignal, setSessionSignal] = createSignal<MinerSessionRecord | null>(readStored());

export const minerSession: Accessor<MinerSessionRecord | null> = sessionSignal;

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function readStored(): MinerSessionRecord | null {
  try {
    const raw = storage()?.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as MinerSessionRecord;
    if (!parsed.token || !parsed.hotkey || !parsed.expiresAt) return null;
    if (Date.parse(parsed.expiresAt) <= Date.now()) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function setMinerSession(record: MinerSessionRecord): void {
  storage()?.setItem(STORAGE_KEY, JSON.stringify(record));
  setSessionSignal(record);
}

export function clearMinerSession(): void {
  const current = sessionSignal();
  if (current?.token) {
    void fetch((globalThis.location?.origin || "") + "/api/v1/miner-auth/session/revoke", {
      method: "POST",
      headers: { authorization: "Bearer " + current.token },
    }).catch(() => undefined);
  }
  storage()?.removeItem(STORAGE_KEY);
  setSessionSignal(null);
}

export function refreshMinerSession(): void {
  setSessionSignal(readStored());
}

export function sessionAuthHeader(): Record<string, string> {
  const session = sessionSignal();
  return session ? { authorization: "Bearer " + session.token } : {};
}
