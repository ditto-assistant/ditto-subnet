import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  agentLabel,
  agentName,
  agentVersionLabel,
  athDate,
  clamp01,
  duplicateLabel,
  elapsedDuration,
  esc,
  fmtMs,
  fx,
  marginText,
  median,
  num,
  pct,
  relDuration,
  relTime,
  relTimeUntil,
  releaseTime,
  shortKey,
  shortModel,
  telemetryCount,
  telemetryDuration,
  timelineDate,
} from "./format";

describe("pct / fx / num", () => {
  it("formats a fraction as a one-decimal percent", () => {
    expect(pct(0.123)).toBe("12.3%");
    expect(pct(0)).toBe("0.0%");
    expect(pct(1)).toBe("100.0%");
  });

  it("formats fixed three decimals", () => {
    expect(fx(0.4917)).toBe("0.492");
    expect(fx(0)).toBe("0.000");
    expect(fx(1)).toBe("1.000");
  });

  it("strips trailing zeros from consensus parameters", () => {
    expect(num(0.5)).toBe("0.5");
    expect(num(0.1)).toBe("0.1");
    expect(num(2)).toBe("2");
    expect(num(0.0625)).toBe("0.0625");
  });

  it("caps num at four decimals", () => {
    expect(num(0.123456)).toBe("0.1235");
  });
});

describe("marginText", () => {
  it("names the composite-point margin", () => {
    expect(marginText(0.02)).toBe("0.02 composite points");
  });

  it("reads 'incumbent' for any non-finite margin", () => {
    expect(marginText(NaN)).toBe("incumbent");
    expect(marginText(Infinity)).toBe("incumbent");
    expect(marginText(null)).toBe("incumbent");
    expect(marginText(undefined)).toBe("incumbent");
  });
});

describe("clamp01", () => {
  it("clamps to [0, 1]", () => {
    expect(clamp01(-0.5)).toBe(0);
    expect(clamp01(0.25)).toBe(0.25);
    expect(clamp01(1.5)).toBe(1);
  });
});

describe("fmtMs", () => {
  it("renders milliseconds below one second", () => {
    expect(fmtMs(0)).toBe("0 ms");
    expect(fmtMs(999)).toBe("999 ms");
  });

  it("renders one-decimal seconds below ten seconds", () => {
    expect(fmtMs(1000)).toBe("1.0 s");
    // The branch keys on the raw ms value, so 9999 rounds up to "10.0 s".
    expect(fmtMs(9999)).toBe("10.0 s");
  });

  it("renders whole seconds at ten seconds and above", () => {
    expect(fmtMs(10000)).toBe("10 s");
    expect(fmtMs(12345)).toBe("12 s");
  });
});

describe("median", () => {
  it("returns 0 for an empty array", () => {
    expect(median([])).toBe(0);
  });

  it("picks the middle element for odd counts", () => {
    expect(median([3, 1, 2])).toBe(2);
  });

  it("averages the two middle elements for even counts", () => {
    expect(median([4, 1, 3, 2])).toBe(2.5);
  });

  it("never mutates its input", () => {
    const input = [3, 1, 2];
    median(input);
    expect(input).toEqual([3, 1, 2]);
  });
});

describe("relTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-23T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders seconds, minutes, hours, and days", () => {
    expect(relTime("2026-07-23T11:59:15Z")).toBe("45s ago");
    expect(relTime("2026-07-23T11:55:00Z")).toBe("5m ago");
    expect(relTime("2026-07-23T09:00:00Z")).toBe("3h ago");
    expect(relTime("2026-07-21T12:00:00Z")).toBe("2d ago");
  });

  it("uses the unit boundaries exactly", () => {
    expect(relTime("2026-07-23T11:59:01Z")).toBe("59s ago");
    expect(relTime("2026-07-23T11:59:00Z")).toBe("1m ago");
    expect(relTime("2026-07-23T11:00:01Z")).toBe("59m ago");
    expect(relTime("2026-07-23T11:00:00Z")).toBe("1h ago");
    expect(relTime("2026-07-22T12:00:00Z")).toBe("1d ago");
  });

  it("clamps future timestamps to zero seconds", () => {
    expect(relTime("2026-07-23T12:00:30Z")).toBe("0s ago");
  });

  it("renders an en dash for invalid or missing input", () => {
    expect(relTime("not-a-date")).toBe("–");
    expect(relTime(undefined)).toBe("–");
    expect(relTime(null)).toBe("–");
  });
});

describe("esc", () => {
  it("escapes the five HTML specials", () => {
    expect(esc('<a href="x">&\'</a>')).toBe("&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;");
  });

  it("stringifies non-string input", () => {
    expect(esc(5)).toBe("5");
    expect(esc(null)).toBe("null");
  });
});

describe("relTimeUntil", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-23T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders future deadlines with units rounded up", () => {
    expect(relTimeUntil("2026-07-23T12:00:45Z")).toBe("in 45s");
    // 4m30s rounds UP to 5m — a deadline never reads sooner than it is.
    expect(relTimeUntil("2026-07-23T12:04:30Z")).toBe("in 5m");
    expect(relTimeUntil("2026-07-23T15:00:00Z")).toBe("in 3h");
    expect(relTimeUntil("2026-07-25T12:00:00Z")).toBe("in 2d");
  });

  it("reads 'any moment now' once the deadline has passed", () => {
    expect(relTimeUntil("2026-07-23T12:00:00Z")).toBe("any moment now");
    expect(relTimeUntil("2026-07-23T11:00:00Z")).toBe("any moment now");
  });

  it("renders an en dash for invalid or missing input", () => {
    expect(relTimeUntil("junk")).toBe("–");
    expect(relTimeUntil(null)).toBe("–");
    expect(relTimeUntil(undefined)).toBe("–");
  });
});

describe("relDuration", () => {
  it("renders API-reported ages given in seconds", () => {
    expect(relDuration(45)).toBe("45s");
    expect(relDuration(300)).toBe("5m");
    expect(relDuration(7200)).toBe("2h");
    expect(relDuration(200000)).toBe("2d");
    expect(relDuration("90")).toBe("1m");
  });

  it("renders an en dash for negative or non-numeric input", () => {
    expect(relDuration(-1)).toBe("–");
    expect(relDuration("nope")).toBe("–");
    expect(relDuration(undefined)).toBe("–");
    expect(relDuration(Infinity)).toBe("–");
  });
});

describe("agent naming", () => {
  it("labels an agent by name and submission version", () => {
    expect(agentLabel("My agent", 3)).toBe("My agent, Submission v3");
    expect(agentLabel(null, null)).toBe("Unnamed agent, Legacy submission");
  });

  it("falls back to 'Unnamed agent'", () => {
    expect(agentName("My agent")).toBe("My agent");
    expect(agentName(null)).toBe("Unnamed agent");
    expect(agentName("")).toBe("Unnamed agent");
  });

  it("reads a missing version as a legacy submission (v0 is a real version)", () => {
    expect(agentVersionLabel(null)).toBe("Legacy submission");
    expect(agentVersionLabel(undefined)).toBe("Legacy submission");
    expect(agentVersionLabel(0)).toBe("Submission v0");
    expect(agentVersionLabel(4)).toBe("Submission v4");
  });
});

describe("duplicateLabel", () => {
  it("is empty when the entry duplicates nothing", () => {
    expect(duplicateLabel(null)).toBe("");
    expect(duplicateLabel({})).toBe("");
  });

  it("names the duplicated submission when known, else its short id", () => {
    expect(
      duplicateLabel({ duplicate_of: "abc", duplicate_name: "Prior", duplicate_version: 2 }),
    ).toBe("Prior, Submission v2");
    expect(duplicateLabel({ duplicate_of: "0123456789abcdef" })).toBe("Submission 01234567");
  });
});

describe("shortKey", () => {
  it("abbreviates keys longer than 16 characters as first-8…last-6", () => {
    expect(shortKey("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")).toBe("5GrwvaEF…GKutQY");
  });

  it("keeps short keys intact", () => {
    expect(shortKey("abcdef")).toBe("abcdef");
    expect(shortKey("0123456789abcdef")).toBe("0123456789abcdef");
  });

  it("is null-safe", () => {
    expect(shortKey(null)).toBe("");
    expect(shortKey(undefined)).toBe("");
    expect(shortKey("")).toBe("");
  });
});

describe("shortModel", () => {
  it("strips the provider prefix", () => {
    expect(shortModel("qwen/qwen3-32b")).toBe("qwen3-32b");
    expect(shortModel("a/b/c")).toBe("c");
  });

  it("returns unprefixed names unchanged", () => {
    expect(shortModel("gpt-4")).toBe("gpt-4");
  });
});

describe("telemetryCount", () => {
  it("floors valid non-negative numbers", () => {
    expect(telemetryCount(5)).toBe(5);
    expect(telemetryCount(5.9)).toBe(5);
    expect(telemetryCount("7")).toBe(7);
    expect(telemetryCount(0)).toBe(0);
  });

  it("returns 0 for invalid or negative input", () => {
    expect(telemetryCount(-1)).toBe(0);
    expect(telemetryCount("nope")).toBe(0);
    expect(telemetryCount(null)).toBe(0);
    expect(telemetryCount(undefined)).toBe(0);
    expect(telemetryCount(Infinity)).toBe(0);
  });
});

describe("telemetryDuration", () => {
  it("renders an em dash for invalid or negative durations", () => {
    expect(telemetryDuration(-1)).toBe("—");
    expect(telemetryDuration("nope")).toBe("—");
    expect(telemetryDuration(undefined)).toBe("—");
    // Number(null) coerces to 0, so null reads as a zero duration.
    expect(telemetryDuration(null)).toBe("0 ms");
  });

  it("renders milliseconds below one second", () => {
    expect(telemetryDuration(500)).toBe("500 ms");
    expect(telemetryDuration(999.4)).toBe("999 ms");
  });

  it("renders seconds below one minute", () => {
    expect(telemetryDuration(1500)).toBe("1.5 s");
    expect(telemetryDuration(9500)).toBe("9.5 s");
    expect(telemetryDuration(15000)).toBe("15 s");
    // The sub-minute branch rounds up to "60 s" rather than rolling to 1m.
    expect(telemetryDuration(59999)).toBe("60 s");
  });

  it("renders minutes and seconds at one minute and above", () => {
    expect(telemetryDuration(60000)).toBe("1m 0s");
    expect(telemetryDuration(65000)).toBe("1m 5s");
    expect(telemetryDuration(61999)).toBe("1m 2s");
  });
});

describe("elapsedDuration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-23T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("omits leading zero units", () => {
    expect(elapsedDuration("2026-07-23T11:59:55Z")).toBe("5s");
    expect(elapsedDuration("2026-07-23T11:58:55Z")).toBe("1m 5s");
    expect(elapsedDuration("2026-07-23T10:58:55Z")).toBe("1h 1m 5s");
  });

  it("keeps a zero minutes unit when hours are present", () => {
    expect(elapsedDuration("2026-07-23T10:59:55Z")).toBe("1h 0m 5s");
  });

  it("clamps future starts to zero", () => {
    expect(elapsedDuration("2026-07-23T12:01:00Z")).toBe("0s");
  });

  it("returns an empty string for invalid input", () => {
    expect(elapsedDuration("")).toBe("");
    expect(elapsedDuration(null)).toBe("");
    expect(elapsedDuration(undefined)).toBe("");
    expect(elapsedDuration("garbage")).toBe("");
  });
});

describe("timelineDate / releaseTime", () => {
  it("formats in UTC so a late-evening point never rolls to the next day", () => {
    const expected = new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(new Date("2026-01-15T23:30:00Z"));
    expect(timelineDate("2026-01-15T23:30:00Z")).toBe(expected);
    expect(timelineDate(Date.parse("2026-01-15T23:30:00Z"))).toBe(expected);
  });

  it("parses a release's released_at", () => {
    expect(releaseTime({ released_at: "2026-03-01T00:00:00Z" })).toBe(
      Date.parse("2026-03-01T00:00:00Z"),
    );
  });

  it("yields NaN for a release without a parsable date", () => {
    expect(releaseTime({})).toBeNaN();
    expect(releaseTime({ released_at: "junk" })).toBeNaN();
  });
});

describe("athDate", () => {
  it("falls back to 'Not recorded' for missing or unparsable values", () => {
    expect(athDate(null)).toBe("Not recorded");
    expect(athDate(undefined)).toBe("Not recorded");
    expect(athDate("")).toBe("Not recorded");
    expect(athDate("garbage")).toBe("Not recorded");
  });

  it("formats valid values with the medium/short locale styles", () => {
    const iso = "2026-07-01T09:30:00Z";
    const expected = new Date(iso).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
    expect(athDate(iso)).toBe(expected);
  });
});
