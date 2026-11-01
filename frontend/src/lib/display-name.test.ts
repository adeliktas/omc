import { describe, expect, it } from "vitest";

import type { OrganizationMemberRead } from "@/api/generated/model";
import {
  approvalMachineActor,
  DEFAULT_HUMAN_LABEL,
  liveFeedActor,
  machineActor,
  normalizeDisplayName,
  resolveHumanActorName,
  resolveMemberDisplayName,
  UNATTRIBUTED_ACTOR,
  unattributedActorName,
} from "./display-name";

const memberWithUser = (user: {
  preferred_name?: string | null;
  name?: string | null;
}): OrganizationMemberRead =>
  ({
    user,
  }) as OrganizationMemberRead;

describe("display-name", () => {
  it("normalizes empty strings to null", () => {
    expect(normalizeDisplayName("   ")).toBeNull();
    expect(normalizeDisplayName(" Abhimanyu ")).toBe("Abhimanyu");
  });

  it("resolves generic labels to fallback names", () => {
    expect(resolveHumanActorName("Admin", "Abhimanyu")).toBe("Abhimanyu");
    expect(resolveHumanActorName(" user ", "Abhimanyu")).toBe("Abhimanyu");
  });

  it("keeps explicit non-generic actor labels", () => {
    expect(resolveHumanActorName("Abhimanyu", "User")).toBe("Abhimanyu");
  });

  it("prefers membership preferred_name over name", () => {
    const member = memberWithUser({
      preferred_name: "Abhimanyu",
      name: "Admin",
    });
    expect(resolveMemberDisplayName(member)).toBe("Abhimanyu");
  });

  it("falls back to membership name when preferred_name missing", () => {
    const member = memberWithUser({
      preferred_name: null,
      name: "Abhimanyu",
    });
    expect(resolveMemberDisplayName(member)).toBe("Abhimanyu");
  });

  it("returns default user label when member names are unavailable", () => {
    expect(resolveMemberDisplayName(null)).toBe(DEFAULT_HUMAN_LABEL);
    expect(resolveMemberDisplayName(memberWithUser({ name: "Admin" }))).toBe(
      DEFAULT_HUMAN_LABEL,
    );
  });
});

describe("machine actors", () => {
  it("names native team members with their actual role and engine", () => {
    expect(
      machineActor(
        "task.comment",
        "**team/team-developer** (developer, pi-neuralwatt/glm-5.3, round 1, attempt 2) finished",
      ),
    ).toEqual({
      name: "team-developer",
      role: "native developer · pi-neuralwatt/glm-5.3",
    });
  });

  it("shows the effort the native attempt actually ran with", () => {
    expect(
      machineActor(
        "task.comment",
        "**team/team-qa** (qa, openai-codex-2/gpt-6.1-sol, effort high, round 1, attempt 1) finished",
      )?.role,
    ).toBe("native qa · openai-codex-2/gpt-6.1-sol · effort high");
  });

  it("names deterministic and host writers", () => {
    expect(
      machineActor(
        "task.comment",
        "**team-coordinator** (deterministic orchestrator, no model): x",
      )?.name,
    ).toBe("Team coordinator");
    expect(
      machineActor("task.comment", "**team-runner**: published")?.name,
    ).toBe("Team runner");
    expect(
      machineActor(
        "task.comment",
        "**agent-runner** (harness=claude-code, rc=1, model=opus-4-8, apply=1) x",
      ),
    ).toEqual({ name: "Coding runner", role: "claude-code/opus-4-8" });
    expect(
      machineActor("task.comment", "**Automated state report** — 2026")?.name,
    ).toBe("Health report");
    expect(
      machineActor("task.comment", "[watchdog-nudge] @Atlas: stuck")?.name,
    ).toBe("Watchdog");
    expect(
      machineActor("task.comment", "[cycle-complete] Recurring task")?.name,
    ).toBe("Watchdog");
    expect(machineActor("task.comment", "[auto-review] decision")?.name).toBe(
      "Standing reviewer",
    );
    expect(machineActor("watchdog.tick", '{"scanned": 3}')?.name).toBe(
      "Watchdog",
    );
    expect(machineActor("test.run", "test=x status=fail")?.name).toBe(
      "Built-in tests",
    );
  });

  it("does not claim human-typed or unknown text", () => {
    expect(
      machineActor("task.comment", "Please also check the login page"),
    ).toBeNull();
    expect(machineActor("task.comment", "[owner-review] Approved.")).toBeNull();
    expect(
      machineActor("task.comment", "see **team-coordinator** above"),
    ).toBeNull();
  });

  it("only defaults typed content to the human", () => {
    expect(unattributedActorName("task.comment", "omc")).toBe("omc");
    expect(unattributedActorName("task.status_changed", "omc")).toBe(
      UNATTRIBUTED_ACTOR,
    );
    expect(unattributedActorName(null, "omc")).toBe(UNATTRIBUTED_ACTOR);
  });
});

describe("machine actor edge cases (independent QA 2026-10-06)", () => {
  it("ignores inherited object keys as event types", () => {
    expect(machineActor("toString", "x")).toBeNull();
    expect(machineActor("__proto__", "x")).toBeNull();
  });

  it("never relabels an explicit human source from its message", () => {
    const chat = {
      agentName: null,
      actorName: "Alice",
      eventType: "board.chat",
      message: "**team-runner**: I copied this diagnostic; is it correct?",
    };
    expect(liveFeedActor(chat, "omc")).toEqual({ name: "Alice", role: null });
    expect(liveFeedActor({ ...chat, actorName: null }, "omc").name).toBe(
      "Team runner",
    );
    expect(liveFeedActor({ ...chat, agentName: "Atlas" }, "omc").name).toBe(
      "Atlas",
    );
    expect(
      liveFeedActor(
        {
          ...chat,
          actorName: null,
          eventType: "task.status_changed",
          message: "moved",
        },
        "omc",
      ).name,
    ).toBe(UNATTRIBUTED_ACTOR);
  });
});

describe("approval actors", () => {
  it("names the standing reviewer from its payload on both feeds", () => {
    const actor = approvalMachineActor(null, { source: "autonomous_review" });
    expect(actor?.name).toBe("Standing reviewer");
    expect(
      liveFeedActor(
        {
          agentName: null,
          actorName: actor?.name,
          eventType: "approval.approved",
          message: "Close approved",
        },
        "omc",
      ).name,
    ).toBe("Standing reviewer");
    expect(
      approvalMachineActor("agent-1", { source: "autonomous_review" }),
    ).toBeNull();
    expect(approvalMachineActor(null, null)).toBeNull();
    expect(approvalMachineActor(null, { source: "owner" })).toBeNull();
  });
});
