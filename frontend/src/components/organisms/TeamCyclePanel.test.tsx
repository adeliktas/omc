import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TeamCyclePanel } from "./TeamCyclePanel";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); delete (window as unknown as { Clerk?: unknown }).Clerk; });

describe("TeamCyclePanel", () => {
  it("ignores late previous-task replies and exposes truthful single-harness configuration", async () => {
    let resolveOld!: (value: Response) => void;
    const old = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const spy = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(old).mockResolvedValue({ ok: true, headers: new Headers({ "Content-Type": "application/json" }), json: async () => ({ cycles: [] }) } as Response);
    const { rerender, unmount } = render(<TeamCyclePanel boardId="b" taskId="old" />);
    rerender(<TeamCyclePanel boardId="b" taskId="new" task={{ execution_mode: "single", execution_lane: "harness" }} />);
    await waitFor(() => expect(screen.getByText(/No cycle yet/)).toBeTruthy());
    await act(async () => resolveOld({ ok: true, headers: new Headers({ "Content-Type": "application/json" }), json: async () => ({ cycles: [{ cycle: { state: "OLD WRONG TASK", round: 1, max_rounds: 2 }, attempts: [] }] }) } as Response));
    await waitFor(() => expect(screen.queryByText(/OLD WRONG TASK/)).toBeNull());
    expect(screen.getByText(/not proof of dispatch/)).toBeTruthy();
    expect(screen.getByText(/external host runs are separate/)).toBeTruthy();
    unmount();
    expect((spy.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  });

  it("refreshes recorded state while open and stops polling on close", async () => {
    vi.useFakeTimers();
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, headers: new Headers({ "Content-Type": "application/json" }), json: async () => ({ cycles: [] }) } as Response);
    const { unmount } = render(<TeamCyclePanel boardId="b" taskId="t" />);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(spy).toHaveBeenCalledTimes(2);
    unmount();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(spy).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
  it("shows roles, engines, verdict and observed usage per attempt", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      headers: new Headers({ "Content-Type": "application/json" }),
      json: async () => ({
        cycles: [
          {
            cycle: { id: "c", state: "qa_passed", round: 1, max_rounds: 2, block_reason: null },
            attempts: [
              {
                id: "1", member_name: "team-qa", role_kind: "qa", stage_key: "qa", round: 1, attempt_no: 1,
                status: "succeeded", provider: "deepseek-sub", model: "deepseek-flash", verdict: "pass",
                error: null, usage_reported: false, input_tokens: null, output_tokens: null,
                cache_read_tokens: null, cli_cost_estimate_usd: null,
              },
            ],
          },
        ],
      }),
    } as Response);
    render(<TeamCyclePanel boardId="b" taskId="t" />);
    await waitFor(() => expect(screen.getByText(/qa_passed · round 1\/2/)).toBeTruthy());
    expect(screen.getByText("team-qa")).toBeTruthy();
    expect(screen.getByText(/deepseek-sub\/deepseek-flash/)).toBeTruthy();
    expect(screen.getByText(/verdict pass/)).toBeTruthy();
    expect(screen.getByText("usage not reported")).toBeTruthy();
    expect(screen.getByText(/harness Pi CLI/)).toBeTruthy();
    expect(screen.getByText(/Host coordinator \(no model\)/)).toBeTruthy();
  });

  it("says when no cycle exists and surfaces API errors", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, headers: new Headers({ "Content-Type": "application/json" }), json: async () => ({ cycles: [] }) } as Response);
    const { unmount } = render(<TeamCyclePanel boardId="b" taskId="t" />);
    await waitFor(() => expect(screen.getByText(/No cycle yet/)).toBeTruthy());
    unmount();
    spy.mockResolvedValue(new Response("HTTP 403", { status: 403 }));
    render(<TeamCyclePanel boardId="b" taskId="t" />);
    await waitFor(() => expect(screen.getByText(/HTTP 403/)).toBeTruthy());
  });
});

it("uses the existing Clerk transport for cross-origin execution records", async () => {
  vi.stubEnv("NEXT_PUBLIC_AUTH_MODE", "clerk");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://synthetic-backend.invalid");
  (window as unknown as { Clerk: unknown }).Clerk = { session: { getToken: async () => "synthetic-not-a-credential" } };
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) =>
    new Response(JSON.stringify({ cycles: [] }), { status: new Headers(init?.headers).has("Authorization") ? 200 : 401, headers: { "Content-Type": "application/json" } }));
  render(<TeamCyclePanel boardId="b" taskId="t" />);
  await waitFor(() => expect(screen.getByText(/No cycle yet/)).toBeTruthy());
  expect(new Headers(spy.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer synthetic-not-a-credential");
  expect(screen.queryByRole("alert")).toBeNull();
});
