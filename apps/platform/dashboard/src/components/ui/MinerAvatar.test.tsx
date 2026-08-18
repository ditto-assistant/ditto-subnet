import { cleanup, render } from "@solidjs/testing-library";
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
  });
});
