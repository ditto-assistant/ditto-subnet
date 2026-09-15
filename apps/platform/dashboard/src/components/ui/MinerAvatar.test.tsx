import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import { MinerAvatar } from "./MinerAvatar";

afterEach(() => cleanup());

describe("MinerAvatar", () => {
  it("renders nothing when no url is set", () => {
    const { container } = render(() => <MinerAvatar />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders the same-origin avatar path", () => {
    const { container } = render(() => (
      <MinerAvatar url="/api/v1/public/miners/5Hotkey/avatar" size="lg" />
    ));
    const img = container.querySelector("img.miner-avatar.lg");
    expect(img).toHaveAttribute("src", "/api/v1/public/miners/5Hotkey/avatar");
    expect(img).toHaveAttribute("width", "42");
    expect(img).toHaveAttribute("height", "42");
    expect(img).toHaveAttribute("loading", "lazy");
    expect(container.querySelector("button.miner-avatar-trigger")).toBeTruthy();
  });

  it("opens a scaled thumbnail lightbox on click", () => {
    const { container } = render(() => <MinerAvatar url="/api/v1/public/miners/5Hotkey/avatar" />);
    const trigger = container.querySelector("button.miner-avatar-trigger") as HTMLButtonElement;
    trigger.click();
    const dialog = screen.getByRole("dialog", { name: "Avatar preview" });
    expect(dialog.querySelector("img.miner-avatar-lightbox-img")).toHaveAttribute(
      "src",
      "/api/v1/public/miners/5Hotkey/avatar",
    );
  });
});
