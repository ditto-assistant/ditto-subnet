import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "./CopyButton";

let writeText: ReturnType<typeof vi.fn>;

function stubClipboard(impl: () => Promise<void>): void {
  writeText = vi.fn(impl);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

function dropClipboard(): void {
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
}

function renderCopy(value: string | null = "5Ecmtyhotkey", label = "miner hotkey"): void {
  render(() => (
    <>
      <div id="copy-status" class="visually-hidden" role="status" aria-live="polite" />
      <CopyButton value={value} label={label} />
    </>
  ));
}

function button(): HTMLButtonElement {
  const el = document.querySelector<HTMLButtonElement>("button.copy");
  if (!el) throw new Error("missing copy button");
  return el;
}

function status(): HTMLElement {
  const el = document.getElementById("copy-status");
  if (!el) throw new Error("missing copy status region");
  return el;
}

beforeEach(() => {
  stubClipboard(() => Promise.resolve());
});

afterEach(() => {
  cleanup();
  dropClipboard();
});

describe("CopyButton (row 24)", () => {
  it("renders the copy contract: data-key, label, and the shared live region wiring", () => {
    renderCopy("abc123", "dataset SHA-256");
    const el = button();
    expect(el).toHaveAttribute("type", "button");
    expect(el).toHaveAttribute("data-key", "abc123");
    expect(el).toHaveAttribute("aria-label", "Copy dataset SHA-256");
    expect(el).toHaveAttribute("title", "Copy dataset SHA-256");
    expect(el).toHaveAttribute("aria-describedby", "copy-status");
    expect(status()).toHaveAttribute("role", "status");
    expect(status()).toHaveAttribute("aria-live", "polite");
  });

  it("renders nothing without a value (the original returned an empty string)", () => {
    renderCopy(null);
    expect(document.querySelector("button.copy")).toBeNull();
  });

  it("copies on click and announces success through #copy-status", async () => {
    renderCopy();
    fireEvent.click(button());
    await waitFor(() => expect(button().classList.contains("copied")).toBe(true));
    expect(writeText).toHaveBeenCalledWith("5Ecmtyhotkey");
    expect(button()).toHaveAttribute("aria-label", "Copied miner hotkey");
    expect(status()).toHaveTextContent("Copied miner hotkey to the clipboard.");
  });

  it("resets the copied state after the announce window", async () => {
    vi.useFakeTimers();
    try {
      renderCopy();
      fireEvent.click(button());
      await vi.advanceTimersByTimeAsync(0);
      expect(button().classList.contains("copied")).toBe(true);
      await vi.advanceTimersByTimeAsync(1700);
      expect(button().classList.contains("copied")).toBe(false);
      expect(button()).toHaveAttribute("aria-label", "Copy miner hotkey");
    } finally {
      vi.useRealTimers();
    }
  });

  it("activates from the keyboard with Enter and Space", async () => {
    renderCopy();
    fireEvent.keyDown(button(), { key: "Enter" });
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    fireEvent.keyDown(button(), { key: " " });
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    // Any other key is not an activation.
    fireEvent.keyDown(button(), { key: "c" });
    expect(writeText).toHaveBeenCalledTimes(2);
  });

  it("falls back through execCommand and surfaces the manual-copy failure copy", async () => {
    // No async clipboard and no execCommand support: the legacy path fails
    // and the button must say so rather than pretending it copied.
    dropClipboard();
    renderCopy();
    fireEvent.click(button());
    await waitFor(() => expect(button().classList.contains("failed")).toBe(true));
    expect(button()).toHaveAttribute("aria-label", "Could not copy miner hotkey");
    expect(status()).toHaveTextContent(
      "Could not copy miner hotkey. Select the full value and copy it manually.",
    );
  });

  it("uses the legacy path when the async clipboard rejects", async () => {
    stubClipboard(() => Promise.reject(new Error("denied")));
    const execCommand = vi.fn(() => true);
    const doc = document as unknown as { execCommand?: (command: string) => boolean };
    doc.execCommand = execCommand;
    try {
      renderCopy();
      fireEvent.click(button());
      await waitFor(() => expect(button().classList.contains("copied")).toBe(true));
      expect(execCommand).toHaveBeenCalledWith("copy");
    } finally {
      delete doc.execCommand;
    }
  });
});
