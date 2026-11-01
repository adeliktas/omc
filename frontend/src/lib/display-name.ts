import type { OrganizationMemberRead } from "@/api/generated/model";

export const DEFAULT_HUMAN_LABEL = "User";

export const normalizeDisplayName = (
  value: string | null | undefined,
): string | null => {
  const trimmed = (value ?? "").trim();
  return trimmed.length > 0 ? trimmed : null;
};

export const resolveHumanActorName = (
  value: string | null | undefined,
  fallbackName: string = DEFAULT_HUMAN_LABEL,
): string => {
  const normalized = normalizeDisplayName(value);
  if (!normalized) return fallbackName;
  const lowered = normalized.toLowerCase();
  if (lowered === "admin" || lowered === "user") {
    return fallbackName;
  }
  return normalized;
};

export const resolveMemberDisplayName = (
  member: OrganizationMemberRead | null | undefined,
  fallbackName: string = DEFAULT_HUMAN_LABEL,
): string =>
  resolveHumanActorName(
    normalizeDisplayName(member?.user?.preferred_name) ??
      normalizeDisplayName(member?.user?.name),
    fallbackName,
  );

// Events without a gateway agent were all shown as the signed-in human, so the
// team coordinator, native workers, host runners and the watchdog read as the
// owner's own actions (2026-10-06). Machine writers prefix their messages; this
// names them from that prefix. Anything else stays unattributed or human-typed.
export type MachineActor = { name: string; role: string | null };

export const UNATTRIBUTED_ACTOR = "Unattributed";

const MACHINE_EVENT_TYPES = new Map<string, MachineActor>([
  ["watchdog.tick", { name: "Watchdog", role: "system" }],
  ["test.run", { name: "Built-in tests", role: "system" }],
]);

const MACHINE_MARKERS: Array<[RegExp, (m: RegExpMatchArray) => MachineActor]> =
  [
    [
      /^\*\*team-coordinator\*\*/,
      () => ({ name: "Team coordinator", role: "deterministic, no model" }),
    ],
    [
      /^\*\*team-runner\*\*/,
      () => ({ name: "Team runner", role: "host supervisor" }),
    ],
    [
      /^\*\*team\/([\w-]+)\*\*\s*\(([\w-]+), ([^,)]+)(?:, effort ([\w-]+))?/,
      (m) => ({
        name: m[1],
        role: `native ${m[2]} · ${m[3]}` + (m[4] ? ` · effort ${m[4]}` : ""),
      }),
    ],
    [
      /^\*\*team\/([\w-]+)\*\*/,
      (m) => ({ name: m[1], role: "native team member" }),
    ],
    [
      /^\*\*agent-runner\*\*\s*\(harness=([\w-]+), rc=-?\d+, model=([\w.:-]+)/,
      (m) => ({ name: "Coding runner", role: `${m[1]}/${m[2]}` }),
    ],
    [/^\*\*agent-runner\*\*/, () => ({ name: "Coding runner", role: "host" })],
    [
      /^\*\*Automated state report\*\*/,
      () => ({ name: "Health report", role: "host cron" }),
    ],
    [/^\[auto-review\]/, () => ({ name: "Standing reviewer", role: "system" })],
    [
      /^\[(?:watchdog-[\w-]+|cycle-complete|owner-directive-[\w-]+|artifact-[\w-]+|repeat-reset[^\]]*)\]/,
      () => ({ name: "Watchdog", role: "system" }),
    ],
  ];

export const machineActor = (
  eventType: string | null | undefined,
  message: string | null | undefined,
): MachineActor | null => {
  const byType = eventType ? MACHINE_EVENT_TYPES.get(eventType) : undefined;
  if (byType) return byType;
  const text = (message ?? "").trimStart();
  for (const [pattern, actor] of MACHINE_MARKERS) {
    const match = text.match(pattern);
    if (match) return actor(match);
  }
  return null;
};

// Only typed content (comments, chat, commands) defaults to the signed-in human;
// a status change or other record without any actor is not the human's by default.
const HUMAN_TYPED_EVENTS = new Set([
  "task.comment",
  "board.chat",
  "board.command",
]);

export const unattributedActorName = (
  eventType: string | null | undefined,
  humanName: string,
): string =>
  eventType && HUMAN_TYPED_EVENTS.has(eventType)
    ? humanName
    : UNATTRIBUTED_ACTOR;

// Board live feed: a gateway agent's name first, then an explicit human source
// (board chat); only records with neither are named from their message prefix.
export const liveFeedActor = (
  item: {
    agentName: string | null;
    actorName: string | null | undefined;
    eventType: string;
    message: string | null;
  },
  humanName: string,
): MachineActor => {
  if (item.agentName) return { name: item.agentName, role: null };
  if (item.actorName)
    return {
      name: resolveHumanActorName(item.actorName, humanName),
      role: null,
    };
  return (
    machineActor(item.eventType, item.message) ?? {
      name: unattributedActorName(item.eventType, humanName),
      role: null,
    }
  );
};

// The standing reviewer records its approval decisions without a gateway agent;
// its payload says so. Shared by the activity feed and the board live feed.
export const approvalMachineActor = (
  agentId: string | null | undefined,
  payload: unknown,
): MachineActor | null =>
  !agentId &&
  payload !== null &&
  typeof payload === "object" &&
  (payload as Record<string, unknown>).source === "autonomous_review"
    ? { name: "Standing reviewer", role: "system" }
    : null;
