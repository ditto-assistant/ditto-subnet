// Tests for the entity modal shell: opens from the routeStore's entityRoute,
// overlay vs dedicated /agent/{id} page modes (role swap + body.entity-page,
// showModal 5980–6004), close semantics via closeEntityRoute + Escape
// (closeModal 6338–6367), focus moved into the dialog, and the three tenant
// bodies (miner summary, validator summary, agent evidence skeleton).
// Also guards the shell slice of assert-inventory row 26
// (test_validator_names_remain_optional_untrusted_decoration): the validator
// display name is optional untrusted decoration — rendered as text, never
// markup — and the hotkey stays the anchor identity; and row 30's #648 slice:
// a cold agent link resolves through /public/agent/{id}/summary (never the
// activity feed), the loading state shows for overlay routes too, and the deep
// history starts concurrently without blocking the summary paint.
import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rankEntries } from "../lib/scoring";
import { queryClient } from "../data/queryClient";
import { syncFromLocation } from "../stores/routeStore";
import { FIXTURE_TOP_AGENT_ID, installFixtureFetch, loadFixture } from "../test-fixtures";
import type { OperationsPayload } from "../types/fleet";
import type { LeaderboardPayload } from "../types/leaderboard";
import { EntityPanel } from "./EntityPanel";

const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const operations = loadFixture<OperationsPayload>("operations");
const entries = rankEntries(leaderboard.entries ?? []);
const topEntry = entries[0] as (typeof entries)[number];
const validatorHotkey = String(operations.validators.validators?.[0]?.validator_hotkey);

let restoreFetch: (() => void) | null = null;

beforeEach(() => {
  queryClient.removeQueries({ queryKey: ["public", "agent"] });
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
  document.body.classList.remove("entity-page");
});

function renderPanel(names: Record<string, string> = {}): void {
  render(() => (
    <EntityPanel
      entries={() => entries}
      operations={() => operations}
      validatorNames={() => names}
      currentBench={() => 7}
      settledView={() => false}
    />
  ));
}

function visit(url: string): void {
  history.replaceState(null, "", url);
  syncFromLocation();
}

function modal(): HTMLElement {
  const el = document.getElementById("modal");
  if (!el) throw new Error("missing modal");
  return el;
}

describe("EntityPanel miner tenant", () => {
  it("opens the overlay dialog from a hash-query miner route", () => {
    renderPanel();
    visit("/#/overview?miner=" + topEntry.miner_hotkey);
    expect(modal().classList.contains("open")).toBe(true);
    expect(modal()).toHaveAttribute("role", "dialog");
    expect(modal()).toHaveAttribute("aria-modal", "true");
    expect(modal()).toHaveAttribute("aria-hidden", "false");
    expect(document.getElementById("modal-back")?.classList.contains("open")).toBe(true);
    expect(document.getElementById("d-title")).toHaveTextContent("Raw score rank #1");
    // The hotkey is an entity anchor plus the copy control.
    const anchor = document.querySelector('#d-hotkey a[data-entity-link="miner"]');
    expect(anchor).toHaveTextContent(topEntry.miner_hotkey);
    expect(document.getElementById("d-hotkey-copy")).toHaveAttribute(
      "data-key",
      topEntry.miner_hotkey,
    );
    // Focus lands on the close control for keyboard/AT users.
    expect(document.activeElement).toBe(document.getElementById("modal-close"));
  });

  it("summarizes the run and links the full page", () => {
    renderPanel();
    visit("/#/overview?miner=" + topEntry.miner_hotkey);
    expect(document.getElementById("d-bench")).toHaveTextContent("DittoBench v7");
    expect(document.getElementById("d-stats")?.textContent).toContain("Best-scoring agent");
    expect(document.getElementById("d-stats")?.textContent).toContain("Current leaderboard score");
    const openFull = document.getElementById("d-open-full");
    expect(openFull).toHaveAttribute("href", "/miner/" + topEntry.miner_hotkey);
    expect(document.getElementById("d-stats")?.classList.contains("pipeline-mode")).toBe(false);
  });

  it("closes on Escape, returning to the page under the overlay", async () => {
    renderPanel();
    visit("/#/overview?miner=" + topEntry.miner_hotkey);
    expect(modal().classList.contains("open")).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await waitFor(() => expect(modal().classList.contains("open")).toBe(false));
    expect(location.hash).toBe("#/overview");
    expect(modal()).toHaveAttribute("aria-hidden", "true");
  });

  it("loads /h/{handle} through the public miner profile", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (raw.includes("/public/miners/jupiter")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
              name_handle: { stem: "jupiter", status: "reserved" },
              avatar_url:
                "/api/v1/public/miners/5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY/avatar",
              profile: { x_url: "https://x.com/jupiter", github_url: null, discord_handle: null },
              submissions: [],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      return original(input, init);
    }) as typeof fetch;
    renderPanel();
    visit("/h/jupiter");
    await waitFor(() => {
      expect(document.getElementById("d-title")).toHaveTextContent("jupiter");
    });
    expect(document.getElementById("d-stats")?.textContent).toContain("X");
    expect(document.querySelector("#d-hotkey a")?.textContent).toContain(
      "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    );
  });
});

describe("EntityPanel validator tenant (row 26 shell slice)", () => {
  it("titles the dialog with the hotkey identity when no display name exists", () => {
    renderPanel();
    visit("/#/operations?validator=" + validatorHotkey);
    expect(modal().classList.contains("open")).toBe(true);
    expect(document.getElementById("d-title")).toHaveTextContent("Validator");
    const anchor = document.querySelector('#d-hotkey a[data-entity-link="validator"]');
    expect(anchor).toHaveTextContent(validatorHotkey);
    // Validators have no dedicated full page; the action is hidden.
    const openFull = document.getElementById("d-open-full") as HTMLElement;
    expect(openFull.style.display).toBe("none");
  });

  it("treats display names as escaped, optional decoration over the hotkey identity", () => {
    const hostile = '<img src=x onerror="alert(1)"> & "Validator" <b>Name</b>';
    renderPanel({ [validatorHotkey]: hostile });
    visit("/#/operations?validator=" + validatorHotkey);
    const title = document.getElementById("d-title");
    // Rendered as text, byte for byte — never parsed as markup.
    expect(title?.textContent).toBe(hostile);
    expect(title?.querySelector("img")).toBeNull();
    expect(title?.querySelector("b")).toBeNull();
    // The hotkey stays the anchor identity regardless of the name.
    const anchor = document.querySelector('#d-hotkey a[data-entity-link="validator"]');
    expect(anchor).toHaveTextContent(validatorHotkey);
  });

  it("renders the signed-report summary from the heartbeat", () => {
    renderPanel();
    visit("/#/operations?validator=" + validatorHotkey);
    const stats = document.getElementById("d-stats");
    expect(stats?.textContent).toContain("Fleet status");
    expect(stats?.textContent).toContain("Worker state");
    expect(stats?.textContent).toContain("Heartbeat protocol");
    expect(stats?.textContent).toContain("Slots");
    expect(stats?.classList.contains("pipeline-mode")).toBe(true);
  });

  it("normalizes a validator route onto the operations page", async () => {
    renderPanel();
    visit("/#/overview?validator=" + validatorHotkey);
    await waitFor(() => expect(location.hash).toBe("#/operations?validator=" + validatorHotkey));
    expect(modal().classList.contains("open")).toBe(true);
  });
});

describe("EntityPanel agent tenant", () => {
  it("resolves an agent route through the summary endpoint, not the activity feed", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    renderPanel();
    visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
    await waitFor(() => expect(modal().classList.contains("open")).toBe(true));
    await waitFor(() =>
      expect(document.getElementById("d-title")).toHaveTextContent("bolt-v7-top1"),
    );
    const paths = (): string[] => spy.mock.calls.map((call) => String(call[0]));
    await waitFor(() =>
      expect(
        paths().some((path) => path.endsWith("/public/agent/" + FIXTURE_TOP_AGENT_ID + "/summary")),
      ).toBe(true),
    );
    // #648: one addressed submission, never the global feed narrowed to it.
    expect(paths().some((path) => path.includes("/public/activity"))).toBe(false);
    // The summary paints independently while the deep history query settles.
    expect(document.querySelector("[data-agent-evidence]")).toHaveAttribute(
      "data-agent-evidence",
      FIXTURE_TOP_AGENT_ID,
    );
    for (const id of ["pipeline-current-title", "pipeline-meta-title"]) {
      expect(document.getElementById(id), id).toBeTruthy();
    }
    expect(document.querySelector("[data-agent-history]")).toBeTruthy();
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(
      paths().filter((path) =>
        path.endsWith("/public/agent/" + FIXTURE_TOP_AGENT_ID + "/pipeline"),
      ),
    ).toHaveLength(1);
    expect(document.querySelector("[data-agent-history]")?.tagName).toBe("SECTION");
    expect(document.body.textContent).not.toContain("Load details");
    spy.mockRestore();
  });

  it("does not mount per-question rows or restomp focus when a scored agent opens", async () => {
    renderPanel();
    const close = document.getElementById("modal-close") as HTMLButtonElement;
    let focuses = 0;
    close.addEventListener("focus", () => {
      focuses += 1;
    });
    visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(document.querySelectorAll("#modal details.cases").length).toBeGreaterThan(0);
    expect(document.querySelectorAll("#modal .crow")).toHaveLength(0);
    expect(document.querySelectorAll("#modal details.cgroup")).toHaveLength(0);
    const firstCases = document.querySelector("#modal details.cases") as HTMLDetailsElement;
    firstCases.open = true;
    firstCases.dispatchEvent(new Event("toggle"));
    expect(document.querySelectorAll("#modal details.cgroup").length).toBeGreaterThan(1);
    expect(document.querySelectorAll("#modal .crow")).toHaveLength(0);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(focuses).toBeLessThan(5);
  });

  it("shows the loading state while an overlay route resolves, not only a full page", async () => {
    renderPanel();
    visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
    // #648 dropped the entity.full condition: the wait is the same wait.
    expect(document.getElementById("d-stats")?.textContent).toContain(
      "Loading submission details…",
    );
    expect(document.getElementById("d-bench")).toHaveTextContent("Loading");
    await waitFor(() => expect(document.getElementById("d-title")).toHaveTextContent("bolt-v7"));
  });

  it("renders the dedicated /agent/{id} page as a main region, not a dialog", async () => {
    renderPanel();
    visit("/agent/" + FIXTURE_TOP_AGENT_ID);
    await waitFor(() => expect(modal().classList.contains("open")).toBe(true));
    await waitFor(() =>
      expect(document.getElementById("d-title")).toHaveTextContent("bolt-v7-top1"),
    );
    expect(modal()).toHaveAttribute("role", "main");
    expect(modal()).toHaveAttribute("aria-modal", "false");
    expect(modal().classList.contains("full-page")).toBe(true);
    expect(document.body.classList.contains("entity-page")).toBe(true);
    // No backdrop in full-page mode; Escape must not tear down the page.
    expect(document.getElementById("modal-back")?.classList.contains("open")).toBe(false);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(modal().classList.contains("open")).toBe(true);
  });

  // #648 dropped the "could not be found" branch: the summary endpoint 404s
  // for an unknown id, and a 404 is not distinguishable here from any other
  // failed read — both mean the card cannot be painted right now.
  it("states plainly when a submission's details cannot be read", async () => {
    renderPanel();
    visit("/agent/ffffffff-0000-0000-0000-000000000000");
    await waitFor(() =>
      expect(document.getElementById("d-stats")?.textContent).toContain(
        "Submission details are temporarily unavailable. Try refreshing in a moment.",
      ),
    );
    expect(document.getElementById("d-stats")?.textContent).not.toContain("could not be found");
    expect(document.getElementById("d-bench")).toHaveTextContent("Unavailable");
  });
});
