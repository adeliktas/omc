import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import RankingPage from "./page";

vi.mock("@/auth/clerk", () => ({
  useAuth: () => ({ isSignedIn: true }),
  SignedIn: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SignedOut: () => null,
  SignInButton: () => null,
}));
vi.mock("@/components/organisms/DashboardSidebar", () => ({ DashboardSidebar: () => null }));
vi.mock("@/components/templates/DashboardShell", () => ({
  DashboardShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/auth/localAuth", () => ({ isLocalAuthMode: () => false }));
vi.mock("@/lib/api-base", () => ({ getApiBaseUrl: () => "http://localhost" }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each([undefined, false, true, "unknown"])(
  "defaults unknown trust to denied and saves a per-model boolean (%s)",
  async (flag) => {
    const entry = {
      provider: "DN-jetsonagx-qwen", model: "Qwen3.8-27B-FP8", level: 3, rank: 1,
      provider_priority: 1, max_complexity: 3, cost_class: "local", disabled: false,
      fully_trusted: flag, execution: "model_endpoint", picked_for: [],
      harness_picked_for: [], usage: [], eligible: false, provider_status: "unregistered",
    };
    const board = { levels: [{ level: 3, entries: [entry] }] };
    const fetch = vi.fn(async () => ({ ok: true, json: async () => board }));
    vi.stubGlobal("fetch", fetch);
    render(<RankingPage />);
    const checkbox = await screen.findByRole("checkbox", { name: /fully trusted/i, hidden: true });
    expect(checkbox).toHaveProperty("checked", flag === true);
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: /save metadata/i, hidden: true }));
    await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(3));
    const calls = fetch.mock.calls as unknown as [string, RequestInit][];
    const saved = calls.find(([url, options]) => url.endsWith("/ranking/entry") && options.method === "PUT");
    expect(saved).toBeDefined();
    expect(JSON.parse(saved![1].body as string)).toMatchObject({
      provider: entry.provider, model: entry.model, fully_trusted: flag !== true,
    });
  },
);
