// Parity tests for the submissions page (assert-inventory rows 10, 11, 12,
// 13, 14, 21, 23, 25, and row 30's deferred-history slice from #648). The old
// suite grepped the monolith's source; these
// render the SolidJS port against the recorded fixtures (frozen clock
// 2026-07-31T14:00Z, the golden renderer's instant) and assert the same
// contracts on the DOM, the store, and the stylesheet. Each block keeps the
// old test's docstring rationale as a comment. The pure vocabulary slices of
// rows 10/11 (status whitelist, stage labels, score-floor attribution) live
// beside their module in src/components/pipeline/status.test.ts; endpoint
// path shapes live in src/lib/api.test.ts.
import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { RescreenNotice } from "../components/operations/PipelineBoard";
import { queryClient } from "../data/queryClient";
import { AgentEvidence } from "../components/evidence/AgentEvidence";
import {
  ScreeningDispute,
  disputeSigningCommand,
  disputeSigningMessage,
  shellQuote,
} from "../components/evidence/DisputeForm";
import { ScreeningReview } from "../components/evidence/ScreeningReview";
import { validationAttemptView } from "../components/evidence/labels";
import { reviewPacket } from "../components/evidence/review-packet";
import { createActivityStore } from "../components/pipeline/activity-store";
import { syncFromLocation } from "../stores/routeStore";
import { FIXTURE_TOP_AGENT_ID, installFixtureFetch, loadFixture } from "../test-fixtures";
import type { ActivityPayload, AgentSummaryPayload } from "../types/pipeline";
import type { ScreeningAttempt, ValidationAttempt } from "../types/pipeline";
import { SubmissionsPage } from "./SubmissionsPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const STYLES = join(HERE, "..", "styles");
const SUBMISSIONS_CSS = readFileSync(join(STYLES, "pages", "submissions.css"), "utf-8");
const WIDGETS_CSS = readFileSync(join(STYLES, "widgets.css"), "utf-8");
const BASE_CSS = readFileSync(join(STYLES, "base.css"), "utf-8");

const activity = loadFixture<ActivityPayload>("activity");

let restoreFetch: (() => void) | null = null;
let fetchSpy: ReturnType<typeof vi.spyOn> | null = null;

beforeEach(() => {
  queryClient.removeQueries({ queryKey: ["public", "agent"] });
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/submissions");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
  fetchSpy = vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  cleanup();
  fetchSpy?.mockRestore();
  fetchSpy = null;
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
});

function fetchedPaths(): string[] {
  return (fetchSpy?.mock.calls ?? []).map((call: unknown[]) => String(call[0]));
}

function activityRequests(): URLSearchParams[] {
  return fetchedPaths()
    .filter((path) => path.includes("/public/activity"))
    .map((path) => new URLSearchParams(path.split("?")[1] ?? ""));
}

async function renderPage(): Promise<void> {
  render(() => <SubmissionsPage />);
  await waitFor(() =>
    expect(document.getElementById("activity-filter-summary")?.textContent).toContain(
      "927 submissions total",
    ),
  );
}

/** Serve a fixed activity payload for /public/activity (other paths 404). */
function stubActivityFetch(responder: (params: URLSearchParams) => ActivityPayload): void {
  // Unwind the spy FIRST: mockRestore reinstates the fetch it wrapped, which
  // would otherwise clobber the stub installed below.
  fetchSpy?.mockRestore();
  restoreFetch?.();
  restoreFetch = null;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fixtures.test");
    if (!url.pathname.endsWith("/public/activity")) {
      return Promise.resolve(new Response("{}", { status: 404 }));
    }
    return Promise.resolve(
      new Response(JSON.stringify(responder(url.searchParams)), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }) as typeof fetch;
  fetchSpy = vi.spyOn(globalThis, "fetch");
}

/** Keep the fixture fetch for everything except `/pipeline`, which `responder`
 * answers. Same hazard as above: mockRestore FIRST, since it reinstates the
 * fetch the spy wrapped and would otherwise clobber this wrapper. */
function stubPipelineFetch(responder: () => Promise<Response>): void {
  fetchSpy?.mockRestore();
  const fixtures = globalThis.fetch;
  const restoreFixtures = restoreFetch;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    return raw.includes("/pipeline") ? responder() : fixtures(input, init);
  }) as typeof fetch;
  fetchSpy = vi.spyOn(globalThis, "fetch");
  restoreFetch = (): void => {
    globalThis.fetch = fixtures;
    restoreFixtures?.();
  };
}

// ── Row 10: test_includes_server_backed_submission_quick_filters ────────────
// Server-side quick filters on the submissions table: status buttons build
// the query server-side, paging resets, every status has a label class, and
// the below-score-floor / operator-review explanations are public-state
// copy. (#623 renamed the review filter "Integrity review" and the
// validator filter "Waiting for validators".)
describe("server-backed quick filters (row 10)", () => {
  it("renders the five filter buttons with server counts and the drifted labels", async () => {
    await renderPage();
    const group = document.querySelector('[role="group"][aria-label="Quick submission filters"]');
    expect(group).toBeTruthy();
    const buttons = Array.from(
      (group as Element).querySelectorAll("button.activity-filter"),
      (button) => [
        button.getAttribute("data-activity-filter"),
        button.textContent?.replace(/\s+/g, " ").trim(),
        button.getAttribute("aria-pressed"),
      ],
    );
    expect(buttons).toEqual([
      ["all", "All 927", "true"],
      ["rejected", "Rejected 164", "false"],
      ["under_review", "Integrity review 53", "false"],
      ["waiting_validator", "Waiting for validators 3", "false"],
      ["queued", "Queued work 3", "false"],
      ["downloadable", "Downloadable 0", "false"],
    ]);
    // The summary is a live region; the clear affordance hides until a
    // filter is active.
    const summary = document.getElementById("activity-filter-summary");
    expect(summary).toHaveAttribute("role", "status");
    expect(summary).toHaveAttribute("aria-live", "polite");
    expect(document.getElementById("activity-clear")).toHaveProperty("hidden", true);
  });

  it("applies a filter server-side, resets paging, and flips aria-pressed", async () => {
    await renderPage();
    const rejected = document.querySelector(
      '[data-activity-filter="rejected"]',
    ) as HTMLButtonElement;
    fireEvent.click(rejected);
    await waitFor(() => expect(rejected).toHaveAttribute("aria-pressed", "true"));
    const last = activityRequests().pop() as URLSearchParams;
    expect(last.getAll("status")).toEqual(["rejected"]);
    expect(last.get("page")).toBe("1");
    expect(last.get("limit")).toBe("10");
    expect(location.hash).toBe("#/submissions?status=rejected");
    // The user-initiated load flashes "Updating submissions…" (aria-busy),
    // then the live region lands on the match count.
    await waitFor(() =>
      expect(document.getElementById("activity-filter-summary")?.textContent).toBe(
        "927 submissions match",
      ),
    );
    expect(document.getElementById("activity-clear")).toHaveProperty("hidden", false);
  });

  it("composes the downloadable filter with URL state and server requests", async () => {
    stubActivityFetch(() => ({
      ...activity,
      downloadable_count: 7,
    }));
    render(() => <SubmissionsPage />);
    await waitFor(() =>
      expect(document.querySelector('[data-activity-count="downloadable"]')?.textContent).toBe("7"),
    );
    const downloadable = document.querySelector(
      '[data-activity-filter="downloadable"]',
    ) as HTMLButtonElement;
    fireEvent.click(downloadable);
    await waitFor(() => expect(downloadable).toHaveAttribute("aria-pressed", "true"));
    const last = activityRequests().pop() as URLSearchParams;
    expect(last.get("downloadable")).toBe("true");
    expect(last.get("page")).toBe("1");
    expect(location.hash).toBe("#/submissions?downloadable=true");
  });

  it("states unavailability outright — never sample rows", async () => {
    restoreFetch?.();
    restoreFetch = null;
    globalThis.fetch = (() => Promise.reject(new Error("down"))) as typeof fetch;
    render(() => <SubmissionsPage />);
    await waitFor(() =>
      expect(document.getElementById("activity-filter-summary")?.textContent).toBe(
        "Could not load submissions. Try again.",
      ),
    );
    expect(document.querySelector(".empty-msg")?.textContent).toBe(
      "Submission activity is temporarily unavailable.",
    );
    // Counts show stated absence and the pager controls stay up, disabled.
    expect(document.querySelector('[data-activity-count="all"]')?.textContent).toBe("–");
    document.querySelectorAll("[data-activity-page]").forEach((button) => {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    });
  });

  it("says when no submissions match the active filters", async () => {
    stubActivityFetch(() => ({
      entries: [],
      status_counts: {},
      page: 1,
      total_pages: 1,
      total: 0,
    }));
    history.replaceState(null, "", "/#/submissions?status=rejected");
    syncFromLocation();
    render(() => <SubmissionsPage />);
    await waitFor(() =>
      expect(document.querySelector(".empty-msg")?.textContent).toBe(
        "No submissions match these filters. Clear filters or try a different search.",
      ),
    );
    expect(document.getElementById("activity-filter-summary")?.textContent).toBe(
      "0 submissions match",
    );
  });
});

// ── Row 11: test_score_floor_message_attributes_the_number_it_quotes ────────
// "The low-priority explanation has to be falsifiable from public data."
// The full attribution contract (canonical official_composite ordering,
// named floor holder, banned unfalsifiable phrasings) is pinned beside the
// module in src/components/pipeline/status.test.ts; this file keeps the
// page wiring under test via the table rows above.

describe("handle claim annotations", () => {
  it("renders a stricken name with the disputed badge", async () => {
    const first = activity.entries?.[0];
    stubActivityFetch(() => ({
      ...activity,
      entries: first
        ? [
            {
              ...first,
              name: "Unnamed submission",
              name_handle: {
                stem: "jupiter",
                status: "disputed",
                claim_id: "11111111-1111-1111-1111-111111111111",
              },
            },
          ]
        : [],
    }));
    render(() => <SubmissionsPage />);
    await waitFor(() => {
      expect(document.querySelector(".agent-name")?.textContent).toContain("Unnamed submission");
      expect(document.querySelector(".handle-badge.disputed")?.textContent).toContain(
        "name stricken",
      );
    });
  });
});

// ── Row 12: test_submission_filters_and_page_restore_and_sanitize_the_url ───
// Activity filter/page state lives in the hash query; legacy real-query
// filters are honored once and normalized; page numbers are validated
// (regex + safe-integer); popstate restores; sanitize passes replace, never
// push.
describe("URL filter/page restore and sanitize (row 12)", () => {
  it("restores canonical hash-query state without touching the URL", () => {
    history.replaceState(
      null,
      "",
      "/#/submissions?status=under_review,rejected&downloadable=true&q=bolt&page=3",
    );
    const store = createActivityStore();
    expect(store.restore()).toBe(false);
    expect(store.statuses()).toEqual(["under_review", "rejected"]);
    expect(store.downloadable()).toBe(true);
    expect(store.query()).toBe("bolt");
    expect(store.page()).toBe(3);
    expect(store.requestPath(store.page())).toBe(
      "/public/activity?page=3&limit=10&status=under_review&status=rejected&downloadable=true&q=bolt",
    );
  });

  it("sanitizes junk statuses, an over-long q, and an invalid page — with a replace", () => {
    const longQ = "x".repeat(201);
    history.replaceState(
      null,
      "",
      "/#/submissions?status=bogus,rejected,rejected&downloadable=maybe&q=" + longQ + "&page=007",
    );
    const store = createActivityStore();
    const pushSpy = vi.spyOn(history, "pushState");
    const replaceSpy = vi.spyOn(history, "replaceState");
    expect(store.restore()).toBe(true);
    expect(store.statuses()).toEqual(["rejected"]);
    expect(store.downloadable()).toBe(false);
    expect(store.query()).toBe("x".repeat(200));
    // /^[1-9][0-9]*$/ rejects "007"; junk pages restore to 1.
    expect(store.page()).toBe(1);
    store.write(false);
    // Sanitize passes never mint history entries.
    expect(pushSpy).not.toHaveBeenCalled();
    expect(replaceSpy).toHaveBeenCalled();
    // Param order follows the URL's surviving keys (q predates the re-added
    // status), exactly as the monolith's delete-then-append write behaves.
    expect(location.hash).toBe("#/submissions?q=" + "x".repeat(200) + "&status=rejected");
    pushSpy.mockRestore();
    replaceSpy.mockRestore();
  });

  it("drops an explicit page=1 (and any unsafe page number) from the URL", () => {
    history.replaceState(null, "", "/#/submissions?page=1");
    const store = createActivityStore();
    expect(store.restore()).toBe(true);
    store.write(false);
    expect(location.hash).toBe("#/submissions");

    history.replaceState(null, "", "/#/submissions?page=99999999999999999999");
    const unsafe = createActivityStore();
    // Number.isSafeInteger gates the parse even when the regex matches.
    expect(unsafe.restore()).toBe(true);
    expect(unsafe.page()).toBe(1);
  });

  it("honors legacy real-query filters once and normalizes them into the hash", () => {
    history.replaceState(null, "", "/?status=rejected&page=2#/submissions");
    const store = createActivityStore();
    expect(store.restore()).toBe(true);
    expect(store.statuses()).toEqual(["rejected"]);
    expect(store.page()).toBe(2);
    store.write(false);
    // The real query carries only deploy knobs; state moved into the hash.
    expect(location.search).toBe("");
    expect(location.hash).toBe("#/submissions?status=rejected&page=2");
  });

  it("redirects an out-of-range page to the last page with a replace", async () => {
    stubActivityFetch((params) =>
      Number(params.get("page")) > 4
        ? { entries: [], status_counts: {}, page: 1, total_pages: 4, total: 40 }
        : {
            entries: activity.entries?.slice(0, 3) ?? [],
            status_counts: activity.status_counts,
            page: Number(params.get("page")),
            total_pages: 4,
            total: 40,
          },
    );
    const store = createActivityStore();
    const pushSpy = vi.spyOn(history, "pushState");
    store.load(9, null, true);
    await waitFor(() => expect(store.page()).toBe(4));
    const pages = activityRequests().map((params) => params.get("page"));
    expect(pages).toEqual(["9", "4"]);
    // The redirect is a replace: no history entry is minted for page 9.
    expect(pushSpy).not.toHaveBeenCalled();
    expect(location.hash).toBe("#/submissions?page=4");
    pushSpy.mockRestore();
  });

  it("re-derives state and refetches on popstate", async () => {
    await renderPage();
    const rejected = document.querySelector(
      '[data-activity-filter="rejected"]',
    ) as HTMLButtonElement;
    fireEvent.click(rejected);
    await waitFor(() => expect(location.hash).toBe("#/submissions?status=rejected"));
    // The browser travels back to the unfiltered URL.
    history.replaceState(null, "", "/#/submissions");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => expect(rejected).toHaveAttribute("aria-pressed", "false"));
    const last = activityRequests().pop() as URLSearchParams;
    expect(last.getAll("status")).toEqual([]);
  });
});

// ── Row 13: test_submission_filters_are_mobile_and_keyboard_accessible ──────
// 44px touch targets, aria-pressed state, and a visible focus outline on
// the activity filter buttons; on small screens the table reflows into
// stacked cards instead of keeping a sideways-scroll floor.
describe("mobile + keyboard accessible filters (row 13)", () => {
  it("keeps the 44px touch floor, full-width band, and card reflow in CSS", () => {
    expect(WIDGETS_CSS).toMatch(/\.activity-filter\s*\{\s*min-height:\s*44px;\s*\}/);
    expect(WIDGETS_CSS).toMatch(/\.activity-filter-list\s*\{\s*width:\s*100%;\s*\}/);
    expect(WIDGETS_CSS).toMatch(/\.activity-filter\[aria-pressed="true"\]/);
    expect(SUBMISSIONS_CSS).toMatch(/\.activity tbody tr\s*\{\s*display:\s*grid;/);
    expect(BASE_CSS).toMatch(/:focus-visible\s*\{\s*outline:\s*2px solid var\(--focus\)/);
  });

  it("drives selection through real buttons with aria-pressed state", async () => {
    await renderPage();
    const queued = document.querySelector('[data-activity-filter="queued"]') as HTMLButtonElement;
    expect(queued.tagName).toBe("BUTTON");
    expect(queued).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(queued);
    await waitFor(() => expect(queued).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(queued);
    await waitFor(() => expect(queued).toHaveAttribute("aria-pressed", "false"));
  });
});

// ── Row 14: test_explains_policy_rescreen_from_public_activity_state ────────
// The notice explaining policy-version rescreens (screening backlog is
// intentional, not data loss) is derived from public activity state alone.
// The chip vocabulary — including #623's build-only silence — is pinned in
// src/components/pipeline/status.test.ts.
describe("policy-rescreen explainer (row 14)", () => {
  const rescreens = [
    {
      status: "waiting_screening",
      screening_policy_version: 3,
      required_screening_policy_version: 5,
      score_count: 3,
    },
    {
      status: "screening",
      screening_policy_version: 4,
      required_screening_policy_version: 5,
      score_count: 0,
    },
    // A first-time screening (no completed prior policy) is not a rescreen.
    {
      status: "waiting_screening",
      screening_policy_version: 0,
      required_screening_policy_version: 5,
      score_count: 0,
    },
    // Already screened under the current policy: not queued for rescreen.
    {
      status: "scored",
      screening_policy_version: 5,
      required_screening_policy_version: 5,
      score_count: 3,
    },
  ];

  it("derives the notice from queue state and preserves-scores copy", () => {
    render(() => <RescreenNotice entries={rescreens} unavailable={false} />);
    const notice = document.getElementById("rescreen-notice") as HTMLElement;
    expect(notice.hidden).toBe(false);
    expect(document.getElementById("rescreen-title")?.textContent).toBe(
      "Policy v5 rescreen in progress",
    );
    expect(document.getElementById("rescreen-policy")?.textContent).toBe("LIVE API STATE");
    const copy = document.getElementById("rescreen-copy")?.textContent ?? "";
    expect(copy).toContain("Prior scores remain preserved");
    expect(copy).toContain("validators may intentionally idle");
    expect(copy).toContain("This is not data loss");
    expect(document.getElementById("rescreen-count")?.textContent).toBe("2");
    expect(document.getElementById("rescreen-scored")?.textContent).toBe("1");
  });

  it("stays hidden with no confirmed rescreens or an unavailable feed", () => {
    render(() => <RescreenNotice entries={rescreens.slice(2)} unavailable={false} />);
    expect((document.getElementById("rescreen-notice") as HTMLElement).hidden).toBe(true);
    cleanup();
    render(() => <RescreenNotice entries={rescreens} unavailable={true} />);
    expect((document.getElementById("rescreen-notice") as HTMLElement).hidden).toBe(true);
  });
});

// ── Row 21: test_validator_progress_keeps_superseded_failures_as_history ────
// "A retried lease must render its real outcome, not its old failure."
// validator_tickets is one mutable row per (agent, version, validator): a
// reissue resets issued_at and bumps attempt_count while failure_reason /
// failed_at are preserved as an audit trail. The drawer used to let that
// preserved failure win outright, so a submission whose three validators
// each failed once and then scored rendered as three "Scoring run failed ·
// deferred" rows stamped with the superseded failure times — with three
// accepted scores sitting above them, unexplained (#459).
describe("superseded validator failures stay history (row 21, #459)", () => {
  const retried: ValidationAttempt = {
    validator_hotkey: "5CqJAjSj",
    status: "scored",
    purpose: "canonical_quorum",
    issued_at: "2026-07-25T21:26:27Z",
    deadline: "2026-07-25T23:26:27Z",
    bench_version: 7,
    actively_running: false,
    failure_reason: "scoring_error",
    failed_at: "2026-07-25T20:54:41Z",
  };

  it("reports the retried lease's real outcome, dated by the lease it describes", () => {
    const view = validationAttemptView({
      ...retried,
      ...({ attempt_count: 2 } as Partial<ValidationAttempt>),
    });
    expect(view.headline).toBe("Score submitted");
    expect(view.headline).not.toContain("deferred");
    expect(view.metaRest).toContain("submitted a score on attempt 2");
    expect(view.metaRest).toContain("an earlier attempt reported scoring run failed");
    // Dated by the lease, not the superseded failure.
    expect(view.when).toBe("2026-07-25T21:26:27Z");
    expect(view.when).not.toBe("2026-07-25T20:54:41Z");
  });

  it("keeps an unsuperseded failure as the headline, dated by the failure", () => {
    const view = validationAttemptView({
      validator_hotkey: "5CFtzzb4",
      status: "expired",
      purpose: "canonical_quorum",
      issued_at: "2026-07-25T20:00:00Z",
      deadline: "2026-07-25T22:00:00Z",
      bench_version: 7,
      actively_running: false,
      failure_reason: "infrastructure",
      failed_at: "2026-07-25T21:00:00Z",
    });
    expect(view.headline).toBe("Validator infrastructure failure · deferred");
    expect(view.metaRest).toContain("reported validator infrastructure failure");
    expect(view.metaRest).not.toContain("an earlier attempt");
    expect(view.when).toBe("2026-07-25T21:00:00Z");
  });

  it("makes inference allowance exhaustion terminal and explicit", () => {
    const view = validationAttemptView({
      validator_hotkey: "5CFtzzb4",
      status: "expired",
      purpose: "canonical_quorum",
      issued_at: "2026-07-25T20:00:00Z",
      deadline: "2026-07-25T22:00:00Z",
      bench_version: 8,
      actively_running: false,
      failure_reason: "scoring_error",
      failure_code: "inference_allowance_exhausted",
      failed_at: "2026-07-25T21:00:00Z",
    });
    expect(view.headline).toBe("Inference allowance exhausted");
    expect(view.headline).not.toContain("deferred");
    expect(view.tone).toBe("bad");
    expect(view.metaRest).toContain("exceeded the run inference allowance");
    expect(view.retryTip).toContain("not validator infrastructure");
    expect(view.retryTip).toContain("does not receive an automatic infrastructure retry");
  });

  it("makes a pre-reservation refusal terminal without calling it a spent grant", () => {
    const view = validationAttemptView({
      validator_hotkey: "5CFtzzb4",
      status: "expired",
      purpose: "canonical_quorum",
      issued_at: "2026-07-25T20:00:00Z",
      deadline: "2026-07-25T22:00:00Z",
      bench_version: 11,
      actively_running: false,
      failure_reason: "scoring_error",
      failure_code: "inference_request_rejected",
      failed_at: "2026-07-25T21:00:00Z",
    });
    expect(view.headline).toBe("Inference request rejected");
    expect(view.headline).not.toContain("deferred");
    expect(view.headline).not.toContain("allowance");
    expect(view.tone).toBe("bad");
    expect(view.metaRest).toContain("request refused before reservation");
    expect(view.retryTip).toContain("not a spent grant");
    expect(view.retryTip).toContain("does not receive an automatic infrastructure retry");
  });

  it("keeps a clean first-attempt score plainly worded", () => {
    const view = validationAttemptView({
      validator_hotkey: "5HmP9732",
      status: "scored",
      purpose: "canonical_quorum",
      issued_at: "2026-07-25T20:56:19Z",
      deadline: "2026-07-25T22:56:19Z",
      bench_version: 7,
      actively_running: false,
      failure_reason: null,
      failed_at: null,
    });
    expect(view.headline).toBe("Score submitted");
    expect(view.metaRest).not.toContain("on attempt");
    expect(view.headline + view.metaRest).not.toContain("deferred");
  });
});

// ── Row 23: test_includes_public_terminal_screening_review_cards ────────────
// The public terminal-screening rejection card: findings, source locations,
// policy observations — digest-verified, with no source text or private
// challenge data.
describe("terminal screening review cards (row 23)", () => {
  const attempt: ScreeningAttempt = {
    status: "rejected",
    policy_version: 9,
    screener_hotkey: "5Screener",
    review_finding: {
      summary: "Served responses are replayed from a lookup table.",
      confidence: 0.97,
      categories: ["policy_finding-replay"],
      locations: [{ path: "agent/main.py", line: 42, category: "replay-cache" }],
      reviewer_revision: "r14",
    },
    review_evidence: [{ code: "static_answer_map", summary: "Constant answers by question id." }],
  };

  it("renders the rejection card with findings, locations, and observations", () => {
    render(() => <ScreeningReview attempt={attempt} />);
    const card = document.querySelector('[aria-label="Detailed screening rejection"]');
    expect(card).toBeTruthy();
    expect(card?.querySelector(".screening-review-title")?.textContent).toBe(
      "Why this submission was rejected",
    );
    expect(card?.querySelector(".screening-review-verdict")?.textContent).toBe("97% confidence");
    expect(card?.textContent).toContain("Source locations in the served path");
    expect(card?.querySelector(".screening-review-location code")?.textContent).toBe(
      "agent/main.py:42",
    );
    expect(card?.textContent).toContain("Policy observations");
    expect(card?.textContent).toContain("Static Answer Map.");
    expect(card?.textContent).toContain(
      "Digest-verified public review · no source text or private challenge data",
    );
    expect(card?.textContent).toContain("reviewer r14");
  });

  it("renders nothing without a finding or evidence, and keeps the card CSS", () => {
    render(() => <ScreeningReview attempt={{ status: "rejected", policy_version: 9 }} />);
    expect(document.querySelector(".screening-review")).toBeNull();
    expect(SUBMISSIONS_CSS).toContain(".screening-review-location code");
    expect(SUBMISSIONS_CSS).toContain("grid-column: 1 / -1");
  });
});

// ── Row 25: test_includes_miner_facing_review_details_copy ──────────────────
// The one-click "review packet" text block miners paste when asking for a
// review: agent id, name/version, hotkey, status, artifact SHA, canonical
// URL. (The monolith carried the button in two places — the agent drawer
// and the miner run modal; the SPA's drawer instance is asserted here.)
describe("miner-facing review packet (row 25)", () => {
  const entry = {
    agent_id: "8a5d27ee-6ed4-45d4-aaa4-e73957eb8217",
    name: "kabaw\nv76b",
    version: 1,
    miner_hotkey: "5FZ9wU4cE8WjPHusDe2GfFU6NAWJaZZgpDHDruSRDmaf7oDU",
    status: "rejected",
    artifact_sha256: "ab".repeat(32),
  };

  it("assembles the paste-ready packet with the canonical URL", () => {
    const packet = reviewPacket(entry);
    const lines = packet.split("\n");
    expect(lines[0]).toBe("Please review agent 8a5d27ee-6ed4-45d4-aaa4-e73957eb8217");
    // Newlines inside values are flattened — one field per line, always.
    expect(lines[1]).toBe("Name: kabaw v76b (Submission v1)");
    expect(lines[2]).toBe("Miner hotkey: 5FZ9wU4cE8WjPHusDe2GfFU6NAWJaZZgpDHDruSRDmaf7oDU");
    expect(lines[3]).toBe("Status: rejected");
    expect(lines[4]).toBe("Artifact SHA-256: " + "ab".repeat(32));
    expect(lines[5]).toBe(
      "URL: " + location.origin + "/agent/8a5d27ee-6ed4-45d4-aaa4-e73957eb8217",
    );
  });

  it("mounts the copy control in the drawer with the packet as its payload", async () => {
    const top = (activity.entries ?? []).find((e) => e.agent_id === FIXTURE_TOP_AGENT_ID);
    expect(top).toBeTruthy();
    render(() => <AgentEvidence entry={top as NonNullable<typeof top>} />);
    await waitFor(() => {
      const button = document.querySelector("button.copy.review-copy");
      expect(button).toBeTruthy();
      expect(button).toHaveAttribute("aria-label", "Copy review details");
      expect(button).toHaveAttribute("data-key", reviewPacket(top as NonNullable<typeof top>));
      expect(button?.textContent).toBe("Copy review details");
    });
    expect(WIDGETS_CSS).toMatch(/\.review-copy\s*\{\s*width:\s*100%;/);
  });
});

// ── The screening dispute form (row 1's dispute slice; #278 flow) ───────────
// A rejected submission may file exactly one private dispute, signed
// locally with btcli; wallet details stay in the browser and are not
// submitted — only the message and the 128-hex hotkey signature go to the
// API (postJSON /public/agent/{id}/dispute).
describe("screening dispute form", () => {
  const AGENT = "8a5d27ee-6ed4-45d4-aaa4-e73957eb8217";

  it("derives the ditto-dispute-v1 signing payload and a quoted btcli command", async () => {
    if (!globalThis.crypto?.subtle) {
      (globalThis as { crypto?: Crypto }).crypto = webcrypto as unknown as Crypto;
    }
    const payload = await disputeSigningMessage(AGENT, "the screener misread my cache layer");
    expect(payload).toMatch(new RegExp("^ditto-dispute-v1:" + AGENT + ":[0-9a-f]{64}$"));
    const command = disputeSigningCommand("my wallet", "mine'r", payload);
    expect(command).toBe(
      "btcli wallet sign --wallet-name 'my wallet' --wallet-hotkey 'mine'\"'\"'r'" +
        " --use-hotkey --message '" +
        payload +
        "' --json-output",
    );
    expect(shellQuote("a'b")).toBe("'a'\"'\"'b'");
  });

  it("gates submission on message length and the 128-hex signature, then posts", async () => {
    render(() => (
      <ScreeningDispute agentId={AGENT} status="rejected" dispute={null} onSubmitted={() => {}} />
    ));
    const form = document.getElementById("screening-dispute-form") as HTMLFormElement;
    expect(form).toBeTruthy();
    const message = document.getElementById("screening-dispute-message") as HTMLTextAreaElement;
    const signature = document.getElementById("screening-dispute-signature") as HTMLInputElement;
    const submit = document.querySelector(".screening-dispute-submit") as HTMLButtonElement;
    expect(message).toHaveAttribute("maxlength", "1000");
    expect(signature).toHaveAttribute("maxlength", "128");
    expect(signature).toHaveAttribute("pattern", "[0-9a-fA-F]{128}");
    expect(document.body.textContent).toContain(
      "Wallet details stay in this browser and are not submitted.",
    );
    expect(submit.disabled).toBe(true);

    fireEvent.input(message, { target: { value: "the screener misread my cache layer" } });
    expect(submit.disabled).toBe(true); // still no signature
    fireEvent.input(signature, { target: { value: "a".repeat(127) } });
    expect(submit.disabled).toBe(true); // 127 hex chars is not a signature
    fireEvent.input(signature, { target: { value: "a".repeat(128) } });
    await waitFor(() => expect(submit.disabled).toBe(false));

    fireEvent.submit(form);
    await waitFor(() => {
      const posted = (fetchSpy?.mock.calls ?? []).find((call: unknown[]) =>
        String(call[0]).includes("/public/agent/" + AGENT + "/dispute"),
      );
      expect(posted).toBeTruthy();
      expect((posted?.[1] as RequestInit | undefined)?.method).toBe("POST");
    });
  });

  it("renders the one-shot outcome card instead of a second form", () => {
    render(() => (
      <ScreeningDispute
        agentId={AGENT}
        status="rejected"
        dispute={{ status: "pending", submitted_at: "2026-07-31T10:00:00Z" }}
      />
    ));
    expect(document.getElementById("pipeline-dispute-title")?.textContent).toBe(
      "Dispute awaiting review",
    );
    expect(document.getElementById("screening-dispute-form")).toBeNull();
  });
});

// ── Async agent evidence (row 30's #648 follow-up) ──────────────────────────
// The targeted summary remains the first-paint answer, while the full evidence
// record starts automatically and fills its own region. No user gesture owns a
// network request; keyed Solid Query state deduplicates remounts and exposes an
// explicit retry when the independent detail request fails.
describe("async agent evidence", () => {
  const summary = loadFixture<AgentSummaryPayload>("agent-top-summary");

  function body(): HTMLElement {
    return document.querySelector("[data-agent-history-body]") as HTMLElement;
  }

  function pipelineRequests(): string[] {
    return fetchedPaths().filter((path) => path.includes("/pipeline"));
  }

  function pipelineResponse(overrides?: Record<string, unknown>): Response {
    return new Response(
      JSON.stringify({
        ...loadFixture<Record<string, unknown>>("agent-top-pipeline"),
        ...overrides,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }

  it("paints the summary while the evidence record loads automatically", async () => {
    let resolvePipeline: ((response: Response) => void) | undefined;
    stubPipelineFetch(
      () =>
        new Promise<Response>((resolve) => {
          resolvePipeline = resolve;
        }),
    );
    render(() => <AgentEvidence entry={summary} />);
    await waitFor(() => expect(document.getElementById("pipeline-current-title")).toBeTruthy());
    expect(document.getElementById("pipeline-meta-title")).toBeTruthy();
    expect(pipelineRequests()).toHaveLength(1);
    expect(body()).toHaveAttribute("aria-busy", "true");
    expect(body().textContent).toContain("Loading evidence record…");
    expect(document.querySelector(".pipeline-history-skeleton")).toBeTruthy();
    expect(document.querySelector(".pipeline-section-loading")).toBeTruthy();
    expect(document.querySelector("[data-agent-history]")?.tagName).toBe("SECTION");
    expect(document.body.textContent).not.toContain("Load details");

    resolvePipeline?.(pipelineResponse());
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(body()).not.toHaveAttribute("aria-busy");
  });

  it("deduplicates the keyed record across an unmount and remount", async () => {
    render(() => <AgentEvidence entry={summary} />);
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(pipelineRequests()).toHaveLength(1);
    expect(pipelineRequests()[0]).toContain("/public/agent/" + FIXTURE_TOP_AGENT_ID + "/pipeline");

    cleanup();
    render(() => <AgentEvidence entry={summary} />);
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(document.getElementById("pipeline-accepted-scores")).toBeTruthy();
    expect(pipelineRequests()).toHaveLength(1);
  });

  it("groups run cost, compresses allowance usage, and closes expired active grants", async () => {
    stubPipelineFetch(() =>
      Promise.resolve(
        pipelineResponse({
          inference_runs: [
            {
              validator_hotkey: "5CFtzzb4mS118",
              bench_version: 8,
              ticket_deadline: "2026-07-31T14:30:00Z",
              status: "exhausted",
              request_budget: 8192,
              requests: 8192,
              prompt_tokens: 7_000_000,
              completion_tokens: 500_000,
              token_budget: 25_000_000,
              embedding_requests: 123,
              embedding_tokens: 456_789,
              cost_microusd: 1_246_912,
              accounting_version: 2,
              created_at: "2026-07-31T13:00:00Z",
              updated_at: "2026-07-31T13:55:00Z",
            },
            {
              validator_hotkey: "5GrwvaEFother",
              bench_version: 8,
              ticket_deadline: "2026-07-31T13:30:00Z",
              status: "active",
              request_budget: 8192,
              requests: 400,
              prompt_tokens: 1_000_000,
              completion_tokens: 100_000,
              token_budget: 25_000_000,
              embedding_requests: 0,
              embedding_tokens: 0,
              cost_microusd: 250_000,
              accounting_version: 2,
              created_at: "2026-07-31T12:00:00Z",
              updated_at: "2026-07-31T12:25:00Z",
            },
          ],
        }),
      ),
    );

    render(() => <AgentEvidence entry={summary} />);

    await waitFor(() => expect(document.getElementById("pipeline-inference-spend")).toBeTruthy());
    const section = document
      .getElementById("pipeline-inference-spend")
      ?.closest("section") as HTMLElement;
    expect(section.textContent).toContain("$1.50 total");
    expect(section.textContent).toContain("$0.7485 average · 2 runs");
    expect(section.querySelectorAll(".inference-run")).toHaveLength(2);
    expect(section.textContent).toContain("$1.25");
    expect(section.textContent).toContain("$0.2500");
    expect(section.textContent).toContain("Chat 100%");
    expect(section.textContent).toContain("Tokens 30%");
    expect(section.textContent).toContain("Embed 123 / 456.8K tok");
    expect(section.textContent).toContain("Allowance exhausted");
    expect(section.textContent).toContain("Closed");
    expect(section.textContent).not.toContain("Running");
  });

  it("isolates a detail failure and retries from the section", async () => {
    let attempts = 0;
    stubPipelineFetch(() => {
      attempts += 1;
      return attempts < 3 ? Promise.reject(new Error("down")) : Promise.resolve(pipelineResponse());
    });
    render(() => <AgentEvidence entry={summary} />);
    await waitFor(() => expect(document.getElementById("pipeline-current-title")).toBeTruthy());
    await waitFor(() =>
      expect(body().textContent).toContain("Detailed history is temporarily unavailable."),
    );
    expect(body()).not.toHaveAttribute("aria-busy");
    expect(pipelineRequests()).toHaveLength(2); // initial read + one transient retry
    fireEvent.click(body().querySelector("button") as HTMLButtonElement);
    await waitFor(() => expect(document.getElementById("pipeline-validator-history")).toBeTruthy());
    expect(pipelineRequests()).toHaveLength(3);
  });

  it("keeps summary facts visible while the independent record is pending", async () => {
    const running = {
      ...summary,
      score_count: 1,
      score_composite: 0.311,
      active_benchmarks: [
        {
          stage: "running_benchmark",
          percent: 42,
          bench_version: 7,
          started_at: "2026-07-31T13:40:00Z",
          completed_checks: 8,
          total_checks: 20,
        },
      ],
    } satisfies AgentSummaryPayload;
    const pendingProps = {
      pipeline: () => undefined,
      pipelineLoading: () => true,
      pipelineFetching: () => true,
      pipelineError: () => null,
      retryPipeline: () => undefined,
    };
    render(() => <AgentEvidence entry={running} {...pendingProps} />);
    await waitFor(() => expect(document.getElementById("pipeline-current-title")).toBeTruthy());
    const facts = document.querySelector(".pipeline-key-facts") as HTMLElement;
    // Below quorum the median is labelled for what it is.
    expect(facts.textContent).toContain("Preliminary median");
    expect(facts.textContent).toContain("0.311");
    expect(facts.textContent).not.toContain("Canonical median");
    expect(document.querySelector(".pipeline-current .benchmark-progress")).toBeTruthy();
    cleanup();

    render(() => <AgentEvidence entry={summary} {...pendingProps} />);
    await waitFor(() => expect(document.getElementById("pipeline-current-title")).toBeTruthy());
    const atQuorum = document.querySelector(".pipeline-key-facts") as HTMLElement;
    expect(atQuorum.textContent).toContain("Canonical median");
    expect(atQuorum.textContent).toContain("0.373");
  });

  it("prefers the loaded record's artifact release, falling back to the entry's", async () => {
    // #648 put artifact_release on the pipeline payload; the entry's copy is
    // what paints until the record lands, and the record wins once it has.
    let resolvePipeline: ((response: Response) => void) | undefined;
    stubPipelineFetch(
      () =>
        new Promise<Response>((resolve) => {
          resolvePipeline = resolve;
        }),
    );
    render(() => (
      <AgentEvidence entry={{ ...summary, artifact_release: { status: "awaiting_quorum" } }} />
    ));
    await waitFor(() => expect(document.getElementById("pipeline-current-title")).toBeTruthy());
    expect(document.querySelector(".artifact-release-card")?.textContent).toContain("Awaiting 3/3");
    resolvePipeline?.(
      pipelineResponse({ artifact_release: { status: "available", embargo_hours: 48 } }),
    );
    await waitFor(() =>
      expect(document.querySelector(".artifact-release-card")?.textContent).toContain(
        "Source public",
      ),
    );
  });
});

// ── Weekend drift #622/#636 in the activity table ────────────────────────────
// #622: the stage cell leads with the CURRENT review reason under its event
// label, keeping the initial hold as labeled history. #636: past the
// opening event the duplicate note reads "Initial comparison" — the
// mechanical claim stays out of the current-evidence channel.
describe("review-event evidence in the table (#622/#636)", () => {
  it("renders the current reason, the initial hold, and the initial comparison", async () => {
    const base = (activity.entries ?? [])[0] as Record<string, unknown>;
    stubActivityFetch(() => ({
      entries: [
        {
          ...base,
          status: "under_review",
          review_event: "reopened",
          review_reason: "manual re-check of tool-call provenance",
          review_original_reason: "content near-duplicate of agent abc",
          duplicate_of: "2d64b84b-d5bf-4c87-ae62-ed0668b1023e",
          duplicate_name: "granite",
          duplicate_version: 3,
        },
      ],
      status_counts: { under_review: 1 },
      page: 1,
      total_pages: 1,
      total: 1,
    }));
    render(() => <SubmissionsPage />);
    await waitFor(() => expect(document.querySelector(".stage-cell .stage")).toBeTruthy());
    const cell = document.querySelector(".stage-cell") as HTMLElement;
    expect(cell.querySelector(".stage")?.textContent).toBe("Source integrity review");
    const notes = Array.from(cell.querySelectorAll(".stage-note"), (note) => note.textContent);
    expect(notes[0]).toBe("Review reopened: manual re-check of tool-call provenance");
    expect(notes[1]).toBe("Initial hold: content near-duplicate of agent abc");
    expect(notes[2]).toContain("Initial comparison:");
    expect(notes[2]).toContain("granite, Submission v3");
    expect(cell.textContent).not.toContain("Copy review:");
  });

  it("names a same-miner match as the previously rejected ancestor", async () => {
    const base = (activity.entries ?? [])[0] as Record<string, unknown>;
    stubActivityFetch(() => ({
      entries: [
        {
          ...base,
          status: "rejected",
          miner_hotkey: "5Di44xhKBhUfX7X1s411zD9WxwTWGhLECXsajkASzbtmzQWf",
          review_event: "rejected",
          review_reason:
            "Same miner, previously rejected as Zeus_v11 v6. Served /run still uses the compiler.",
          review_original_reason: "Resubmission of a rejected artifact",
          duplicate_of: "7020bd00-bd1b-42e9-90b2-34937fe3f0bd",
          duplicate_name: "Zeus_v11",
          duplicate_version: 6,
          duplicate_hotkey: "5Di44xhKBhUfX7X1s411zD9WxwTWGhLECXsajkASzbtmzQWf",
        },
      ],
      status_counts: { rejected: 1 },
      page: 1,
      total_pages: 1,
      total: 1,
    }));
    render(() => <SubmissionsPage />);
    await waitFor(() => expect(document.querySelector(".stage-cell .stage")).toBeTruthy());
    const cell = document.querySelector(".stage-cell") as HTMLElement;
    const notes = Array.from(cell.querySelectorAll(".stage-note"), (note) => note.textContent);
    expect(notes.some((note) => note?.includes("Previously rejected as"))).toBe(true);
    expect(cell.textContent).toContain("Zeus_v11, Submission v6");
    expect(cell.textContent).not.toContain("Compared with");
    expect(cell.textContent).not.toContain("Initial comparison");
  });
});
