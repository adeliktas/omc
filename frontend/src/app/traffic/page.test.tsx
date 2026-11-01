import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import TrafficPage from "./page";

vi.mock("@/auth/clerk", () => ({ SignedIn: ({ children }: { children: ReactNode }) => children, SignedOut: () => null }));
vi.mock("@/components/templates/DashboardShell", () => ({ DashboardShell: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/organisms/DashboardSidebar", () => ({ DashboardSidebar: () => null }));
vi.mock("@/components/ProviderStatsBar", () => ({ ProviderStatsBar: () => null }));
vi.mock("@/components/HarnessSummary", () => ({ HarnessSummary: () => null }));
vi.mock("@/lib/harness-summary", () => ({ summarizeHarnesses: () => [] }));
afterEach(() => vi.unstubAllGlobals());

it("shows task-bound native execution separately from HTTP flows without inventing usage or tools", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, json: async () => {
    if (url.includes("native-attempts")) return [{
      attempt_id: "attempt-123456789", cycle_id: "cycle", task_id: "task", board_id: "board",
      task_title: "Retained original work", member_name: "QA", role_kind: "qa", stage_key: "qa",
      provider: "qualified-provider", model: "model", status: "running", started_at: null,
      usage_reported: false, total_tokens: null, tool_counts: null,
    }];
    if (url.includes("flows")) return [];
    if (url.includes("stats")) return { flow_count: 0, total_tokens: 0, errors: 0, agents: [] };
    return null;
  } })));
  render(<TrafficPage />);
  expect(await screen.findByRole("link", { name: "Retained original work" })).toHaveAttribute("href", "/boards/board?taskId=task");
  expect(screen.getByText("qualified-provider/model")).toBeInTheDocument();
  expect(screen.getByText("running")).toBeInTheDocument();
  expect(screen.getByText("Not observed")).toBeInTheDocument();
  expect(screen.getByText("Not reported")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Native task attempts" })).toBeInTheDocument();
});
