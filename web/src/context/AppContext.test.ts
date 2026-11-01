import { describe, it, expect } from "vitest";
import { computeDateRange } from "./AppContext";
const coverage = { start: "2026-10-01T00:00:00Z", end: "2026-11-02T00:00:00Z" };
describe("coverage-backed UTC date ranges", () => {
  it("uses the complete verified range rather than a hardcoded seven days", () => {
    expect(computeDateRange("all", coverage)).toEqual(coverage);
  });
  it("latest day includes current-date imports and stays aligned to midnight", () => {
    expect(computeDateRange("24h", coverage)).toEqual({
      start: "2026-11-01T00:00:00Z",
      end: coverage.end,
    });
  });
  it("latest three days are clamped to available coverage", () => {
    expect(computeDateRange("3d", coverage)).toEqual({
      start: "2026-10-30T00:00:00Z",
      end: coverage.end,
    });
    const one = { start: "2026-11-01T00:00:00Z", end: coverage.end };
    expect(computeDateRange("3d", one)).toEqual(one);
  });
});
