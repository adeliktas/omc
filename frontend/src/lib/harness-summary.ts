export type HarnessEntry = {
  provider: string;
  model: string;
  execution?: string;
  eligible?: boolean;
  disabled?: boolean;
  availability_reason?: string | null;
  block_reason?: string | null;
  unavailable_until?: string | null;
  provider_priority?: number;
  observed_usage?: {
    calls?: number;
    total_tokens?: number;
    cost_usd?: number;
    last_at?: string;
  };
  level?: number;
  rank?: number;
};

export type HarnessSummary = {
  provider: string;
  models: HarnessEntry[];
  readyModels: number;
  priority: number | null;
  calls: number;
  totalTokens: number;
  costUsd: number;
  lastAt: string | null;
  retryAt: string | null;
  reason: string | null;
};

type RankingLevel = { entries?: unknown };
type RankingResponse = { levels?: unknown };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function toHarnessEntry(value: unknown): HarnessEntry | null {
  if (!isRecord(value)) return null;
  const provider = value.provider;
  const model = value.model;
  if (typeof provider !== "string" || typeof model !== "string") return null;
  if (value.execution !== "agent_harness") return null;

  return {
    provider,
    model,
    execution: "agent_harness",
    eligible: value.eligible === true,
    disabled: value.disabled === true,
    availability_reason:
      typeof value.availability_reason === "string" ? value.availability_reason : null,
    block_reason: typeof value.block_reason === "string" ? value.block_reason : null,
    unavailable_until:
      typeof value.unavailable_until === "string" ? value.unavailable_until : null,
    provider_priority:
      typeof value.provider_priority === "number" ? value.provider_priority : undefined,
    observed_usage: isRecord(value.observed_usage)
      ? {
          calls: typeof value.observed_usage.calls === "number" ? value.observed_usage.calls : 0,
          total_tokens: typeof value.observed_usage.total_tokens === "number" ? value.observed_usage.total_tokens : 0,
          cost_usd: typeof value.observed_usage.cost_usd === "number" ? value.observed_usage.cost_usd : 0,
          last_at: typeof value.observed_usage.last_at === "string" ? value.observed_usage.last_at : undefined,
        }
      : undefined,
    level: typeof value.level === "number" ? value.level : undefined,
    rank: typeof value.rank === "number" ? value.rank : undefined,
  };
}

/**
 * Ranking is the source of truth for both routed endpoints and coding harnesses.
 * The generated schema deliberately permits additional ranking metadata, so this
 * adapter validates only the fields needed by the read-only summary.
 */
export function summarizeHarnesses(ranking: unknown): HarnessSummary[] {
  if (!isRecord(ranking)) return [];
  const levels = (ranking as RankingResponse).levels;
  if (!Array.isArray(levels)) return [];

  const groups = new Map<string, HarnessEntry[]>();
  for (const level of levels as RankingLevel[]) {
    if (!isRecord(level) || !Array.isArray(level.entries)) continue;
    for (const rawEntry of level.entries) {
      const entry = toHarnessEntry(rawEntry);
      if (!entry) continue;
      const entries = groups.get(entry.provider) ?? [];
      entries.push(entry);
      groups.set(entry.provider, entries);
    }
  }

  return [...groups.entries()]
    .map(([provider, models]) => {
      const sortedModels = [...models].sort(
        (left, right) => (left.level ?? Infinity) - (right.level ?? Infinity) ||
          (left.rank ?? Infinity) - (right.rank ?? Infinity) ||
          left.model.localeCompare(right.model),
      );
      const blocked = sortedModels.find((entry) => !entry.eligible);
      return {
        provider,
        models: sortedModels,
        readyModels: sortedModels.filter((entry) => entry.eligible).length,
        priority: sortedModels.reduce<number | null>(
          (lowest, entry) => entry.provider_priority == null
            ? lowest
            : Math.min(lowest ?? entry.provider_priority, entry.provider_priority),
          null,
        ),
        calls: sortedModels.reduce((total, entry) => total + (entry.observed_usage?.calls ?? 0), 0),
        totalTokens: sortedModels.reduce((total, entry) => total + (entry.observed_usage?.total_tokens ?? 0), 0),
        costUsd: sortedModels.reduce((total, entry) => total + (entry.observed_usage?.cost_usd ?? 0), 0),
        lastAt: sortedModels
          .map((entry) => entry.observed_usage?.last_at)
          .filter((value): value is string => Boolean(value))
          .sort()
          .at(-1) ?? null,
        retryAt: blocked?.unavailable_until ?? null,
        reason: blocked?.availability_reason ?? blocked?.block_reason ?? null,
      };
    })
    .sort((left, right) => left.provider.localeCompare(right.provider));
}
