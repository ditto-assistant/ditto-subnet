import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { syncFromLocation } from "../stores/routeStore";
import { clearMinerSession } from "../stores/sessionStore";
import { ReviewsPage } from "./ReviewsPage";

beforeEach(() => {
  history.replaceState(null, "", "/#/reviews");
  syncFromLocation();
  clearMinerSession();
});

afterEach(cleanup);

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
});
