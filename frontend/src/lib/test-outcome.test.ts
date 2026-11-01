import { describe, expect, it } from "vitest";

import {
  displayTestStatus,
  filterTests,
  isNotVerified,
} from "@/lib/test-outcome";

const tests = [
  { status: "pass", category: "infra", last_run: { detail: "SELECT 1 OK" } },
  {
    status: "pass",
    category: "pipeline",
    last_run: { detail: "vacuous — no work on quiet board" },
  },
  { status: "fail", category: "pm", last_run: { detail: "1 task unassigned" } },
  {
    status: "stale",
    category: "infra",
    last_run: { detail: "system signature changed" },
  },
  {
    status: "skipped",
    category: "external",
    last_run: { detail: "external dependency omitted" },
  },
];

describe("test display outcomes", () => {
  it("keeps vacuous passes out of verified passes without conflating skipped tests", () => {
    expect(tests.filter(isNotVerified)).toHaveLength(1);
    expect(tests.map(displayTestStatus)).toEqual([
      "pass",
      "not-verified",
      "fail",
      "stale",
      "skipped",
    ]);
  });

  it("filters the neutral not-verified result independently", () => {
    expect(filterTests(tests, "not-verified")).toEqual([tests[1]]);
    expect(filterTests(tests, "pass")).toEqual([tests[0], tests[1]]);
    expect(filterTests(tests, "external")).toEqual([tests[4]]);
  });
});
