import { cleanup, fireEvent, render, screen, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BountiesPage, alphaAmount, claimOutcome } from "./BountiesPage";
import { HTTPError, getJSON } from "../lib/api";
import { refreshAllEndpoints } from "../data/useEndpoint";
import { loadFixture } from "../test-fixtures";
import type { BountyBoardPage, BountyClaimView, BountyDetail } from "../types/bounty";

// The fixtures are the real public responses for one lifecycle, recorded and
// pinned by Platform's test_bounty_board_fixtures.py.
vi.mock("../lib/api", async (actual) => ({
  ...(await actual<typeof import("../lib/api")>()),
  getJSON: vi.fn(),
}));

const board = loadFixture<BountyBoardPage>("bounty-board");
const detail = loadFixture<BountyDetail>("bounty-detail");
const claims = loadFixture<Record<string, BountyClaimView>>("bounty-claims");
const bountyId = detail.summary.bounty_id;
const claimOf = (login: string) =>
  Object.values(claims).find((claim) => claim.github_login === login)!;

function serve(path: string): Promise<unknown> {
  const pathname = path.split("?")[0];
  if (pathname === "/public/bounties") return Promise.resolve(board);
  if (pathname === `/public/bounties/${bountyId}`) return Promise.resolve(detail);
  const claim = /^\/bounties\/claims\/([^/]+)$/.exec(pathname ?? "");
  if (claim && claims[claim[1]!]) return Promise.resolve(claims[claim[1]!]);
  return Promise.reject(new HTTPError(404));
}

beforeEach(() => {
  history.replaceState(null, "", "/bounties");
  vi.mocked(getJSON).mockImplementation(serve as typeof getJSON);
});
afterEach(() => {
  cleanup();
  vi.mocked(getJSON).mockReset();
});

async function openBounty(): Promise<void> {
  render(() => <BountiesPage />);
  fireEvent.click(await screen.findByRole("button", { name: detail.summary.title }));
  await screen.findByRole("heading", { name: "Terms" });
}

function slotRow(position: number): HTMLElement {
  return screen.getByRole("rowheader", { name: String(position) }).closest("tr")!;
}

describe("bounty board", () => {
  it("lists every published bounty with its terms and slots", async () => {
    render(() => <BountiesPage />);
    const table = await screen.findByRole("table", { name: "Published bounties" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(board.items.length);
    // Newest first, as Platform serves them.
    expect(rows.map((row) => within(row).getByRole("button").textContent)).toEqual(
      board.items.map((item) => item.title),
    );
    const row = rows[board.items.findIndex((item) => item.bounty_id === bountyId)]!;
    expect(row).toHaveTextContent("ditto-assistant/ditto-subnet#2942");
    expect(row).toHaveTextContent("2 α");
    expect(row).toHaveTextContent("100 α");
    expect(row).toHaveTextContent("40 α awarded");
    expect(row).toHaveTextContent("1 claimed · 2 closed");
    const fresh = rows.find((other) => other !== row)!;
    expect(fresh).toHaveTextContent("3 open");
    expect(within(row).getByText("Rehearsal")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("No alpha moves");
    expect(screen.getByRole("link", { name: "contributor guide" })).toHaveAttribute(
      "href",
      expect.stringContaining("docs/bounty-guide.md"),
    );
  });

  it("states a failure instead of showing a board", async () => {
    vi.mocked(getJSON).mockRejectedValue(new Error("offline"));
    render(() => <BountiesPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Bounties could not be loaded");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("hides a board a poll failed to refresh, and shows it again on the next good poll", async () => {
    render(() => <BountiesPage />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
    // A stale board would show slots open and deadlines counting that may
    // have moved, so a failed refresh states the failure instead.
    vi.mocked(getJSON).mockRejectedValue(new Error("offline"));
    refreshAllEndpoints();
    expect(await screen.findByRole("alert")).toHaveTextContent("Bounties could not be loaded");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    vi.mocked(getJSON).mockImplementation(serve as typeof getJSON);
    refreshAllEndpoints();
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("states an empty board", async () => {
    vi.mocked(getJSON).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    render(() => <BountiesPage />);
    expect(await screen.findByText("No bounties are published yet.")).toBeInTheDocument();
  });

  it("offers the first page when a later page comes back empty", async () => {
    vi.mocked(getJSON).mockImplementation(((path: string) => {
      const offset = Number(new URLSearchParams(path.split("?")[1]).get("offset"));
      return Promise.resolve(
        offset === 0
          ? { items: board.items, total: 120, limit: 50, offset }
          : { items: [], total: 120, limit: 50, offset },
      );
    }) as typeof getJSON);
    render(() => <BountiesPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findByText(/No bounties on this page/)).toBeInTheDocument();
    expect(screen.queryByText("No bounties are published yet.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "First page" }));
    expect(await screen.findByRole("table", { name: "Published bounties" })).toBeInTheDocument();
  });

  it("pages a long board", async () => {
    const item = board.items[0]!;
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      Promise.resolve({
        items: [item],
        total: 120,
        limit: 50,
        offset: Number(new URLSearchParams(path.split("?")[1]).get("offset")),
      })) as typeof getJSON);
    render(() => <BountiesPage />);
    expect(await screen.findByText("1–1 of 120")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("51–51 of 120")).toBeInTheDocument();
    expect(vi.mocked(getJSON)).toHaveBeenLastCalledWith("/public/bounties?limit=50&offset=50");
  });
});

describe("bounty detail", () => {
  it("returns to the board page a bounty was opened from", async () => {
    const item = board.items.find((candidate) => candidate.bounty_id === bountyId)!;
    vi.mocked(getJSON).mockImplementation(((path: string) => {
      const [pathname, query] = path.split("?");
      if (pathname !== "/public/bounties") return serve(path);
      const offset = Number(new URLSearchParams(query).get("offset"));
      return Promise.resolve({ items: [item], total: 120, limit: 50, offset });
    }) as typeof getJSON);
    render(() => <BountiesPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findByText("51–51 of 120")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: detail.summary.title }));
    await screen.findByRole("heading", { name: "Terms" });
    fireEvent.click(screen.getByRole("button", { name: "← All bounties" }));
    expect(await screen.findByText("51–51 of 120")).toBeInTheDocument();
  });

  it("opens from the board into a linkable detail and back", async () => {
    await openBounty();
    expect(location.pathname + location.search).toBe(`/bounties?bounty=${bountyId}`);
    expect(screen.getByRole("link", { name: "ditto-assistant/ditto-subnet#2942" })).toHaveAttribute(
      "href",
      "https://github.com/ditto-assistant/ditto-subnet/issues/2942",
    );
    fireEvent.click(screen.getByRole("button", { name: "← All bounties" }));
    await screen.findByRole("table", { name: "Published bounties" });
    expect(location.search).toBe("");
  });

  it("renders a deep link directly and follows history", async () => {
    history.replaceState(null, "", `/bounties?bounty=${bountyId}`);
    render(() => <BountiesPage />);
    await screen.findByRole("heading", { name: "Terms" });
    history.replaceState(null, "", "/bounties");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await screen.findByRole("table", { name: "Published bounties" });
  });

  it("opens an uppercase or hash-route deep link", async () => {
    for (const url of [
      `/bounties?bounty=${bountyId.toUpperCase()}`,
      `/#/bounties?bounty=${bountyId}`,
    ]) {
      history.replaceState(null, "", url);
      render(() => <BountiesPage />);
      await screen.findByRole("heading", { name: "Terms" });
      expect(vi.mocked(getJSON)).toHaveBeenCalledWith(`/public/bounties/${bountyId}`);
      cleanup();
    }
  });

  it("never requests a malformed bounty id", async () => {
    history.replaceState(null, "", "/bounties?bounty=../admin");
    render(() => <BountiesPage />);
    await screen.findByRole("table", { name: "Published bounties" });
    expect(vi.mocked(getJSON).mock.calls.map(([path]) => path)).not.toContain(
      "/public/bounties/../admin",
    );
  });

  it("states an unknown bounty", async () => {
    history.replaceState(null, "", "/bounties?bounty=00000000-0000-4000-8000-000000000000");
    render(() => <BountiesPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be loaded");
  });

  it("shows the signed terms served by the API", async () => {
    await openBounty();
    const terms = detail.revisions.at(-1)!.terms;
    expect(screen.getByText("Entry fee").nextElementSibling).toHaveTextContent("2 α");
    expect(screen.getByText("Reward pool").nextElementSibling).toHaveTextContent("100 α");
    expect(screen.getByText("Awarded").nextElementSibling).toHaveTextContent("40 α");
    expect(screen.getByText("Slots per round").nextElementSibling).toHaveTextContent(
      String(terms.attempts_per_round),
    );
    expect(screen.getByText(terms.scope)).toBeInTheDocument();
    expect(screen.getByText(terms.fee_escrow_coldkey)).toBeInTheDocument();
    expect(screen.getByText(detail.summary.spec_digest)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Fees and payments are simulated");
  });

  it("links only well-formed dependency references", async () => {
    const edited = structuredClone(detail);
    edited.revisions.at(-1)!.terms.dependencies = [
      "ditto-assistant/ditto-subnet#12",
      "owner/repo#abc",
    ];
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      path.split("?")[0] === `/public/bounties/${bountyId}`
        ? Promise.resolve(edited)
        : serve(path)) as typeof getJSON);
    await openBounty();
    expect(screen.getByText("ditto-assistant/ditto-subnet#12").closest("a")).toHaveAttribute(
      "href",
      "https://github.com/ditto-assistant/ditto-subnet/issues/12",
    );
    expect(screen.getByText("owner/repo#abc").closest("a")).toBeNull();
  });

  it("shows each slot's effective status, claimant and deadline", async () => {
    await openBounty();
    for (const slot of detail.slots) {
      const row = slotRow(slot.position);
      const label = {
        accepted: "Accepted",
        rejected: "Rejected",
        claimed: "Claimed",
        open: "Open",
      }[slot.status as "accepted" | "rejected" | "claimed" | "open"];
      expect(row, slot.status).toHaveTextContent(label);
      if (slot.claimant) expect(row).toHaveTextContent(slot.claimant.github_login);
    }
    const working = detail.slots.find((slot) => slot.status === "claimed")!;
    expect(slotRow(working.position)).toHaveTextContent("Delivery due");
  });

  it("opens the accepted claim's review, award and payments", async () => {
    await openBounty();
    const alice = claimOf("alice-dev");
    const row = slotRow(alice.position);
    fireEvent.click(within(row).getByRole("button", { name: "Claim record" }));
    const record = await within(row).findByText("Payments");
    const body = record.closest(".bounty-claim-body") as HTMLElement;
    const [submission] = alice.submissions;
    expect(
      within(body).getByRole("link", { name: `PR #${submission!.pr_number}` }),
    ).toBeInTheDocument();
    expect(body).toHaveTextContent(submission!.head_sha.slice(0, 12));
    expect(body).toHaveTextContent("Accepted");
    expect(body).toHaveTextContent(submission!.decision_reason!);
    expect(body).toHaveTextContent("Fee refund 2 α (simulated) · Confirmed");
    expect(body).toHaveTextContent("Award 40 α (simulated) · Awaiting approval (1 of 2 approvals)");
    expect(body).toHaveTextContent("Credit: Alice built the board");
    expect(body).toHaveTextContent("Merged");
  });

  it("keeps an open claim record open when the bounty refreshes", async () => {
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      serve(path).then((payload) => structuredClone(payload))) as typeof getJSON);
    await openBounty();
    const alice = claimOf("alice-dev");
    fireEvent.click(within(slotRow(alice.position)).getByRole("button", { name: "Claim record" }));
    await within(slotRow(alice.position)).findByText("Payments");
    refreshAllEndpoints();
    await vi.waitFor(() => expect(vi.mocked(getJSON).mock.calls.length).toBeGreaterThan(3));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const toggle = within(slotRow(alice.position)).getByRole("button", { name: "Claim record" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(slotRow(alice.position)).getByText("Payments")).toBeInTheDocument();
  });

  it("points a claim toggle only at a record that is mounted", async () => {
    await openBounty();
    const alice = claimOf("alice-dev");
    const toggle = within(slotRow(alice.position)).getByRole("button", { name: "Claim record" });
    // A collapsed record is not mounted, so the toggle controls nothing yet.
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).not.toHaveAttribute("aria-controls");
    fireEvent.click(toggle);
    const payments = await within(slotRow(alice.position)).findByText("Payments");
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel).toContainElement(payments);
    fireEvent.click(toggle);
    expect(toggle).not.toHaveAttribute("aria-controls");
  });

  it("reads a review past its due date as overdue, not about to pass", async () => {
    const edited = structuredClone(detail);
    const slot = edited.slots.find((candidate) => candidate.status === "claimed")!;
    const due = Date.now() - 3_600_000;
    Object.assign(slot, {
      status: "submitted",
      deadline_at: null,
      review_due_at: new Date(due).toISOString(),
      review_overdue: true,
    });
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      path.split("?")[0] === `/public/bounties/${bountyId}`
        ? Promise.resolve(edited)
        : serve(path)) as typeof getJSON);
    await openBounty();
    const row = slotRow(slot.position);
    const deadline = within(row).getAllByRole("cell").at(-1)!;
    expect(deadline).toHaveTextContent("Review overdue");
    expect(deadline).not.toHaveTextContent("any moment now");
  });

  it("counts a deadline down again when the bounty refreshes", async () => {
    // A real fetch parses a new object every poll, as the detail polls.
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      serve(path).then((payload) => structuredClone(payload))) as typeof getJSON);
    const carol = claimOf("carol-dev");
    const slot = detail.slots.find((candidate) => candidate.slot_id === carol.slot_id)!;
    const due = Date.parse(slot.deadline_at!);
    const now = vi.spyOn(Date, "now").mockReturnValue(due - 5 * 3_600_000);
    try {
      await openBounty();
      expect(slotRow(carol.position)).toHaveTextContent("Delivery due in 5h");
      now.mockReturnValue(due - 2 * 3_600_000);
      refreshAllEndpoints();
      await vi.waitFor(() =>
        expect(slotRow(carol.position)).toHaveTextContent("Delivery due in 2h"),
      );
    } finally {
      now.mockRestore();
    }
  });

  it.each([
    ["confirmed", "refunded in full"],
    ["awaiting_approval", "owed back in full"],
  ])("says a fee that never claimed a slot was received, its refund %s", async (state, label) => {
    const alice = claimOf("alice-dev");
    const edited = structuredClone(claims);
    Object.assign(edited[alice.claim_id]!, { state: "refund_owed", claimed_at: null });
    edited[alice.claim_id]!.obligations.find((o) => o.kind === "fee_refund")!.state =
      state as never;
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      path === `/bounties/claims/${alice.claim_id}`
        ? Promise.resolve(edited[alice.claim_id])
        : serve(path)) as typeof getJSON);
    await openBounty();
    const row = slotRow(alice.position);
    fireEvent.click(within(row).getByRole("button", { name: "Claim record" }));
    const body = (await within(row).findByText("Payments")).closest(
      ".bounty-claim-body",
    ) as HTMLElement;
    expect(body).toHaveTextContent(`received but not applied to a slot; ${label}`);
    expect(body).not.toHaveTextContent("not paid");
    // The outcome agrees with the payment it reports.
    expect(body).toHaveTextContent(state === "confirmed" ? "Fee refunded" : "Fee refund owed");
  });

  it("names an award above the approval limit and a missing payout policy", async () => {
    const alice = claimOf("alice-dev");
    const edited = structuredClone(claims);
    const owed = edited[alice.claim_id]!.obligations;
    owed.find((o) => o.kind === "award")!.state = "over_policy_limit";
    owed.find((o) => o.kind === "award")!.approvals_required = null;
    const refund = owed.find((o) => o.kind === "fee_refund")!;
    refund.state = "awaiting_approval";
    refund.approvals_required = null;
    vi.mocked(getJSON).mockImplementation(((path: string) =>
      path === `/bounties/claims/${alice.claim_id}`
        ? Promise.resolve(edited[alice.claim_id])
        : serve(path)) as typeof getJSON);
    await openBounty();
    const row = slotRow(alice.position);
    fireEvent.click(within(row).getByRole("button", { name: "Claim record" }));
    const body = (await within(row).findByText("Payments")).closest(
      ".bounty-claim-body",
    ) as HTMLElement;
    expect(body).toHaveTextContent("Above the approval limit; awaits a policy change");
    expect(body).toHaveTextContent("(no payout policy is set yet)");
  });

  it("shows a rejected claim's pending appeal without its statement", async () => {
    await openBounty();
    const bob = claimOf("bob-dev");
    const row = slotRow(bob.position);
    fireEvent.click(within(row).getByRole("button", { name: "Claim record" }));
    const appeals = await within(row).findByText("Appeals");
    const body = appeals.closest(".bounty-claim-body") as HTMLElement;
    expect(body).toHaveTextContent("Rejected");
    expect(body).toHaveTextContent("Pending");
    expect(body).not.toHaveTextContent("Fixture private statement.");
  });

  it("keeps a reopened slot's earlier claim and its retained fee", async () => {
    await openBounty();
    const dave = claimOf("dave-dev");
    const earlier = screen.getByRole("button", {
      name: `Round 1 · slot ${dave.position} · claim ${dave.claim_id.slice(0, 8)}`,
    });
    fireEvent.click(earlier);
    const panel = earlier.closest(".bounty-claim") as HTMLElement;
    const payments = await within(panel).findByText("Payments");
    const body = payments.closest(".bounty-claim-body") as HTMLElement;
    expect(body).toHaveTextContent("Fee retained 2 α (simulated) · Retained by the treasury");
    // His own outcome, not that of the claim that took the slot after him.
    expect(within(body).getByText("Outcome").nextElementSibling).toHaveTextContent("Withdrawn");
  });

  it("lists the history newest first", async () => {
    await openBounty();
    const history = screen.getByRole("heading", { name: "History" }).nextElementSibling!;
    const items = within(history as HTMLElement).getAllByRole("listitem");
    expect(items).toHaveLength(detail.events.length);
    expect(items[0]).toHaveTextContent("Delivery recorded");
    expect(items.at(-1)).toHaveTextContent("Revision published");
    // A contributor appeals a decision by its event id.
    const rejection = detail.events.find((event) => event.kind === "rejected")!;
    const listed = items.find((item) => item.textContent?.includes("Work rejected"))!;
    expect(listed).toHaveTextContent(`event ${rejection.event_id}`);
  });
});

describe("alphaAmount", () => {
  it("names a claim status a newer API added instead of leaving it blank", () => {
    const view = { ...claimOf("alice-dev"), state: "claimed", slot_status: "paused_for_audit" };
    expect(claimOutcome(view as never)).toBe("paused_for_audit");
  });

  it("is exact for every amount Platform serves", () => {
    expect(alphaAmount(2_000_000_000)).toBe("2 α");
    expect(alphaAmount(1)).toBe("0.000000001 α");
    expect(alphaAmount(Number.MAX_SAFE_INTEGER)).toBe("9,007,199.25474 α");
  });

  it("refuses an amount JSON could not carry exactly", () => {
    expect(alphaAmount(Number.MAX_SAFE_INTEGER + 2)).toBe("Unknown");
  });
});
