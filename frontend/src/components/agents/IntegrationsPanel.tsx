"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/mutator";
import {
  getListIntegrationsApiV1IntegrationsGetQueryKey,
  useListIntegrationsApiV1IntegrationsGet,
  useToggleMcpServerApiV1IntegrationsMcpNamePatch,
  useToggleSkillApiV1IntegrationsSkillsSkillIdPatch,
  useUpdateToolsPolicyApiV1IntegrationsToolsPolicyPatch,
  type listIntegrationsApiV1IntegrationsGetResponse,
} from "@/api/generated/integrations/integrations";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// The generated response type is loose ({[key:string]:unknown}); describe the
// real registry shape locally and cast at the boundary.
interface McpServerEntry {
  name: string;
  enabled: boolean;
  endpoint: string;
  config: Record<string, unknown>;
}

interface SkillEntry {
  id: string;
  enabled: boolean;
}

interface ToolsRegistry {
  policy: {
    profile: string | null;
    allow: string[];
    deny: string[];
  };
  known_builtins: string[];
}

interface IntegrationsRegistry {
  mcp_servers: McpServerEntry[];
  skills: SkillEntry[];
  tools: ToolsRegistry;
  defaults: { tools: unknown; skills: unknown };
}

const SECTION_CLASS =
  "rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] p-5";
const LABEL_CLASS =
  "text-xs font-semibold uppercase tracking-[0.2em] text-quiet";
const RELOAD_NOTE_CLASS =
  "rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-xs text-muted";

const parseList = (raw: string): string[] =>
  raw
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

export function IntegrationsPanel() {
  const queryClient = useQueryClient();
  const integrationsKey = getListIntegrationsApiV1IntegrationsGetQueryKey();

  const integrationsQuery = useListIntegrationsApiV1IntegrationsGet<
    listIntegrationsApiV1IntegrationsGetResponse,
    ApiError
  >({
    query: {
      refetchOnMount: "always",
    },
  });

  const registry = useMemo<IntegrationsRegistry | null>(() => {
    if (integrationsQuery.data?.status !== 200) return null;
    return integrationsQuery.data.data as unknown as IntegrationsRegistry;
  }, [integrationsQuery.data]);

  const toggleMutation = useToggleMcpServerApiV1IntegrationsMcpNamePatch<ApiError>(
    {
      mutation: {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: integrationsKey });
        },
      },
    },
  );

  const toggleSkillMutation =
    useToggleSkillApiV1IntegrationsSkillsSkillIdPatch<ApiError>({
      mutation: {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: integrationsKey });
        },
      },
    });

  const toolsPolicyMutation =
    useUpdateToolsPolicyApiV1IntegrationsToolsPolicyPatch<ApiError>({
      mutation: {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: integrationsKey });
        },
      },
    });

  if (integrationsQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-muted">
        Loading integrations…
      </div>
    );
  }

  if (integrationsQuery.error) {
    return (
      <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-sm text-muted">
        {integrationsQuery.error.message}
      </div>
    );
  }

  if (!registry) {
    return (
      <div className="rounded-lg border border-dashed border-[color:var(--border)] bg-[color:var(--surface)] p-4 text-sm text-muted">
        No integrations registry available.
      </div>
    );
  }

  const mcpServers = registry.mcp_servers ?? [];
  const skills = registry.skills ?? [];
  const policy = registry.tools?.policy ?? {
    profile: null,
    allow: [],
    deny: [],
  };
  const knownBuiltins = registry.tools?.known_builtins ?? [];

  return (
    <div className="space-y-6">
      {toggleMutation.error ? (
        <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-xs text-muted">
          {toggleMutation.error.message}
        </div>
      ) : null}

      {/* MCP Servers */}
      <section className={SECTION_CLASS}>
        <div className="mb-4 flex items-center justify-between">
          <p className={LABEL_CLASS}>MCP Servers</p>
          <p className="text-xs text-quiet">{mcpServers.length} registered</p>
        </div>
        {mcpServers.length === 0 ? (
          <p className="rounded-lg border border-dashed border-[color:var(--border)] bg-[color:var(--surface-muted)] p-4 text-sm text-muted">
            No MCP servers configured.
          </p>
        ) : (
          <ul className="space-y-3">
            {mcpServers.map((server) => {
              const pending =
                toggleMutation.isPending &&
                toggleMutation.variables?.name === server.name;
              return (
                <li
                  key={server.name}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[color:var(--border)] bg-[color:var(--surface-muted)] px-4 py-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-strong">
                        {server.name}
                      </span>
                      <Badge
                        variant={server.enabled ? "success" : "outline"}
                      >
                        {server.enabled ? "Enabled" : "Disabled"}
                      </Badge>
                    </div>
                    <p className="mt-1 break-all font-mono text-xs text-muted">
                      {server.endpoint || "—"}
                    </p>
                  </div>
                  <Button
                    variant={server.enabled ? "outline" : "primary"}
                    size="sm"
                    disabled={pending}
                    onClick={() =>
                      toggleMutation.mutate({
                        name: server.name,
                        data: { enabled: !server.enabled },
                      })
                    }
                  >
                    {pending
                      ? "Saving…"
                      : server.enabled
                        ? "Disable"
                        : "Enable"}
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* Skills */}
      <section className={SECTION_CLASS}>
        <div className="mb-4 flex items-center justify-between">
          <p className={LABEL_CLASS}>Skills</p>
          <p className="text-xs text-quiet">{skills.length} available</p>
        </div>
        {skills.length === 0 ? (
          <p className="rounded-lg border border-dashed border-[color:var(--border)] bg-[color:var(--surface-muted)] p-4 text-sm text-muted">
            No skills registered.
          </p>
        ) : (
          <ul className="space-y-3">
            {skills.map((skill) => {
              const pending =
                toggleSkillMutation.isPending &&
                toggleSkillMutation.variables?.skillId === skill.id;
              return (
                <li
                  key={skill.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[color:var(--border)] bg-[color:var(--surface-muted)] px-4 py-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-strong">{skill.id}</span>
                    <Badge variant={skill.enabled ? "accent" : "outline"}>
                      {skill.enabled ? "Enabled" : "Disabled"}
                    </Badge>
                  </div>
                  <Button
                    variant={skill.enabled ? "outline" : "primary"}
                    size="sm"
                    disabled={pending}
                    onClick={() =>
                      toggleSkillMutation.mutate({
                        skillId: skill.id,
                        data: { enabled: !skill.enabled },
                      })
                    }
                  >
                    {pending
                      ? "Saving…"
                      : skill.enabled
                        ? "Disable"
                        : "Enable"}
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
        <p className={`mt-3 ${RELOAD_NOTE_CLASS}`}>
          Toggles are written to <code className="font-mono">openclaw.json</code> and
          applied on the gateway&apos;s next reload. A later skill sync may
          re-enable bundled skills.
        </p>
      </section>

      {/* Tools */}
      <ToolsPolicyEditor
        policy={policy}
        knownBuiltins={knownBuiltins}
        mutation={toolsPolicyMutation}
      />
    </div>
  );
}

function ToolsPolicyEditor({
  policy,
  knownBuiltins,
  mutation,
}: {
  policy: ToolsRegistry["policy"];
  knownBuiltins: string[];
  mutation: ReturnType<typeof useUpdateToolsPolicyApiV1IntegrationsToolsPolicyPatch<ApiError>>;
}) {
  // Controlled-with-fallback: state stays undefined until the user edits,
  // then renders fall back to the loaded policy.
  const [profile, setProfile] = useState<string | undefined>(undefined);
  const [allow, setAllow] = useState<string | undefined>(undefined);
  const [deny, setDeny] = useState<string | undefined>(undefined);

  const resolvedProfile = profile ?? policy.profile ?? "";
  const resolvedAllow = allow ?? (policy.allow ?? []).join(", ");
  const resolvedDeny = deny ?? (policy.deny ?? []).join(", ");

  const handleSave = () => {
    mutation.mutate({
      data: {
        profile: resolvedProfile.trim() ? resolvedProfile.trim() : null,
        allow: parseList(resolvedAllow),
        deny: parseList(resolvedDeny),
      },
    });
  };

  return (
    <section className={SECTION_CLASS}>
      <p className={LABEL_CLASS}>Tools policy</p>
      <p className="mt-1 text-xs text-muted">
        The gateway&apos;s global tool policy. Distinct from{" "}
        <code className="font-mono">AGENT_DEFAULT_TOOLS</code>, which is the
        per-agent default layered by provisioning on the Defaults tab.
      </p>

      <div className="mt-4 space-y-4">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted">Profile</label>
          <Input
            value={resolvedProfile}
            onChange={(e) => setProfile(e.target.value)}
            placeholder="full"
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

        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
            Known built-ins
          </p>
          {knownBuiltins.length ? (
            <div className="mt-2 flex flex-wrap gap-2">
              {knownBuiltins.map((t) => (
                <Badge key={t} variant="default">
                  {t}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="mt-1 text-muted">—</p>
          )}
        </div>

        {mutation.isError ? (
          <p className="text-xs text-[color:var(--danger)]">
            {mutation.error?.message ?? "Save failed."}
          </p>
        ) : null}
        {mutation.isSuccess ? (
          <p className="text-xs text-[color:var(--success)]">Saved.</p>
        ) : null}

        <Button size="sm" onClick={handleSave} disabled={mutation.isPending}>
          {mutation.isPending ? "Saving…" : "Save tools policy"}
        </Button>
      </div>

      <p className={`mt-3 ${RELOAD_NOTE_CLASS}`}>
        Applied on the gateway&apos;s next reload — not live.
      </p>
    </section>
  );
}
