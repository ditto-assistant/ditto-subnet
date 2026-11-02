import { cleanup, fireEvent, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import { GATE_NOTE_DISPUTE_STATUSES, ScreeningDispute, parseGateNoteIds } from "./DisputeForm";

afterEach(cleanup);

describe("parseGateNoteIds (#1852 dispute link)", () => {
  it("accepts an empty field as no citation", () => {
    expect(parseGateNoteIds("")).toEqual([]);
    expect(parseGateNoteIds("  \n ")).toEqual([]);
  });

  it("splits on commas, whitespace and newlines and de-duplicates", () => {
    expect(parseGateNoteIds("0123456789abcdef, FEDCBA9876543210\n0123456789abcdef")).toEqual([
      "0123456789abcdef",
      "fedcba9876543210",
    ]);
  });

  it("refuses anything that is not a 16-hex note id", () => {
    expect(parseGateNoteIds("0123456789abcde")).toBeNull();
    expect(parseGateNoteIds("0123456789abcdef, nope")).toBeNull();
    expect(parseGateNoteIds("0123456789abcdefg")).toBeNull();
  });

  it("caps the list at the API's 64 ids", () => {
    const many = Array.from({ length: 65 }, (_, i) => i.toString(16).padStart(16, "0")).join(",");
    expect(parseGateNoteIds(many)).toBeNull();
  });
});

describe("ScreeningDispute gate-note mode (#1852)", () => {
  const agentId = "5fdadd33-bd0f-492d-ba71-49bef159f069";

  it("offers a scored submission with v13 gate evidence a gate-note dispute", () => {
    render(() => (
      <ScreeningDispute agentId={agentId} status="scored" dispute={null} gateNotes={true} />
    ));
    const section = document.querySelector("[data-dispute-mode]");
    expect(section?.getAttribute("data-dispute-mode")).toBe("gate_notes");
    expect(section?.textContent).toContain("Dispute bench v13 gate notes");
    expect(section?.textContent).toContain("never changes your score or status");
    const ids = document.getElementById("screening-dispute-gate-notes") as HTMLInputElement;
    expect(ids.required).toBe(true);
    // A well-formed message and signature are not enough: the ids are what is
    // being disputed, so the submit stays disabled until at least one is cited.
    fireEvent.input(document.getElementById("screening-dispute-message") as HTMLTextAreaElement, {
      target: {
        value: "The twin_concordant marker fired on twins the seeded history answers identically.",
      },
    });
    fireEvent.input(document.getElementById("screening-dispute-signature") as HTMLInputElement, {
      target: { value: "ab".repeat(64) },
    });
    const submit = document.querySelector(".screening-dispute-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.input(ids, { target: { value: "0123456789abcdef" } });
    expect(submit.disabled).toBe(false);
  });

  it("renders no form for a scored submission without gate evidence", () => {
    render(() => <ScreeningDispute agentId={agentId} status="scored" dispute={null} />);
    expect(document.querySelector("[data-dispute-mode]")).toBeNull();
    cleanup();
    // Statuses that carry no accepted score never get the gate-note form.
    render(() => (
      <ScreeningDispute agentId={agentId} status="screening" dispute={null} gateNotes={true} />
    ));
    expect(document.querySelector("[data-dispute-mode]")).toBeNull();
    expect([...GATE_NOTE_DISPUTE_STATUSES]).toEqual([
      "scored",
      "live",
      "evaluating",
      "ath_pending_review",
    ]);
  });

  it("keeps the screening form for a rejected submission, ids optional", () => {
    render(() => (
      <ScreeningDispute agentId={agentId} status="rejected" dispute={null} gateNotes={true} />
    ));
    const section = document.querySelector("[data-dispute-mode]");
    expect(section?.getAttribute("data-dispute-mode")).toBe("screening");
    expect(section?.textContent).toContain("Dispute screening decision");
    const ids = document.getElementById("screening-dispute-gate-notes") as HTMLInputElement;
    expect(ids.required).toBe(false);
  });

  it("explains a resolved gate-note dispute without claiming a release", () => {
    render(() => (
      <ScreeningDispute
        agentId={agentId}
        status="scored"
        dispute={{ kind: "gate_notes", status: "resolved", resolution: "release" }}
      />
    ));
    const text = document.body.textContent ?? "";
    expect(text).toContain("Dispute accepted");
    expect(text).toContain("recorded as contested");
    expect(text).not.toContain("released this submission from quarantine");
    cleanup();
    render(() => (
      <ScreeningDispute
        agentId={agentId}
        status="scored"
        dispute={{ kind: "gate_notes", status: "resolved", resolution: "uphold" }}
      />
    ));
    expect(document.body.textContent).toContain("upheld the cited gate notes");
  });
});
