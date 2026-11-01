"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/mutator";
import {
  getGetAgentApiV1AgentsAgentIdGetQueryKey,
  useGetAgentApiV1AgentsAgentIdGet,
  useUpdateAgentApiV1AgentsAgentIdPatch,
  type getAgentApiV1AgentsAgentIdGetResponse,
} from "@/api/generated/agents/agents";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// tool_config is loosely typed ({[key:string]:unknown}|null); describe the
// shape we read/write locally.
interface ToolConfigShape {
  tools?: {
    profile?: string | null;
    allow?: string[];
    deny?: string[];
  } | null;
  skills?: string[] | null;
}

const SECTION_CLASS =
  "rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] p-5";
const LABEL_CLASS =
  "text-xs font-semibold uppercase tracking-[0.2em] text-quiet";

const parseList = (raw: string): string[] =>
  raw
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

export function AgentToolConfigEditor({ agentId }: { agentId: string }) {
  const queryClient = useQueryClient();
  const agentKey = getGetAgentApiV1AgentsAgentIdGetQueryKey(agentId);

  const agentQuery = useGetAgentApiV1AgentsAgentIdGet<
    getAgentApiV1AgentsAgentIdGetResponse,
    ApiError
  >(agentId, {
    query: {
      enabled: Boolean(agentId),
      refetchOnMount: "always",
      retry: false,
    },
  });

  const toolConfig = useMemo<ToolConfigShape | null>(() => {
    if (agentQuery.data?.status !== 200) return null;
    const raw = agentQuery.data.data.tool_config;
    return (raw as ToolConfigShape | null) ?? null;
  }, [agentQuery.data]);

  // Controlled-with-fallback: state stays undefined until the user edits, then
  // renders fall back to the loaded config (avoids setState-in-effect).
  const [profile, setProfile] = useState<string | undefined>(undefined);
  const [allow, setAllow] = useState<string | undefined>(undefined);
  const [deny, setDeny] = useState<string | undefined>(undefined);
  const [skills, setSkills] = useState<string | undefined>(undefined);
  const [feedback, setFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);

  const resolvedProfile = profile ?? toolConfig?.tools?.profile ?? "";
  const resolvedAllow = allow ?? (toolConfig?.tools?.allow ?? []).join(", ");
  const resolvedDeny = deny ?? (toolConfig?.tools?.deny ?? []).join(", ");
  const resolvedSkills = skills ?? (toolConfig?.skills ?? []).join(", ");

  const updateMutation = useUpdateAgentApiV1AgentsAgentIdPatch<ApiError>({
    mutation: {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: agentKey });
      },
    },
  });

  const handleSave = () => {
    setFeedback(null);
    const allowList = parseList(resolvedAllow);
    const denyList = parseList(resolvedDeny);
    const skillsList = parseList(resolvedSkills);
    const trimmedProfile = resolvedProfile.trim();

    const isEmpty =
      !trimmedProfile &&
      allowList.length === 0 &&
      denyList.length === 0 &&
      skillsList.length === 0;

    const toolConfigPayload: ToolConfigShape | null = isEmpty
      ? null
      : {
          tools: {
            profile: trimmedProfile ? trimmedProfile : null,
            allow: allowList,
            deny: denyList,
          },
          skills: skillsList,
        };

    updateMutation.mutate(
      {
        agentId,
        data: {
          tool_config: toolConfigPayload as { [key: string]: unknown } | null,
        },
      },
      {
        onSuccess: () =>
          setFeedback({
            kind: "success",
            message: isEmpty
              ? "Overrides cleared."
              : "Tool overrides saved.",
          }),
        onError: (err) =>
          setFeedback({
            kind: "error",
            message: err.message || "Save failed.",
          }),
      },
    );
  };

  if (agentQuery.isLoading) {
    return (
      <section className={SECTION_CLASS}>
        <p className="text-sm text-muted">Loading tool configuration…</p>
      </section>
    );
  }

  if (agentQuery.error) {
    return (
      <section className={SECTION_CLASS}>
        <p className={LABEL_CLASS}>Tools &amp; Integrations</p>
        <p className="mt-3 rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-xs text-muted">
          {agentQuery.error.message}
        </p>
      </section>
    );
  }

  return (
    <section className={SECTION_CLASS}>
      <div className="flex items-center justify-between">
        <p className={LABEL_CLASS}>Tools &amp; Integrations</p>
      </div>
      <p className="mt-1 text-xs text-muted">
        Per-agent overrides layered over the global defaults. MCP tools are
        referenced as <code className="font-mono">server__tool</code>. Leave all
        fields blank to clear overrides and inherit defaults.
      </p>

      <div className="mt-4 space-y-4">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted">Profile</label>
          <Input
            value={resolvedProfile}
            onChange={(e) => setProfile(e.target.value)}
            placeholder="e.g. default"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted">
            Allow (comma-separated)
          </label>
          <Input
            value={resolvedAllow}
            onChange={(e) => setAllow(e.target.value)}
            placeholder="Read, Write, server__tool"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted">
            Deny (comma-separated)
          </label>
          <Input
            value={resolvedDeny}
            onChange={(e) => setDeny(e.target.value)}
            placeholder="Bash"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted">
            Skills (comma-separated)
          </label>
          <Input
            value={resolvedSkills}
            onChange={(e) => setSkills(e.target.value)}
            placeholder="research, summarize"
          />
        </div>

        {feedback ? (
          <p
            className={
              feedback.kind === "success"
                ? "text-xs text-[color:var(--success)]"
                : "text-xs text-[color:var(--danger)]"
            }
          >
            {feedback.message}
          </p>
        ) : null}

        <Button
          size="sm"
          onClick={handleSave}
          disabled={updateMutation.isPending}
        >
          {updateMutation.isPending ? "Saving…" : "Save tool config"}
        </Button>
      </div>
    </section>
  );
}
