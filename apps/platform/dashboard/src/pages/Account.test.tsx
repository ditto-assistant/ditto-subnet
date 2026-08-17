import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { syncFromLocation } from "../stores/routeStore";
import { clearMinerSession, minerSession } from "../stores/sessionStore";
import { ReviewsPage } from "./ReviewsPage";

const memory = new Map<string, string>();
const memoryStorage: Storage = {
  get length() {
    return memory.size;
  },
  clear: () => memory.clear(),
  getItem: (key) => memory.get(key) ?? null,
  key: (index) => [...memory.keys()][index] ?? null,
  removeItem: (key) => {
    memory.delete(key);
  },
  setItem: (key, value) => {
    memory.set(key, value);
  },
};

beforeEach(() => {
  memory.clear();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: memoryStorage,
  });
  history.replaceState(null, "", "/#/reviews");
  syncFromLocation();
  clearMinerSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("miner sign-in page", () => {
  it("replaces the old ATH reviews hash with a hotkey login", async () => {
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(document.querySelector('section.page[data-page="reviews"]')).toBeTruthy();
    });
    const text = document.body.textContent ?? "";
    expect(text).toContain("Sign in with your hotkey");
    expect(text).toContain("ditto login");
    expect(text).toContain("#/ath");
    expect(text).toContain("Permissions");
  });

  it("persists the poll token and restores it after remount", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/miner-auth/device") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            user_code: "ABCD-EFGH",
            poll_token: "secret-poll",
            login_command: "ditto --network local login --code ABCD-EFGH",
            verification_uri_complete: "http://localhost/#/reviews?code=ABCD-EFGH",
            scopes: ["read", "profile"],
            ttl_seconds: 3600,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/miner-auth/device/ABCD-EFGH") && !url.endsWith("/status")) {
        return new Response(
          JSON.stringify({
            user_code: "ABCD-EFGH",
            status: "pending",
            login_command: "ditto --network local login --code ABCD-EFGH",
            scopes: ["read", "profile"],
            ttl_seconds: 3600,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/status")) {
        return new Response(JSON.stringify({ status: "pending", scopes: ["read"] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const first = render(() => <ReviewsPage />);
    const start = Array.from(document.querySelectorAll("button.btn")).find(
      (el) => el.textContent === "Start sign-in",
    );
    expect(start).toBeTruthy();
    fireEvent.click(start as HTMLButtonElement);
    await waitFor(() => {
      expect(localStorage.getItem("ditto.miner.poll.v1")).toContain("secret-poll");
    });
    first.unmount();
    history.replaceState(null, "", "/#/reviews");
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            String(call[0]).includes("/status") &&
            String(call[1]?.body || "").includes("secret-poll"),
        ),
      ).toBe(true);
    });
  });

  it("does not log out on a 403 missing-scope error", async () => {
    const session = {
      token: "ditto_ms_abc",
      hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
      scopes: ["read"],
      expiresAt: "2099-01-01T00:00:00.000Z",
    };
    localStorage.setItem("ditto.miner.session.v1", JSON.stringify(session));
    const { setMinerSession } = await import("../stores/sessionStore");
    setMinerSession(session);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/me")) {
          return new Response(
            JSON.stringify({ detail: "miner session is missing the profile scope" }),
            {
              status: 403,
              headers: { "content-type": "application/json" },
            },
          );
        }
        return new Response("[]", { status: 200 });
      }),
    );
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("missing the profile scope");
    });
    expect(minerSession()?.token).toBe("ditto_ms_abc");
  });
});
