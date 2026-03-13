import { describe, expect, it } from "vitest";
import { parsePromMetrics } from "@/features/telemetry/queries";

describe("parsePromMetrics", () => {
  it("parses metric rows and ignores comments", () => {
    const raw = [
      "# HELP mission_control_tasks_created_total counter",
      "mission_control_tasks_created_total 10",
      "mission_control_runs_db_total 4",
      ""
    ].join("\n");

    expect(parsePromMetrics(raw)).toEqual({
      mission_control_tasks_created_total: 10,
      mission_control_runs_db_total: 4
    });
  });
});
