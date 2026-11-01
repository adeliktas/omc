"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { BookOpen, RefreshCw } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { apiUrl, authHeaders, fileHref, type InstalledSkill, type SkillRoot } from "@/lib/installed-skills";

export const dynamic = "force-dynamic";

/** Every skill installed on this host, where it lives, who loads it and what fleet runs reported (owner 2026-10-28). */

export default function InstalledSkillsPage() {
  const { isSignedIn } = useAuth();
  const [skills, setSkills] = useState<InstalledSkill[]>([]);
  const [roots, setRoots] = useState<SkillRoot[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch(apiUrl("/api/v1/installed-skills"), { headers: authHeaders() });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      const data = await response.json();
      setSkills(data.skills);
      setRoots(data.roots);
      setError(null);
    } catch (caught) {
      setError(String(caught));
    }
  }, []);

  useEffect(() => {
    if (!isSignedIn) return;
    const first = setTimeout(load, 0);
    return () => clearTimeout(first);
  }, [isSignedIn, load]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? skills.filter((s) => `${s.name} ${s.description} ${s.host_path}`.toLowerCase().includes(q)) : skills;
  }, [skills, query]);

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut><div className="flex min-h-[60vh] flex-col items-center justify-center gap-4"><BookOpen className="h-12 w-12" /><SignInButton /></div></SignedOut>
      </DashboardShell>
    );
  }

  return (
    <DashboardShell>
      <SignedIn>
        <DashboardSidebar />
        <main className="space-y-6 p-6">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <BookOpen className="h-6 w-6 text-slate-600" />
              <h1 className="text-2xl font-bold text-slate-900">Installed skills</h1>
              <span className="text-sm text-slate-500">{skills.length} skills</span>
              <button onClick={load} className="rounded p-1 hover:bg-slate-100" aria-label="refresh"><RefreshCw className="h-4 w-4" /></button>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter by name, text or path"
                className="ml-auto w-72 rounded border border-slate-300 px-2 py-1 text-sm" />
            </div>
            <p className="mt-1 max-w-4xl text-sm text-slate-600">
              Every skill an agent on this host can load. &ldquo;Fleet runs&rdquo; lists the engines whose team-runner attempts
              reported loading it (ponytail ultra on every prompt, a superpowers subset per role). Open a file to read it as
              Markdown or edit it; saves are audited admin changes.
            </p>
            {error && <div className="mt-2 rounded bg-red-50 p-2 text-sm text-red-700">{error}</div>}
          </div>

          {roots.map((root) => {
            const group = shown.filter((s) => s.source === root.source);
            return (
              <section key={root.source}>
                <h2 className="text-lg font-semibold text-slate-800">
                  {root.source} <span className="font-mono text-sm font-normal text-slate-500">{root.host_path}</span>
                  <span className="ml-2 text-sm font-normal text-slate-500">({root.present ? `${group.length} shown` : "not mounted"})</span>
                </h2>
                <p className="mb-2 text-xs text-slate-500">Loaded by: {root.loaded_by}</p>
                {group.length > 0 && (
                  <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                        <tr><th className="p-2">Skill</th><th className="p-2">Location</th><th className="p-2">Fleet runs</th><th className="p-2">Files</th></tr>
                      </thead>
                      <tbody>
                        {group.map((skill) => (
                          <tr key={skill.id} className="border-t border-slate-100 align-top">
                            <td className="max-w-md p-2">
                              <Link className="font-medium text-blue-700 hover:underline" href={fileHref(skill.source, `${skill.dir}/SKILL.md`)}>{skill.name}</Link>
                              {skill.package && <span className="ml-1 text-xs text-slate-500">{skill.package}</span>}
                              <div className="text-xs text-slate-600">{skill.description}</div>
                            </td>
                            <td className="p-2 font-mono text-xs text-slate-500">{skill.host_path}</td>
                            <td className="p-2 text-xs text-slate-600">{skill.fleet_engines.length ? skill.fleet_engines.join(", ") : <span className="text-slate-400">none reported</span>}</td>
                            <td className="p-2 text-xs">
                              {skill.files.map((file) => (
                                <div key={file}><Link className="text-blue-700 hover:underline" href={fileHref(skill.source, file)}>{file.slice(skill.dir.length + 1)}</Link></div>
                              ))}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            );
          })}
        </main>
      </SignedIn>
      <SignedOut>{null}</SignedOut>
    </DashboardShell>
  );
}
