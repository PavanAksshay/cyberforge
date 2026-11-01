import { describe, it, expect } from "vitest";
import { formatCurrency, relativeTime, averageMoney } from "./utils";
describe("truthful presentation", () => {
  it("renders source USD independently from EUR dashboard", () => {
    expect(formatCurrency("506.97", "USD")).toBe("$506.97");
    expect(formatCurrency("506.97", "EUR")).toBe("€506.97");
  });
  it("preserves cents beyond JavaScript safe integer precision", () => {
    expect(formatCurrency("9999999999999999.99", "USD")).toBe(
      "$9,999,999,999,999,999.99",
    );
  });
  it("keeps materialization and fetch timestamps independent", () => {
    expect(relativeTime(new Date(Date.now() - 47000))).toBe("47s ago");
    expect(relativeTime(new Date(Date.now() - 120000))).toBe("2m ago");
  });
});

it("rounds shipped average value in exact cents and handles no shipments", () => {
  expect(averageMoney("2534.85", 30)).toBe("84.50");
  expect(averageMoney("506.97", 6)).toBe("84.50");
  expect(averageMoney("0.00", 0)).toBeNull();
});
