import { For, Show, createMemo, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";
import { alpha } from "../components/GammaLedger";
import { athDate, monoDisplay, relTimeUntil, shortKey } from "../lib/format";
import { spaHref, spaQuery } from "../lib/router";
import type {
  BountyBoardPage,
  BountyClaimView,
  BountyDetail,
  BountyEventKind,
  BountyEventView,
  BountyObligationView,
  BountySlotStatus,
  BountySlotView,
  BountySummary,
  BountyTransferView,
} from "../types/bounty";

// Build bounties (#2942). Every term, deadline and amount comes from the API;
// nothing here is a default. Rehearsal bounties move no funds, and the page
// says so wherever a fee or payment appears.

const PAGE_SIZE = 50;
const GUIDE_URL = "https://github.com/ditto-assistant/ditto-subnet/blob/main/docs/bounty-guide.md";
// Matches a bounty id before it reaches a request path.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Exact alpha for an integer rao amount. Platform caps amounts at 2^53 - 1,
 * so every one it serves is exact here. */
export function alphaAmount(rao: number): string {
  return alpha(Number.isSafeInteger(rao) ? String(rao) : null);
}

const githubIssue = (repo: string, issue: number) => `https://github.com/${repo}/issues/${issue}`;

const STATUS_LABEL: Record<BountySlotStatus, string> = {
  open: "Open",
  held: "Held for payment",
  claimed: "Claimed",
  submitted: "In review",
  rework: "Rework",
  accepted: "Accepted",
  rejected: "Rejected",
  expired: "Expired",
  withdrawn: "Withdrawn",
  cancelled: "Cancelled",
};

const EVENT_LABEL: Record<BountyEventKind, string> = {
  revision_published: "Revision published",
  round_opened: "Round opened",
  slot_held: "Slot held for payment",
  hold_expired: "Hold expired",
  hold_released: "Hold released",
  slot_claimed: "Slot claimed",
  fee_refund_owed: "Fee refund owed",
  claims_paused: "Claims paused",
  claims_resumed: "Claims resumed",
  submitted: "Work submitted",
  withdrawn: "Claim withdrawn",
  deadline_missed: "Deadline missed",
  review_overdue: "Review overdue",
  rework_requested: "Changes requested",
  accepted: "Work accepted",
  rejected: "Work rejected",
  reviewer_reassigned: "Reviewer reassigned",
  appeal_filed: "Appeal filed",
  appeal_upheld: "Appeal upheld",
  appeal_denied: "Appeal denied",
  slot_cancelled: "Slot cancelled",
  slot_reopened: "Slot reopened",
  fee_retained: "Fee retained",
  award_recorded: "Award recorded",
  obligation_approved: "Payment approved",
  transfer_dispatched: "Payment sent",
  transfer_confirmed: "Payment confirmed",
  transfer_failed: "Payment failed",
  transfer_uncertain: "Payment unconfirmed",
  fact_recorded: "Delivery recorded",
};

const OBLIGATION_LABEL: Record<BountyObligationView["kind"], string> = {
  fee_refund: "Fee refund",
  fee_retained: "Fee retained",
  award: "Award",
};

const OBLIGATION_STATE: Record<BountyObligationView["state"], string> = {
  retained: "Retained by the treasury",
  awaiting_approval: "Awaiting approval",
  over_policy_limit: "Above the approval limit; awaits a policy change",
  approved: "Approved, not sent",
  dispatched: "Sent, awaiting confirmation",
  uncertain: "Outcome unknown, being reconciled",
  confirmed: "Confirmed",
  failed: "Failed, needs fresh approval",
};

const TRANSFER_STATE: Record<BountyTransferView["state"], string> = {
  dispatched: "Sent",
  confirmed: "Confirmed",
  failed: "Failed",
  expired: "Expired",
  uncertain: "Unknown",
};

const DECISION_LABEL = {
  accept: "Accepted",
  changes_requested: "Changes requested",
  reject: "Rejected",
} as const;

/** A claim's own outcome: its hold, or the status of the work it paid for. */
export function claimOutcome(view: BountyClaimView): string {
  if (view.state === "held") return "Held for payment";
  if (view.state === "hold_expired") return "Hold ended unpaid";
  if (view.state === "refund_owed") return "Fee refund owed";
  return STATUS_LABEL[view.slot_status];
}

function selectedFromUrl(): string | null {
  // spaQuery also reads a hash-route bookmark (#/bounties?bounty=...); ids are
  // matched in their canonical lowercase form.
  const id = spaQuery().get("bounty")?.toLowerCase() ?? null;
  return id !== null && UUID.test(id) ? id : null;
}

function showBounty(id: string | null): void {
  const params = spaQuery();
  params.delete("bounty");
  if (id !== null) params.set("bounty", id);
  history.pushState(history.state, "", spaHref("bounties", params));
}

export function BountiesPage(): JSX.Element {
  const [selected, setSelected] = createSignal(selectedFromUrl());
  const restore = () => setSelected(selectedFromUrl());
  window.addEventListener("popstate", restore);
  onCleanup(() => window.removeEventListener("popstate", restore));
  const open = (id: string | null) => {
    showBounty(id);
    setSelected(id);
    window.scrollTo?.(0, 0);
  };
  return (
    <section class="page active bounties-page" data-page="bounties" aria-label="Build bounties">
      <Show when={selected()} fallback={<BountyBoard onOpen={open} />} keyed>
        {(id) => <BountyDetailView bountyId={id} onBack={() => open(null)} />}
      </Show>
    </section>
  );
}

function RehearsalBadge(props: { mode: BountySummary["funding_mode"] }): JSX.Element {
  return (
    <Show when={props.mode === "rehearsal"}>
      <span class="bounty-badge rehearsal" title="Fees and payments are simulated">
        Rehearsal
      </span>
    </Show>
  );
}

function SlotCounts(props: { summary: BountySummary }): JSX.Element {
  const parts = () => {
    const slots = props.summary.slots;
    return [
      [slots.open, "open"],
      [slots.held, "held"],
      [slots.claimed, "claimed"],
      [slots.submitted, "in review"],
      [slots.rework, "rework"],
      [slots.closed, "closed"],
    ].filter(([count]) => count !== 0) as [number, string][];
  };
  return (
    <span class="bounty-slot-counts">
      <Show when={parts().length > 0} fallback="No slots">
        {parts()
          .map(([count, label]) => `${count} ${label}`)
          .join(" · ")}
      </Show>
    </span>
  );
}

function BountyBoard(props: { onOpen: (id: string) => void }): JSX.Element {
  const [offset, setOffset] = createSignal(0);
  const board = useEndpoint<BountyBoardPage>(
    () => `/public/bounties?limit=${PAGE_SIZE}&offset=${offset()}`,
    { pollMs: REFRESH_MS },
  );
  const data = () => (board.error() ? undefined : board.data());
  const rehearsal = () => data()?.items.some((item) => item.funding_mode === "rehearsal");
  return (
    <>
      <header class="bounty-intro">
        <h2>Build bounties</h2>
        <p>
          Scoped work on Ditto repositories, paid in alpha. Each bounty&apos;s terms are a signed
          revision: its scope, acceptance evidence, entry fee, reward pool, slots and deadlines.
          Claims, submissions, review decisions and payments are recorded here as they happen.
        </p>
        <p>
          To take part, claim a slot with the miner CLI and pay the entry fee to the bounty&apos;s
          escrow. The <a href={GUIDE_URL}>contributor guide</a> covers linking GitHub, the CLA,
          claiming, submitting and appeals.
        </p>
      </header>
      <Show when={rehearsal()}>
        <p class="bounty-notice" role="note">
          Rehearsal bounties exercise the whole workflow with simulated fees and payments. No alpha
          moves, and a rehearsal award is not a payment.
        </p>
      </Show>
      <Show when={board.error()}>
        <div role="alert" class="bounty-state">
          Bounties could not be loaded. No example data is shown.{" "}
          <button type="button" onClick={() => board.refresh()}>
            Retry
          </button>
        </div>
      </Show>
      <Show when={data()}>
        {(page) => (
          <Show
            when={page().items.length > 0}
            fallback={
              <Show
                when={page().offset > 0}
                fallback={<p class="bounty-state">No bounties are published yet.</p>}
              >
                <p class="bounty-state">
                  No bounties on this page.{" "}
                  <button type="button" onClick={() => setOffset(0)}>
                    First page
                  </button>
                </p>
              </Show>
            }
          >
            <div class="bounty-table-scroll">
              <table class="bounty-table bounty-board">
                <caption>Published bounties</caption>
                <thead>
                  <tr>
                    <th scope="col">Bounty</th>
                    <th scope="col">Entry fee</th>
                    <th scope="col">Reward pool</th>
                    <th scope="col">Slots</th>
                    <th scope="col">Terms published</th>
                  </tr>
                </thead>
                <tbody>
                  <For each={page().items}>
                    {(item) => (
                      <tr>
                        <td>
                          <button
                            type="button"
                            class="bounty-open"
                            onClick={() => props.onOpen(item.bounty_id)}
                          >
                            {item.title}
                          </button>
                          <small>
                            {item.repo}#{item.issue} · revision {item.current_revision} · round{" "}
                            {item.current_round}
                          </small>
                          <span class="bounty-flags">
                            <RehearsalBadge mode={item.funding_mode} />
                            <Show when={item.claims_paused}>
                              <span class="bounty-badge">Claims paused</span>
                            </Show>
                            <Show when={item.reviews_overdue > 0}>
                              <span class="bounty-badge warn">
                                {item.reviews_overdue} review
                                {item.reviews_overdue === 1 ? "" : "s"} overdue
                              </span>
                            </Show>
                          </span>
                        </td>
                        <td>{alphaAmount(item.entry_fee_alpha_rao)}</td>
                        <td>
                          {alphaAmount(item.reward_pool_alpha_rao)}
                          <small>{alphaAmount(item.awarded_alpha_rao)} awarded</small>
                        </td>
                        <td>
                          <SlotCounts summary={item} />
                        </td>
                        <td>{athDate(item.updated_at)}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </div>
            <Show when={page().total > page().limit}>
              <nav class="bounty-pager" aria-label="Bounty pages">
                <span>
                  {page().offset + 1}–{page().offset + page().items.length} of {page().total}
                </span>
                <button
                  type="button"
                  disabled={page().offset === 0}
                  onClick={() => setOffset(Math.max(0, page().offset - page().limit))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  disabled={page().offset + page().items.length >= page().total}
                  onClick={() => setOffset(page().offset + page().limit)}
                >
                  Next
                </button>
              </nav>
            </Show>
          </Show>
        )}
      </Show>
    </>
  );
}

/** The deadline that applies to a slot's current status, if any. */
function slotDeadline(slot: BountySlotView): { label: string; at: string } | null {
  if (slot.status === "held" && slot.hold_expires_at)
    return { label: "Payment due", at: slot.hold_expires_at };
  if ((slot.status === "claimed" || slot.status === "rework") && slot.deadline_at)
    return { label: "Delivery due", at: slot.deadline_at };
  if (slot.status === "submitted" && slot.review_due_at)
    return { label: "Review due", at: slot.review_due_at };
  return null;
}

function slotName(slot: { round: number; position: number }): string {
  return `Round ${slot.round} · slot ${slot.position}`;
}

function BountyDetailView(props: { bountyId: string; onBack: () => void }): JSX.Element {
  const detail = useEndpoint<BountyDetail>(() => `/public/bounties/${props.bountyId}`, {
    pollMs: REFRESH_MS,
  });
  const data = () => (detail.error() ? undefined : detail.data());
  return (
    <>
      <button type="button" class="bounty-back" onClick={() => props.onBack()}>
        ← All bounties
      </button>
      <Show when={detail.error()}>
        <div role="alert" class="bounty-state">
          This bounty could not be loaded. It may not exist, or the API is unavailable.{" "}
          <button type="button" onClick={() => detail.refresh()}>
            Retry
          </button>
        </div>
      </Show>
      <Show when={data()}>{(view) => <BountyDetailBody view={view()} />}</Show>
    </>
  );
}

function BountyDetailBody(props: { view: BountyDetail }): JSX.Element {
  const summary = () => props.view.summary;
  const current = () => props.view.revisions.at(-1);
  const slotsById = createMemo(
    () => new Map(props.view.slots.map((slot) => [slot.slot_id, slot] as const)),
  );
  // Claims no slot holds any more (a reopened slot's earlier claimant) are
  // reachable only through their events.
  const earlierClaims = createMemo(() => {
    const holding = new Set(props.view.slots.map((slot) => slot.claimant?.claim_id));
    const seen = new Map<string, BountyEventView>();
    for (const event of props.view.events) {
      if (event.claim_id && !holding.has(event.claim_id) && !seen.has(event.claim_id))
        seen.set(event.claim_id, event);
    }
    return [...seen.values()];
  });
  const rounds = createMemo(() => [...new Set(props.view.slots.map((slot) => slot.round))]);
  const newestFirst = createMemo(() =>
    props.view.events.map((_, index, all) => all[all.length - 1 - index]!),
  );
  return (
    <>
      <header class="bounty-intro">
        <h2>{summary().title}</h2>
        <p class="bounty-ref">
          <a href={githubIssue(summary().repo, summary().issue)}>
            {summary().repo}#{summary().issue}
          </a>{" "}
          · revision {summary().current_revision} · round {summary().current_round}{" "}
          <RehearsalBadge mode={summary().funding_mode} />
          <Show when={summary().claims_paused}>
            <span class="bounty-badge">Claims paused</span>
          </Show>
        </p>
        <Show when={summary().funding_mode === "rehearsal"}>
          <p class="bounty-notice" role="note">
            This is a rehearsal bounty. Fees and payments are simulated and no alpha moves.
          </p>
        </Show>
      </header>
      <section aria-labelledby="bounty-terms">
        <h3 id="bounty-terms">Terms</h3>
        <dl class="bounty-facts">
          <div>
            <dt>Entry fee</dt>
            <dd>{alphaAmount(summary().entry_fee_alpha_rao)}</dd>
          </div>
          <div>
            <dt>Reward pool</dt>
            <dd>{alphaAmount(summary().reward_pool_alpha_rao)}</dd>
          </div>
          <div>
            <dt>Awarded</dt>
            <dd>{alphaAmount(summary().awarded_alpha_rao)}</dd>
          </div>
          <Show when={current()}>
            {(revision) => (
              <>
                <div>
                  <dt>Slots per round</dt>
                  <dd>{revision().terms.attempts_per_round}</dd>
                </div>
                <div>
                  <dt>Payment hold</dt>
                  <dd>{revision().terms.hold_minutes} minutes</dd>
                </div>
              </>
            )}
          </Show>
          <div>
            <dt>Delivery</dt>
            <dd>{summary().delivery_days} days after the fee is confirmed</dd>
          </div>
          <div>
            <dt>Review</dt>
            <dd>{summary().review_days} days after submission</dd>
          </div>
          <div>
            <dt>Reviewer</dt>
            <dd>{summary().reviewer_login}</dd>
          </div>
        </dl>
        <Show when={current()}>
          {(revision) => (
            <>
              <div class="bounty-text">
                <h4>Scope</h4>
                <p>{revision().terms.scope}</p>
                <h4>Public interfaces</h4>
                <p>{revision().terms.public_interfaces}</p>
                <h4>Acceptance evidence</h4>
                <p>{revision().terms.acceptance_evidence}</p>
                <Show when={revision().terms.dependencies.length > 0}>
                  <h4>Depends on</h4>
                  <ul>
                    <For each={revision().terms.dependencies}>
                      {(reference) => {
                        const [repo, issue] = reference.split("#") as [string, string];
                        return (
                          <li>
                            <a href={githubIssue(repo, Number(issue))}>{reference}</a>
                          </li>
                        );
                      }}
                    </For>
                  </ul>
                </Show>
              </div>
              <dl class="bounty-facts keys">
                <div>
                  <dt>Fee escrow coldkey</dt>
                  <dd class="bounty-mono">{revision().terms.fee_escrow_coldkey}</dd>
                </div>
                <div>
                  <dt>Fee escrow hotkey</dt>
                  <dd class="bounty-mono">{revision().terms.fee_escrow_hotkey}</dd>
                </div>
                <div>
                  <dt>Spec digest</dt>
                  <dd class="bounty-mono" title={revision().spec_digest}>
                    {revision().spec_digest}
                  </dd>
                </div>
              </dl>
            </>
          )}
        </Show>
      </section>
      <section aria-labelledby="bounty-slots">
        <h3 id="bounty-slots">Slots</h3>
        <Show
          when={props.view.slots.length > 0}
          fallback={<p class="bounty-state">No slots are open.</p>}
        >
          <For each={rounds()}>
            {(round) => (
              <div class="bounty-table-scroll">
                <table class="bounty-table">
                  <caption>Round {round}</caption>
                  <thead>
                    <tr>
                      <th scope="col">Slot</th>
                      <th scope="col">Status</th>
                      <th scope="col">Claimant</th>
                      <th scope="col">Deadline</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={props.view.slots.filter((slot) => slot.round === round)}>
                      {(slot) => <SlotRow slot={slot} />}
                    </For>
                  </tbody>
                </table>
              </div>
            )}
          </For>
        </Show>
        <Show when={earlierClaims().length > 0}>
          <h4>Earlier claims</h4>
          <ul class="bounty-earlier">
            <For each={earlierClaims()}>
              {(event) => {
                const slot = event.slot_id ? slotsById().get(event.slot_id) : undefined;
                return (
                  <li>
                    <ClaimToggle
                      claimId={event.claim_id!}
                      label={`${slot ? slotName(slot) : "Slot"} · claim ${event.claim_id!.slice(0, 8)}`}
                    />
                  </li>
                );
              }}
            </For>
          </ul>
        </Show>
      </section>
      <section aria-labelledby="bounty-history">
        <h3 id="bounty-history">History</h3>
        <ol class="bounty-events">
          <For each={newestFirst()}>
            {(event) => {
              const slot = event.slot_id ? slotsById().get(event.slot_id) : undefined;
              return (
                <li>
                  <time datetime={event.recorded_at}>{athDate(event.recorded_at)}</time>
                  <span>{EVENT_LABEL[event.kind] ?? event.kind}</span>
                  <small>
                    {[
                      slot ? slotName(slot) : null,
                      event.revision !== null && event.kind === "revision_published"
                        ? `revision ${event.revision}`
                        : null,
                      // An appeal names the decision's event id.
                      `event ${event.event_id}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </small>
                </li>
              );
            }}
          </For>
        </ol>
        <Show when={props.view.revisions.length > 1}>
          <h4>Revisions</h4>
          <ul class="bounty-revisions">
            <For each={props.view.revisions}>
              {(revision) => (
                <li>
                  Revision {revision.revision} · {athDate(revision.published_at)} ·{" "}
                  <span class="bounty-mono" title={revision.spec_digest}>
                    {monoDisplay(revision.spec_digest)}
                  </span>
                </li>
              )}
            </For>
          </ul>
        </Show>
      </section>
    </>
  );
}

function SlotRow(props: { slot: BountySlotView }): JSX.Element {
  const deadline = () => slotDeadline(props.slot);
  return (
    <tr>
      <th scope="row">{props.slot.position}</th>
      <td>
        <span class={`bounty-status ${props.slot.status}`}>{STATUS_LABEL[props.slot.status]}</span>
        <Show when={props.slot.review_overdue}>
          <span class="bounty-badge warn">Review overdue</span>
        </Show>
      </td>
      <td>
        <Show when={props.slot.claimant} fallback="—">
          {(claimant) => (
            <>
              {claimant().github_login}
              <small class="bounty-mono" title={claimant().hotkey}>
                {shortKey(claimant().hotkey)}
              </small>
              <ClaimToggle claimId={claimant().claim_id} label="Claim record" />
            </>
          )}
        </Show>
      </td>
      <td>
        <Show when={deadline()} fallback="—">
          {(due) => (
            <>
              {due().label} {relTimeUntil(due().at)}
              <small>{athDate(due().at)}</small>
            </>
          )}
        </Show>
      </td>
    </tr>
  );
}

function ClaimToggle(props: { claimId: string; label: string }): JSX.Element {
  const [shown, setShown] = createSignal(false);
  const panel = `bounty-claim-${props.claimId}`;
  return (
    <div class="bounty-claim">
      <button
        type="button"
        class="bounty-claim-toggle"
        aria-expanded={shown()}
        aria-controls={panel}
        onClick={() => setShown(!shown())}
      >
        {props.label}
      </button>
      <Show when={shown()}>
        <div id={panel}>
          <ClaimRecord claimId={props.claimId} />
        </div>
      </Show>
    </div>
  );
}

function ClaimRecord(props: { claimId: string }): JSX.Element {
  // An open record follows the claim as it moves on.
  const claim = useEndpoint<BountyClaimView>(() => `/bounties/claims/${props.claimId}`, {
    pollMs: REFRESH_MS,
  });
  const data = () => (claim.error() ? undefined : claim.data());
  return (
    <>
      <Show when={claim.error()}>
        <p role="alert" class="bounty-state">
          This claim could not be loaded.
        </p>
      </Show>
      <Show when={data()}>{(view) => <ClaimBody view={view()} />}</Show>
    </>
  );
}

function ClaimBody(props: { view: BountyClaimView }): JSX.Element {
  const view = () => props.view;
  const simulated = () => view().funding_mode === "rehearsal";
  return (
    <div class="bounty-claim-body">
      <dl class="bounty-facts">
        <div>
          <dt>Contributor</dt>
          <dd>{view().github_login}</dd>
        </div>
        <div>
          <dt>Outcome</dt>
          <dd>{claimOutcome(view())}</dd>
        </div>
        <div>
          <dt>Hotkey</dt>
          <dd class="bounty-mono">{view().hotkey}</dd>
        </div>
        <div>
          <dt>Payee coldkey</dt>
          <dd class="bounty-mono">{view().payee_coldkey}</dd>
        </div>
        <div>
          <dt>Entry fee</dt>
          <dd>
            {alphaAmount(view().fee_alpha_rao)}
            {simulated() ? " (simulated)" : ""} ·{" "}
            {view().claimed_at ? `paid ${athDate(view().claimed_at)}` : "not paid"}
          </dd>
        </div>
      </dl>
      <Show when={view().submissions.length > 0}>
        <h4>Submissions</h4>
        <ol class="bounty-submissions">
          <For each={view().submissions}>
            {(submission) => (
              <li>
                <a href={`https://github.com/${view().repo}/pull/${submission.pr_number}`}>
                  PR #{submission.pr_number}
                </a>{" "}
                at{" "}
                <a
                  class="bounty-mono"
                  href={`https://github.com/${view().repo}/commit/${submission.head_sha}`}
                  title={submission.head_sha}
                >
                  {submission.head_sha.slice(0, 12)}
                </a>{" "}
                · submitted {athDate(submission.submitted_at)}
                <Show
                  when={submission.decision}
                  fallback={<p>Review due {athDate(submission.review_due_at)}</p>}
                >
                  {(decision) => (
                    <p>
                      <strong>{DECISION_LABEL[decision()]}</strong> {athDate(submission.decided_at)}
                      <Show when={submission.decision_reason}>
                        {(reason) => <span class="bounty-reason">{reason()}</span>}
                      </Show>
                      <Show when={submission.attribution}>
                        {(credit) => <span class="bounty-reason">Credit: {credit()}</span>}
                      </Show>
                      <Show when={submission.rework_deadline_at}>
                        {(due) => <span>Rework due {athDate(due())}</span>}
                      </Show>
                    </p>
                  )}
                </Show>
              </li>
            )}
          </For>
        </ol>
      </Show>
      <Show when={view().appeals.length > 0}>
        <h4>Appeals</h4>
        <ul>
          <For each={view().appeals}>
            {(appeal) => (
              <li>
                Filed {athDate(appeal.filed_at)} ·{" "}
                {appeal.upheld === null ? "Pending" : appeal.upheld ? "Upheld" : "Denied"}
                <Show when={appeal.decided_at}>{(at) => <> {athDate(at())}</>}</Show>
                <small class="bounty-mono" title="SHA-256 of the private statement">
                  {monoDisplay(appeal.reason_digest)}
                </small>
              </li>
            )}
          </For>
        </ul>
      </Show>
      <Show when={view().obligations.length > 0}>
        <h4>Payments</h4>
        <ul class="bounty-obligations">
          <For each={view().obligations}>{(owed) => <ObligationRow owed={owed} />}</For>
        </ul>
      </Show>
      <Show when={view().facts.length > 0}>
        <h4>Delivery</h4>
        <ul>
          <For each={view().facts}>
            {(fact) => (
              <li>
                {fact.kind[0]!.toUpperCase() + fact.kind.slice(1)} {athDate(fact.recorded_at)}:{" "}
                <Show when={fact.reference.startsWith("https://")} fallback={fact.reference}>
                  <a href={fact.reference} rel="noopener noreferrer">
                    {fact.reference}
                  </a>
                </Show>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </div>
  );
}

function ObligationRow(props: { owed: BountyObligationView }): JSX.Element {
  const owed = () => props.owed;
  return (
    <li>
      <strong>{OBLIGATION_LABEL[owed().kind]}</strong> {alphaAmount(owed().amount_alpha_rao)}
      {owed().simulated ? " (simulated)" : ""} · {OBLIGATION_STATE[owed().state]}
      <Show when={owed().state === "awaiting_approval"}>
        {" "}
        <Show
          when={owed().approvals_required !== null}
          fallback={<>(no payout policy is set yet)</>}
        >
          ({owed().approvals} of {owed().approvals_required} approvals)
        </Show>
      </Show>
      <small class="bounty-mono" title={owed().payee_coldkey}>
        to {shortKey(owed().payee_coldkey)}
      </small>
      <Show when={owed().attribution}>
        {(credit) => <span class="bounty-reason">Credit: {credit()}</span>}
      </Show>
      <Show when={owed().transfers.length > 0}>
        <ul>
          <For each={owed().transfers}>
            {(transfer) => (
              <li>
                {TRANSFER_STATE[transfer.state]}
                {transfer.simulated ? " (simulated)" : ""} · sent {athDate(transfer.dispatched_at)}
                <Show when={transfer.receipt}>
                  {(receipt) => (
                    <small class="bounty-mono" title={receipt().block_hash}>
                      block {receipt().block_number}, extrinsic {receipt().extrinsic_index}
                    </small>
                  )}
                </Show>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </li>
  );
}
