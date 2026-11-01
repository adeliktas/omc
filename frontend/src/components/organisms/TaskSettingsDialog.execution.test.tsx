import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TaskSettingsDialog } from "./TaskSettingsDialog";

afterEach(() => vi.restoreAllMocks());

it.each(["single", "team"])("preserves explicit %s/no-workspace execution and exact slash-containing pins on edit", async (mode) => {
  const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ items: [], cycles: [] }), { status: 200, headers: { "Content-Type": "application/json" } }));
  const saved = vi.fn();
  render(<TaskSettingsDialog open onClose={() => {}} boardId="synthetic" taskId="synthetic" taskTitle="Safe fixture" onSaved={saved} initial={{ execution_lane: "harness", execution_mode: mode, workspace_path: null, harness_provider: "omniroute-api", harness_model: "vllm/Qwen3.8-27B-FP8", max_seconds_budget: 3600 }} />);
  expect(screen.getByLabelText(/^Mode(?!l)/)).toHaveValue(mode);
  const seconds = screen.getByLabelText(/^Seconds/);
  expect(seconds).toHaveValue(3600);
  expect(seconds).toHaveAttribute("readonly");
  fireEvent.click(screen.getByRole("button", { name: /Save/ }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  const patch = fetcher.mock.calls.find(([, init]) => init?.method === "PATCH");
  const payload = JSON.parse(patch![1]!.body as string);
  expect(payload).not.toHaveProperty("max_seconds_budget");
  expect(payload).toEqual({ execution_lane: "harness", execution_mode: mode, workspace_path: null, harness_provider: "omniroute-api", harness_model: "vllm/Qwen3.8-27B-FP8" });
});
