import { describe, expect, it } from "vitest";
import { resolveStatusVariant } from "@/components/common/status-badge";

describe("resolveStatusVariant", () => {
  it("maps failed states to destructive", () => {
    expect(resolveStatusVariant("failed")).toBe("destructive");
    expect(resolveStatusVariant("blocked_budget")).toBe("destructive");
  });

  it("maps running state to warning", () => {
    expect(resolveStatusVariant("running")).toBe("warning");
  });

  it("maps heartbeat runtime-state badges", () => {
    expect(resolveStatusVariant("live")).toBe("success");
    expect(resolveStatusVariant("stale")).toBe("destructive");
    expect(resolveStatusVariant("historical")).toBe("secondary");
  });

  it("falls back to outline", () => {
    expect(resolveStatusVariant("custom_status")).toBe("outline");
  });
});
