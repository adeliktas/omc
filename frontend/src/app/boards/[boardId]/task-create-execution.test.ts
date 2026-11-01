import { expect, it } from "vitest";

import { taskCreateExecutionFields } from "./task-create-execution";

it("preserves custom-workspace and gateway execution", () => {
  expect(taskCreateExecutionFields("harness", " /srv/site ")).toEqual({
    execution_lane: "harness", execution_mode: "single", workspace_path: "/srv/site",
  });
  expect(taskCreateExecutionFields("agent", "/srv/site")).toEqual({
    execution_lane: "agent", execution_mode: "single", workspace_path: null,
  });
});

it("sends blank-workspace coding tasks to the running native team pool", () => {
  expect(taskCreateExecutionFields("harness", "   ")).toEqual({
    execution_lane: "harness", execution_mode: "team", workspace_path: null,
  });
});
