import { describe, expect, it } from "vitest";

import { formatCost } from "@/lib/utils/format";

describe("formatCost", () => {
  it("shows up to 8 decimal places by default", () => {
    expect(formatCost(0.00001234)).toBe("$0.00001234");
  });

  it("returns a stable fallback for non-numeric values", () => {
    expect(formatCost(undefined)).toBe("$0.00000000");
  });

  it("respects explicit precision override", () => {
    expect(formatCost(0.123456789, 6)).toBe("$0.123457");
  });
});
