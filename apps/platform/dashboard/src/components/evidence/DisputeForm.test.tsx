import { describe, expect, it } from "vitest";

import { parseGateNoteIds } from "./DisputeForm";

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
