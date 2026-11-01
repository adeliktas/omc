"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/mutator";
import {
  getGetRuntimeSettingsApiV1SettingsRuntimeGetQueryKey,
  useGetRuntimeSettingsApiV1SettingsRuntimeGet,
  usePatchRuntimeSettingApiV1SettingsRuntimeKeyPatch,
  type getRuntimeSettingsApiV1SettingsRuntimeGetResponse,
} from "@/api/generated/settings/settings";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const TOOLS_KEY = "AGENT_DEFAULT_TOOLS";
const SKILLS_KEY = "AGENT_DEFAULT_SKILLS";

// The generated settings response is loose; describe the shape we rely on.
interface RuntimeSetting {
  key: string;
  value: string | null;
  type?: string;
  section?: string;
  description?: string;
}

interface RuntimeSettingsResponse {
  settings: RuntimeSetting[];
  sections?: Record<string, unknown>;
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

export function AgentDefaultsPanel() {
  const queryClient = useQueryClient();
  const settingsKey = getGetRuntimeSettingsApiV1SettingsRuntimeGetQueryKey();

  const settingsQuery = useGetRuntimeSettingsApiV1SettingsRuntimeGet<
    getRuntimeSettingsApiV1SettingsRuntimeGetResponse,
    ApiError
  >({
    query: {
      refetchOnMount: "always",
    },
  });

  const settings = useMemo<RuntimeSetting[]>(() => {
    if (settingsQuery.data?.status !== 200) return [];
    const body = settingsQuery.data.data as unknown as RuntimeSettingsResponse;
    return body.settings ?? [];
  }, [settingsQuery.data]);

  const toolsSetting = settings.find((s) => s.key === TOOLS_KEY);
  const skillsSetting = settings.find((s) => s.key === SKILLS_KEY);

  // Derive the persisted values so we can fall back to them until the user edits.
  const loadedTools = useMemo(() => {
    const raw = toolsSetting?.value;
    if (!raw) return { profile: "", allow: "", deny: "" };
    try {
      const parsed = JSON.parse(raw) as {
        profile?: string | null;
        allow?: string[];
        deny?: string[];
      };
      return {
        profile: parsed.profile ?? "",
        allow: (parsed.allow ?? []).join(", "),
        deny: (parsed.deny ?? []).join(", "),
      };
    } catch {
      return { profile: "", allow: "", deny: "" };
    }
  }, [toolsSetting?.value]);

  const loadedSkills = useMemo(() => {
    const raw = skillsSetting?.value;
    if (!raw) return "";
    try {
      const parsed = JSON.parse(raw) as string[];
      return Array.isArray(parsed) ? parsed.join(", ") : "";
    } catch {
      return "";
    }
  }, [skillsSetting?.value]);

  // Controlled-with-fallback: undefined until edited, then falls back to loaded.
  const [profile, setProfile] = useState<string | undefined>(undefined);
  const [allow, setAllow] = useState<string | undefined>(undefined);
  const [deny, setDeny] = useState<string | undefined>(undefined);
  const [skills, setSkills] = useState<string | undefined>(undefined);

  const resolvedProfile = profile ?? loadedTools.profile;
  const resolvedAllow = allow ?? loadedTools.allow;
  const resolvedDeny = deny ?? loadedTools.deny;
  const resolvedSkills = skills ?? loadedSkills;

  const [toolsFeedback, setToolsFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);
  const [skillsFeedback, setSkillsFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);

  const patchMutation = usePatchRuntimeSettingApiV1SettingsRuntimeKeyPatch<ApiError>(
    {
      mutation: {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: settingsKey });
        },
      },
    },
  );

  const savingTools =
    patchMutation.isPending && patchMutation.variables?.key === TOOLS_KEY;
  const savingSkills =
    patchMutation.isPending && patchMutation.variables?.key === SKILLS_KEY;

  const handleSaveTools = () => {
    setToolsFeedback(null);
    const payload = {
      profile: resolvedProfile.trim() ? resolvedProfile.trim() : null,
      allow: parseList(resolvedAllow),
      deny: parseList(resolvedDeny),
    };
    let value: string;
    try {
      value = JSON.stringify(payload);
    } catch {
      setToolsFeedback({ kind: "error", message: "Could not serialize value." });
      return;
    }
    patchMutation.mutate(
      { key: TOOLS_KEY, data: { value } },
      {
        onSuccess: () =>
          setToolsFeedback({ kind: "success", message: "Default tools saved." }),
        onError: (err) =>
          setToolsFeedback({
            kind: "error",
            message: err.message || "Save failed.",
          }),
      },
    );
  };

  const handleSaveSkills = () => {
    setSkillsFeedback(null);
    const payload = parseList(resolvedSkills);
    let value: string;
    try {
      value = JSON.stringify(payload);
    } catch {
      setSkillsFeedback({
        kind: "error",
        message: "Could not serialize value.",
      });
      return;
    }
    patchMutation.mutate(
      { key: SKILLS_KEY, data: { value } },
      {
        onSuccess: () =>
          setSkillsFeedback({
            kind: "success",
            message: "Default skills saved.",
          }),
        onError: (err) =>
          setSkillsFeedback({
            kind: "error",
            message: err.message || "Save failed.",
          }),
      },
    );
  };

  if (settingsQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-muted">
        Loading defaults…
      </div>
    );
  }

  if (settingsQuery.error) {
    return (
      <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-sm text-muted">
        {settingsQuery.error.message}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted">
        Global defaults applied to every agent unless overridden per-agent.
      </p>

      {/* Default tools */}
      <section className={SECTION_CLASS}>
        <p className={LABEL_CLASS}>Default tools</p>
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
          {toolsFeedback ? (
            <p
              className={
                toolsFeedback.kind === "success"
                  ? "text-xs text-[color:var(--success)]"
                  : "text-xs text-[color:var(--danger)]"
              }
            >
              {toolsFeedback.message}
            </p>
          ) : null}
          <Button size="sm" onClick={handleSaveTools} disabled={savingTools}>
            {savingTools ? "Saving…" : "Save default tools"}
          </Button>
        </div>
      </section>

      {/* Default skills */}
      <section className={SECTION_CLASS}>
        <p className={LABEL_CLASS}>Default skills</p>
        <div className="mt-4 space-y-4">
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
          {skillsFeedback ? (
            <p
              className={
                skillsFeedback.kind === "success"
                  ? "text-xs text-[color:var(--success)]"
                  : "text-xs text-[color:var(--danger)]"
              }
            >
              {skillsFeedback.message}
            </p>
          ) : null}
          <Button size="sm" onClick={handleSaveSkills} disabled={savingSkills}>
            {savingSkills ? "Saving…" : "Save default skills"}
          </Button>
        </div>
      </section>
    </div>
  );
}
