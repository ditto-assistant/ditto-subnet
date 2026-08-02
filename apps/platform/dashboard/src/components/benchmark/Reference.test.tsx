// Changelog authority tagging (renderBenchDocs 9535–9606): the active
// version is highlighted and an open rollout target is labeled separately —
// the authority state never promotes the rollout target (only "collecting" /
// "blocked_ineligible" mark it as rolling; lib/bench-state owns the badge
// contract, rows 19/20 in bench-state.test.ts).
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { GlossaryVersion } from "../../types/bench";
import { VersionHistory } from "./Reference";
import { changelogItems, neutralVersionCopy } from "./docs";

afterEach(cleanup);

const versions: GlossaryVersion[] = [
  { version: 8, title: "Next", summary: "next", epoch: "2026-08-01", highlights: ["h8"] },
  { version: 7, title: "Current", summary: "current", epoch: "2026-07-22" },
  { version: 5, title: "Old", summary: "old", epoch: "2026-07-21" },
];

describe("changelogItems", () => {
  it("tags the active version and an open rollout target separately", () => {
    const items = changelogItems(versions, 7, 8, "collecting");
    expect(items.map((item) => [item.version, item.active, item.rollout])).toEqual([
      [8, false, true],
      [7, true, false],
      [5, false, false],
    ]);
  });

  it("never marks a superseded or activated target as rolling", () => {
    for (const status of ["superseded", "activated", "inactive"]) {
      const items = changelogItems(versions, 7, 8, status);
      expect(
        items.some((item) => item.rollout),
        status,
      ).toBe(false);
    }
  });

  it("survives version gaps — v6 missing is rendered as served", () => {
    const tags = changelogItems(versions, 7, 7, "activated").map((item) => item.version);
    expect(tags).toEqual([8, 7, 5]);
  });
});

describe("VersionHistory", () => {
  it("renders the ver-item structure with active/rollout tags", () => {
    const { container } = render(() => (
      <VersionHistory items={changelogItems(versions, 7, 8, "collecting")} />
    ));
    const items = container.querySelectorAll("#bench-changelog .ver-item");
    expect(items.length).toBe(3);
    expect(items[0]?.classList.contains("ver-rollout")).toBe(true);
    expect(items[0]?.querySelector(".ver-next")?.textContent).toBe("rollout");
    expect(items[1]?.classList.contains("ver-current")).toBe(true);
    expect(items[1]?.querySelector(".ver-now")?.textContent).toBe("active");
    expect(items[0]?.querySelector(".ver-epoch")?.textContent).toBe("2026-08-01");
    expect(items[0]?.querySelector(".ver-hl li")?.textContent).toBe("h8");
  });

  it("keeps the loading placeholder until the glossary lands", () => {
    const { container } = render(() => <VersionHistory items={[]} />);
    expect(container.querySelector("#bench-changelog .muted")?.textContent).toBe(
      "Loading the benchmark changelog…",
    );
  });
});

describe("neutralVersionCopy", () => {
  it("swaps the v4-specific paragraph for the versioning rule on any other version", () => {
    // The v4 paragraph is true of 4 and of nothing else; the moment the live
    // version moves on it is replaced rather than left to misdescribe.
    const copy = neutralVersionCopy(9);
    expect(copy).toContain("Version 9 is the current generation contract.");
    expect(copy).toContain("ships as a new version");
  });
});
