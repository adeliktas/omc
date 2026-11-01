"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { BookOpen, Pencil, Save, X } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { Markdown } from "@/components/atoms/Markdown";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { apiUrl, authHeaders, fileHref, type InstalledSkill } from "@/lib/installed-skills";

export const dynamic = "force-dynamic";

interface SkillFile {
  host_path: string;
  content: string;
  sha256: string;
  editable: boolean;
}

/** YAML frontmatter ("---" block) as key/value pairs plus the Markdown body below it. */
function splitFrontmatter(text: string): [Array<[string, string]>, string] {
  const match = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!match) return [[], text];
  const pairs = match[1].split("\n").map((line) => line.match(/^([\w-]+):\s*(.*)$/))
    .filter((m): m is RegExpMatchArray => m !== null).map((m) => [m[1], m[2].replace(/^"|"$/g, "")] as [string, string]);
  return [pairs, text.slice(match[0].length)];
}

function SkillFileView() {
  const params = useSearchParams();
  const source = params.get("source") ?? "";
  const path = params.get("path") ?? "";
  const [file, setFile] = useState<SkillFile | null>(null);
  const [skill, setSkill] = useState<InstalledSkill | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const query = `source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`;
      const [fileResponse, listResponse] = await Promise.all([
        fetch(apiUrl(`/api/v1/installed-skills/file?${query}`), { headers: authHeaders() }),
        fetch(apiUrl("/api/v1/installed-skills"), { headers: authHeaders() }),
      ]);
      if (!fileResponse.ok) throw new Error(`${fileResponse.status}: ${await fileResponse.text()}`);
      setFile(await fileResponse.json());
      if (listResponse.ok) {
        const skills: InstalledSkill[] = (await listResponse.json()).skills;
        setSkill(skills.find((s) => s.source === source && path.startsWith(`${s.dir}/`)) ?? null);
      }
      setError(null);
    } catch (caught) {
      setError(String(caught));
    }
  }, [source, path]);

  useEffect(() => {
    const first = setTimeout(load, 0);
    return () => clearTimeout(first);
  }, [load]);

  const save = async () => {
    if (!file || draft === null) return;
    setSaving(true);
    try {
      const response = await fetch(apiUrl("/api/v1/installed-skills/file"), {
        method: "PUT", headers: authHeaders(),
        body: JSON.stringify({ source, path, content: draft, expected_sha256: file.sha256 }),
      });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      setDraft(null);
      await load();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setSaving(false);
    }
  };

  const [frontmatter, body] = file ? splitFrontmatter(file.content) : [[], ""];
  const button = "inline-flex items-center gap-1 rounded border px-2.5 py-1 text-xs font-medium disabled:opacity-50";
  return (
    <main className="space-y-4 p-6">
      <div className="flex flex-wrap items-center gap-3">
        <BookOpen className="h-6 w-6 text-slate-600" />
        <Link href="/skills/installed" className="text-sm text-blue-700 hover:underline">Installed skills</Link>
        <span className="text-slate-400">/</span>
        <h1 className="text-xl font-bold text-slate-900">{skill?.name ?? path.split("/").slice(-2, -1)[0]}</h1>
        <span className="font-mono text-xs text-slate-500">{file?.host_path}</span>
        <span className="ml-auto flex gap-2">
          {file?.editable && draft === null && (
            <button className={`${button} border-slate-300 bg-white hover:bg-slate-50`} onClick={() => setDraft(file.content)}>
              <Pencil className="h-3.5 w-3.5" /> Edit
            </button>
          )}
          {draft !== null && (
            <>
              <button className={`${button} border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100`} disabled={saving} onClick={save}>
                <Save className="h-3.5 w-3.5" /> {saving ? "Saving…" : "Save"}
              </button>
              <button className={`${button} border-slate-300 bg-white hover:bg-slate-50`} disabled={saving} onClick={() => setDraft(null)}>
                <X className="h-3.5 w-3.5" /> Cancel
              </button>
            </>
          )}
        </span>
      </div>
      {error && <div className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</div>}
      {draft !== null && (source === "pi-packages" || source === "claude-plugins") && (
        <div className="rounded bg-amber-50 p-2 text-xs text-amber-800">
          This file belongs to an installed package; the next package update may overwrite your edit.
        </div>
      )}
      {skill && skill.files.length > 1 && (
        <nav className="flex flex-wrap gap-1.5 text-xs">
          {skill.files.map((name) => (
            <Link key={name} href={fileHref(source, name)}
              className={`rounded border px-2 py-0.5 ${name === path ? "border-blue-300 bg-blue-50 text-blue-800" : "border-slate-200 text-slate-600 hover:bg-slate-50"}`}>
              {name.slice(skill.dir.length + 1)}
            </Link>
          ))}
        </nav>
      )}
      {draft !== null ? (
        <textarea value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false}
          className="h-[70vh] w-full rounded-xl border border-slate-300 bg-white p-3 font-mono text-xs leading-relaxed" />
      ) : file && (
        <article className="rounded-xl border border-slate-200 bg-white p-5">
          {frontmatter.length > 0 && (
            <dl className="mb-4 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 rounded bg-slate-50 p-3 text-xs">
              {frontmatter.map(([key, value]) => (
                <div key={key} className="contents"><dt className="font-medium text-slate-600">{key}</dt><dd className="text-slate-700">{value}</dd></div>
              ))}
            </dl>
          )}
          <div className="prose prose-sm max-w-none">
            <Markdown content={body} variant="basic" />
          </div>
        </article>
      )}
    </main>
  );
}

export default function SkillFilePage() {
  const { isSignedIn } = useAuth();
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
        <Suspense fallback={<main className="p-6 text-sm text-slate-500">Loading…</main>}>
          <SkillFileView />
        </Suspense>
      </SignedIn>
      <SignedOut>{null}</SignedOut>
    </DashboardShell>
  );
}
