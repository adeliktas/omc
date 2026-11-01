import { describe, expect, it } from "vitest";

import {
  taskDisplayStatus,
  taskExecutionLabel,
  usageLabel,
  type TeamAttempt,
} from "./team-cycle";

const base: TeamAttempt = {
  id: "a", member_name: "team-qa", role_kind: "qa", stage_key: "qa", round: 1, attempt_no: 1,
  status: "succeeded", provider: "deepseek-sub", model: "deepseek-flash", verdict: "pass", error: null,
  usage_reported: false, input_tokens: null, output_tokens: null, cache_read_tokens: null,
  cli_cost_estimate_usd: null,
};

describe("observed board work", () => {
  it("keeps parents and idle workers out of In Progress without changing workflow authority", () => {
    const parent = {
      status: "in_progress",
      split_count: 20,
      work_state: "waiting" as const,
    };
    expect(taskDisplayStatus(parent)).toBe("waiting");
    expect(parent.status).toBe("in_progress");
    expect(
      taskDisplayStatus({ status: "in_progress", work_state: "waiting" }),
    ).toBe("waiting");
    expect(taskDisplayStatus({ status: "review" })).toBe("review");
  });

  it("shows only a live running/reviewing lease as active; paused work stays paused", () => {
    for (const work_state of ["running", "reviewing"] as const) {
      const task = {
        status: "review",
        work_state,
        work_expires_at: new Date(Date.now() + 60_000).toISOString(),
      };
      expect(taskDisplayStatus(task)).toBe("in_progress");
      expect(taskDisplayStatus({ ...task, status: "paused" })).toBe("paused");
      expect(
        taskDisplayStatus({
          ...task,
          work_expires_at: new Date(Date.now() - 1).toISOString(),
        }),
      ).toBe("review");
      expect(
        taskDisplayStatus({
          ...task,
          status: "in_progress",
          work_expires_at: "invalid",
        }),
      ).toBe("waiting");
    }
  });
});

describe("task execution labels", () => {
  it("separates native/host execution from gateway assignment without claiming activity", () => {
    expect(taskExecutionLabel({ execution_mode: "team" })).toBe(
      "Native team · see execution",
    );
    expect(taskExecutionLabel({ execution_lane: "harness" })).toBe(
      "Host harness · see execution",
    );
    expect(taskExecutionLabel({ assignee: "Gateway worker" })).toBe(
      "Gateway worker",
    );
    expect(taskExecutionLabel({})).toBe("Unassigned");
  });
});

describe("usageLabel", () => {
  it("never shows zeros for usage that was not reported", () => {
    expect(usageLabel(base)).toBe("usage not reported");
  });

  it("keeps cache reads separate and labels the cost as an unbilled CLI estimate", () => {
    const text = usageLabel({
      ...base, usage_reported: true, input_tokens: 10, output_tokens: 5, cache_read_tokens: 900,
      cli_cost_estimate_usd: 0.1234,
    });
    expect(text).toBe(
      "in 10 · out 5 · cache read 900 · CLI estimate $0.1234 (not billed)",
    );
  });
});
