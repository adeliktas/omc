import { describe, expect, it } from "vitest";

import { summarizeHarnesses } from "./harness-summary";

describe("summarizeHarnesses", () => {
  it("only exposes coding harnesses and aggregates their live availability", () => {
    const summaries = summarizeHarnesses({
      levels: [
        {
          entries: [
            {
              provider: "openai-codex-2",
              model: "gpt-5.6-terra",
              execution: "agent_harness",
              eligible: false,
              block_reason: "subscription usage limit reached",
              unavailable_until: "2026-09-22T14:05:00+02:00",
              provider_priority: 1,
              observed_usage: { calls: 2, total_tokens: 1200, cost_usd: 0, last_at: "2026-09-22T12:00:00Z" },
              level: 2,
              rank: 2,
            },
            {
              provider: "openai-codex-2",
              model: "gpt-5.6-sol",
              execution: "agent_harness",
              eligible: true,
              provider_priority: 1,
              observed_usage: { calls: 3, total_tokens: 1800, cost_usd: 0.25, last_at: "2026-09-22T13:00:00Z" },
              level: 1,
              rank: 2,
            },
            {
              provider: "vllm",
              model: "Qwen",
              execution: "model_endpoint",
              eligible: true,
              level: 1,
              rank: 0,
            },
          ],
        },
      ],
    });

    expect(summaries).toEqual([
      {
        provider: "openai-codex-2",
        readyModels: 1,
        priority: 1,
        calls: 5,
        totalTokens: 3000,
        costUsd: 0.25,
        lastAt: "2026-09-22T13:00:00Z",
        retryAt: "2026-09-22T14:05:00+02:00",
        reason: "subscription usage limit reached",
        models: [
          expect.objectContaining({ model: "gpt-5.6-sol", eligible: true }),
          expect.objectContaining({ model: "gpt-5.6-terra", eligible: false }),
        ],
      },
    ]);
  });

  it("treats malformed or missing ranking data as no inventory", () => {
    expect(summarizeHarnesses(null)).toEqual([]);
    expect(summarizeHarnesses({ levels: [{ entries: "bad" }] })).toEqual([]);
  });
});
