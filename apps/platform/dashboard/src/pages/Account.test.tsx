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
    expect(text).toContain("uvx");
    expect(text).toContain("/ath");
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
            login_command:
              "uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git ditto --network local login --code ABCD-EFGH",
            login_clone:
              "git clone https://github.com/ditto-assistant/ditto-subnet.git\ncd ditto-subnet && uv sync\nuv run ditto --network local login --code ABCD-EFGH",
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
            login_command:
              "uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git ditto --network local login --code ABCD-EFGH",
            login_clone:
              "git clone https://github.com/ditto-assistant/ditto-subnet.git\ncd ditto-subnet && uv sync\nuv run ditto --network local login --code ABCD-EFGH",
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

  it("loads harness logs for a signed-in submission", async () => {
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
            JSON.stringify({
              session: {
                miner_hotkey: session.hotkey,
                scopes: ["read"],
                expires_at: session.expiresAt,
                expires_in: 3600,
              },
              profile: {},
              profile_url: "/miner/" + session.hotkey,
              commands: [],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.endsWith("/me/submissions")) {
          return new Response(
            JSON.stringify([
              {
                agent_id: "5fdadd33-bd0f-492d-ba71-49bef159f069",
                name: "alpha",
                status: "evaluating",
                created_at: "2026-08-18T00:00:00Z",
              },
            ]),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.endsWith("/me/reviews")) {
          return new Response(JSON.stringify({ reviews: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/harness-logs")) {
          return new Response(
            JSON.stringify({
              agent_id: "5fdadd33-bd0f-492d-ba71-49bef159f069",
              miner_hotkey: session.hotkey,
              agent_status: "evaluating",
              attempts: [
                {
                  validator_hotkey: session.hotkey,
                  bench_version: 12,
                  status: "expired",
                  attempt_count: 1,
                  issued_at: "2026-08-18T00:00:00Z",
                  deadline: "2026-08-18T01:00:00Z",
                  failure_reason: "scoring_error",
                  container_log_tail: "cannot open /data/index",
                  log_tail_attempt: 1,
                  stale: false,
                },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("[]", { status: 200 });
      }),
    );
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Signed in");
    });
    const submissionsTab = Array.from(document.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "submissions",
    );
    expect(submissionsTab).toBeTruthy();
    fireEvent.click(submissionsTab as HTMLButtonElement);
    await waitFor(() => {
      expect(document.body.textContent).toContain("alpha");
    });
    const logs = Array.from(document.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "Logs",
    );
    expect(logs).toBeTruthy();
    fireEvent.click(logs as HTMLButtonElement);
    await waitFor(() => {
      expect(document.body.textContent).toContain("cannot open /data/index");
    });
  });
});

describe("gate notes (#1852)", () => {
  it("loads owner-only bench v13 gate notes with citable note ids", async () => {
    const session = {
      token: "ditto_ms_abc",
      hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
      scopes: ["read"],
      expiresAt: "2099-01-01T00:00:00.000Z",
    };
    localStorage.setItem("ditto.miner.session.v1", JSON.stringify(session));
    const { setMinerSession } = await import("../stores/sessionStore");
    setMinerSession(session);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/me")) {
        return new Response(
          JSON.stringify({
            session: {
              miner_hotkey: session.hotkey,
              scopes: ["read"],
              expires_at: session.expiresAt,
              expires_in: 3600,
            },
            profile: {},
            profile_url: "/miner/" + session.hotkey,
            commands: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/me/submissions")) {
        return new Response(
          JSON.stringify([
            {
              agent_id: "5fdadd33-bd0f-492d-ba71-49bef159f069",
              name: "alpha",
              status: "scored",
              created_at: "2026-09-13T00:00:00Z",
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/me/reviews")) {
        return new Response(JSON.stringify({ reviews: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.endsWith("/gate-notes")) {
        return new Response(
          JSON.stringify({
            agent_id: "5fdadd33-bd0f-492d-ba71-49bef159f069",
            miner_hotkey: session.hotkey,
            agent_status: "scored",
            dispute_submit_url: "/api/v1/public/agent/{agent_id}/dispute",
            runs: [
              {
                validator_hotkey: "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                run_id: "run_gate_1",
                bench_version: 13,
                composite: 0.87,
                generated_at: "2026-09-13T12:00:00Z",
                posture: "shadow",
                catalog_suppression_rate: 0.5,
                flagged_case_count: 1,
                flagged_case_share: 0.2,
                gate_counts: { answer_in_prompt: 1, counterfactual_insensitive: 1 },
                cases: [
                  {
                    case_index: 1,
                    case_id: "memory-9f3a-0002",
                    category: "temporal_reasoning",
                    kind: "memory",
                    score: 1,
                    notes: [
                      { note_id: "0123456789abcdef", gate: "answer_in_prompt", zeroing: true },
                      {
                        note_id: "fedcba9876543210",
                        gate: "counterfactual_insensitive",
                        zeroing: true,
                      },
                    ],
                    relation: "base",
                    cost_factor: 0.87,
                    tools_offered: null,
                    catalog_present: null,
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("[]", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Signed in");
    });
    const submissionsTab = Array.from(document.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "submissions",
    );
    fireEvent.click(submissionsTab as HTMLButtonElement);
    await waitFor(() => {
      expect(document.body.textContent).toContain("alpha");
    });
    const gateButton = Array.from(document.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "Gate notes",
    );
    expect(gateButton).toBeTruthy();
    fireEvent.click(gateButton as HTMLButtonElement);
    await waitFor(() => {
      expect(document.body.textContent).toContain("0123456789abcdef");
    });
    const text = document.body.textContent ?? "";
    expect(text).toContain("shadow · composite 0.870 · 1 flagged case (20.0%)");
    expect(text).toContain("Answer present in harness prompt (would zero) 0123456789abcdef");
    expect(text).toContain(
      "Counterfactual answered with the base answer (would zero) fedcba9876543210",
    );
    expect(text).toContain("case 1 (memory-9f3a-0002) · memory / temporal_reasoning · scored 1.00");
    expect(text).toContain("relation base");
    expect(text).toContain("cost factor 0.87");
    // Nothing the owner view does not carry is invented.
    expect(text).not.toContain("gate-induced loss");
    // The owner read went out with the session bearer token.
    const gateCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/gate-notes"));
    expect(gateCall).toBeTruthy();
    const init = gateCall?.[1] as RequestInit | undefined;
    const headers = new Headers(init?.headers);
    expect(headers.get("authorization")).toBe("Bearer ditto_ms_abc");
  });
});

describe("Ditto account link", () => {
  const session = {
    token: "ditto_ms_abc",
    hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    scopes: ["read", "profile"],
    expiresAt: "2099-01-01T00:00:00.000Z",
  };
  const me = {
    session: {
      miner_hotkey: session.hotkey,
      scopes: session.scopes,
      expires_at: session.expiresAt,
      expires_in: 3600,
    },
    profile: {},
    profile_url: "/miner/" + session.hotkey,
    commands: [],
  };

  async function signIn(): Promise<void> {
    localStorage.setItem("ditto.miner.session.v1", JSON.stringify(session));
    const { setMinerSession } = await import("../stores/sessionStore");
    setMinerSession(session);
  }

  it("offers Sign in with Ditto and sends the browser to the consent URL", async () => {
    await signIn();
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...location,
      origin: "https://dittobench.ai",
      pathname: "/",
      search: "",
      hash: "#/reviews",
      href: "https://dittobench.ai/#/reviews",
      assign,
    });
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url.endsWith("/me")) return Response.json(me);
        if (url.endsWith("/me/ditto-link") && (!init?.method || init.method === "GET"))
          return Response.json({ enabled: true, link: null });
        if (url.endsWith("/me/ditto-link/start"))
          return Response.json({
            attempt_id: "00000000-0000-4000-8000-000000000001",
            authorize_url: "https://api.heyditto.ai/authorize?client_id=dittobench&state=s",
            expires_in: 600,
          });
        return new Response("[]", { status: 200 });
      }),
    );
    render(() => <ReviewsPage />);
    const card = await waitFor(() => {
      const found = document.querySelector('[data-testid="ditto-account-card"]');
      expect(found).toBeTruthy();
      return found as HTMLElement;
    });
    const button = Array.from(card.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Sign in with Ditto"),
    );
    expect(button).toBeTruthy();
    fireEvent.click(button!);
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(
        "https://api.heyditto.ai/authorize?client_id=dittobench&state=s",
      );
    });
    const start = calls.find((c) => c.url.endsWith("/me/ditto-link/start"));
    expect(start?.init?.method).toBe("POST");
    const body = JSON.parse(String(start?.init?.body)) as { client: string; return_to: string };
    expect(body.client).toBe("dashboard");
    expect(body.return_to).toBe("https://dittobench.ai/#/reviews");
    // Only the session bearer travels; the page never names a Ditto user.
    expect(new Headers(start?.init?.headers).get("authorization")).toBe("Bearer ditto_ms_abc");
  });

  it("asks the miner to confirm the pairing, then shows the link and can unlink", async () => {
    await signIn();
    history.replaceState(null, "", "/#/reviews?ditto=confirm&attempt=att-1");
    syncFromLocation();
    let linked: { ditto_user_id: string; ditto_email: string; linked_via: string } | null = null;
    const confirms: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/me")) return Response.json(me);
        if (url.endsWith("/me/ditto-link/attempts/att-1/confirm") && init?.method === "POST") {
          confirms.push(new Headers(init.headers).get("authorization") || "");
          linked = {
            ditto_user_id: "ditto-user-1",
            ditto_email: "miner@example.com",
            linked_via: "dashboard",
          };
          return Response.json({ attempt_id: "att-1", status: "linked", link: linked });
        }
        if (url.endsWith("/me/ditto-link/attempts/att-1"))
          return Response.json({
            attempt_id: "att-1",
            status: linked ? "linked" : "authenticated",
            ditto_user_id: "ditto-user-1",
            ditto_email: "miner@example.com",
            miner_hotkey: session.hotkey,
          });
        if (url.endsWith("/me/ditto-link") && init?.method === "DELETE") {
          linked = null;
          return new Response(null, { status: 204 });
        }
        if (url.endsWith("/me/ditto-link"))
          return Response.json({
            enabled: true,
            link: linked
              ? { miner_hotkey: session.hotkey, created_at: "2026-09-12T12:00:00Z", ...linked }
              : null,
          });
        return new Response("[]", { status: 200 });
      }),
    );
    render(() => <ReviewsPage />);
    const confirmCard = await waitFor(() => {
      const found = document.querySelector('[data-testid="ditto-link-confirm"]');
      expect(found).toBeTruthy();
      return found as HTMLElement;
    });
    expect(confirmCard.textContent).toContain("miner@example.com");
    expect(confirmCard.textContent).toContain(session.hotkey);
    // Nothing is linked until the miner confirms.
    expect(document.body.textContent).not.toContain("Unlink Ditto account");
    const confirm = Array.from(confirmCard.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Confirm link"),
    );
    fireEvent.click(confirm!);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Ditto account linked.");
      expect(document.body.textContent).toContain("Unlink Ditto account");
    });
    expect(confirms).toEqual(["Bearer ditto_ms_abc"]);
    expect(location.hash).not.toContain("attempt=");
    const unlink = Array.from(document.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Unlink Ditto account"),
    );
    fireEvent.click(unlink!);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Ditto account unlinked.");
      expect(document.body.textContent).toContain("Sign in with Ditto");
    });
  });

  it("explains when linking is not enabled on this deployment", async () => {
    await signIn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/me")) return Response.json(me);
        if (url.endsWith("/me/ditto-link")) return Response.json({ enabled: false, link: null });
        return new Response("[]", { status: 200 });
      }),
    );
    render(() => <ReviewsPage />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Linking is not enabled on this deployment yet.");
    });
  });
});
