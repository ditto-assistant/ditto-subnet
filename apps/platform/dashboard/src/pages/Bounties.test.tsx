import { cleanup, fireEvent, render, screen, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BountiesPage, alphaAmount } from "./BountiesPage";
import { HTTPError, getJSON } from "../lib/api";
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

  it("states an empty board", async () => {
    vi.mocked(getJSON).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    render(() => <BountiesPage />);
    expect(await screen.findByText("No bounties are published yet.")).toBeInTheDocument();
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
  it("is exact for every amount Platform serves", () => {
    expect(alphaAmount(2_000_000_000)).toBe("2 α");
    expect(alphaAmount(1)).toBe("0.000000001 α");
    expect(alphaAmount(Number.MAX_SAFE_INTEGER)).toBe("9,007,199.25474 α");
  });

  it("refuses an amount JSON could not carry exactly", () => {
    expect(alphaAmount(Number.MAX_SAFE_INTEGER + 2)).toBe("Unknown");
  });
});
