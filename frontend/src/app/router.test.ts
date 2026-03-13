import { describe, expect, it } from "vitest";
import { legacyRedirects } from "@/app/router";

describe("legacy redirects", () => {
  it("maps legacy paths to new IA", () => {
    expect(legacyRedirects["/tasks"]).toBe("/runs");
    expect(legacyRedirects["/automations"]).toBe("/workflows");
    expect(legacyRedirects["/system"]).toBe("/observability");
  });
});
