import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewPage from "./page";

vi.mock("@/auth/clerk", () => ({
  useAuth: () => ({ isSignedIn: true }),
  SignedIn: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SignedOut: () => null,
  SignInButton: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/organisms/DashboardSidebar", () => ({ DashboardSidebar: () => null }));
vi.mock("@/components/templates/DashboardShell", () => ({ DashboardShell: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/lib/api-base", () => ({ getApiBaseUrl: () => "http://test" }));
vi.mock("@/auth/localAuth", () => ({ isLocalAuthMode: () => true, getLocalAuthToken: () => "test-only" }));
afterEach(() => vi.unstubAllGlobals());

it("shows exact ordered intelligence, honest eligibility, and operator pause control", async () => {
  const policy = {
    enabled: true,
    policy: "Independent routine acceptance; major new decisions go to the owner.",
    models: [
      { provider: "openai-codex-2", model: "gpt-6.1-sol", eligible: false, block_reason: "quota_exhausted" },
      { provider: "claude-code", model: "opus-5-5", eligible: false, block_reason: "quota_exhausted" },
      { provider: "pi-neuralwatt", model: "glm-5.3", eligible: true, block_reason: null },
    ],
  };
  const fetch = vi.fn(async (url: string, options?: RequestInit) => ({
    ok: true,
    json: async () => url.endsWith("/items") ? [] : options?.method === "PUT" ? { ...policy, enabled: false } : policy,
  }));
  vi.stubGlobal("fetch", fetch);
  render(<ReviewPage />);
  await screen.findByRole("heading", { name: "Routine acceptance enabled" });
  const choices = screen.getAllByRole("listitem").map((item) => item.textContent);
  expect(choices).toEqual(["GPT-6.1 Sol — unavailable (quota_exhausted)", "Claude Opus 5.5 — unavailable (quota_exhausted)", "GLM-5.3 — eligible"]);
  fireEvent.click(screen.getByRole("button", { name: "Pause routine reviews" }));
  await screen.findByRole("heading", { name: "Routine acceptance paused" });
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test/api/v1/review/automation", expect.objectContaining({ method: "PUT", body: '{"enabled":false}' })));
});

it("shows a fleet failure as one line with task choices; Retry resumes, raises the cap and resolves", async () => {
  const item = {
    id: "r1", task_id: "t1", task_board_id: "b1", task_status: "paused", task_resume_status: "blocked", task_tokens_used: 3_000_000,
    title: "MAJOR: task failed again after meta-debug: Migrate DETECT terminology", type: "task-failure",
    summary: "# Handoff\n" + "x".repeat(9000), reason: "Failed again after its meta-debug task.", proposed_action: "Retry, cancel or keep paused.",
    submitted_by: "fleet-supervisor", submitted_at: "2026-10-24T10:08:33Z", updated_at: "2026-10-24T10:08:33Z", status: "pending",
    reviewed_at: null, reviewer_note: null, snooze_until: null, escalated_to_board_id: null, reject_options: ["Roll out canary"],
  };
  const fetch = vi.fn(async (url: string, _options?: RequestInit) => ({
    ok: true, text: async () => "",
    json: async () => url.endsWith("/items") ? [item] : url.endsWith("/automation") ? { enabled: false, policy: "", models: [] } : {},
  }));
  vi.stubGlobal("fetch", fetch);
  render(<ReviewPage />);
  await screen.findByRole("heading", { name: "Migrate DETECT terminology" });
  expect(screen.getByText("Failed again after its meta-debug task.")).toBeTruthy();
  expect(screen.queryByText(/x{100}/)).toBeNull();  // the handoff lives on the report page
  expect(screen.queryByText("Roll out canary")).toBeNull();
  expect(screen.getByRole("link", { name: "Full report" }).getAttribute("href")).toBe("/review/r1");
  fireEvent.click(screen.getByRole("button", { name: "Retry (+800k tokens)" }));
  await waitFor(() => expect(fetch.mock.calls.filter(([, o]) => o?.method === "PATCH").length).toBe(4));
  const patches = fetch.mock.calls.filter(([, o]) => o?.method === "PATCH").map(([u, o]) => [u, JSON.parse(String(o?.body))]);
  expect(patches[0]).toEqual(["http://test/api/v1/boards/b1/tasks/t1", { max_tokens_budget: 3_800_000 }]);
  expect(patches[1][1]).toEqual({ status: "blocked" });
  expect(patches[2][1].status).toBe("inbox");
  expect(patches[3][0]).toBe("http://test/api/v1/review/items/r1");
  expect(patches[3][1].action).toBe("resolve");
});
