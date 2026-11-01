"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { Search, FileText, Folder, FolderOpen, Link2, Home, Edit3, Save, X, Plus } from "lucide-react";
import { CodeBlock } from "@/components/CodeBlock";

import { SignedIn, SignedOut } from "@/auth/clerk";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { getApiBaseUrl } from "@/lib/api-base";
import { SignedOutPanel } from "@/components/auth/SignedOutPanel";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { cn } from "@/lib/utils";

type TreeNode = {
  name: string;
  path: string;
  is_dir: boolean;
  children?: TreeNode[] | null;
};

type Page = {
  path: string;
  title: string;
  content: string;
  size: number;
  mtime: number;
  links: string[];
};

type SearchHit = {
  path: string;
  title: string;
  snippet: string;
  score: number;
};

type Backlink = { path: string; title: string };

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const t = getLocalAuthToken();
    if (t) h["Authorization"] = `Bearer ${t}`;
  }
  return h;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${getApiBaseUrl()}${path}`, { ...init, headers: { ...authHeaders(), ...(init?.headers || {}) } });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json() as Promise<T>;
}

function FileTree({
  nodes, current, onSelect, depth = 0,
}: {
  nodes: TreeNode[];
  current: string | null;
  onSelect: (p: string) => void;
  depth?: number;
}) {
  const [openDirs, setOpenDirs] = useState<Set<string>>(() => new Set([""]));
  const toggle = (p: string) => {
    setOpenDirs((s) => {
      const next = new Set(s);
      if (next.has(p)) next.delete(p); else next.add(p);
      return next;
    });
  };
  return (
    <ul className={cn(depth === 0 ? "space-y-0.5" : "ml-3 space-y-0.5 border-l border-slate-100 dark:border-slate-700 pl-2")}>
      {nodes.map((n) => {
        if (n.is_dir) {
          const open = openDirs.has(n.path);
          return (
            <li key={n.path}>
              <button
                onClick={() => toggle(n.path)}
                className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-xs text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800"
              >
                {open ? <FolderOpen className="h-3 w-3 text-amber-500" /> : <Folder className="h-3 w-3 text-slate-400" />}
                <span className="font-medium">{n.name}</span>
              </button>
              {open && n.children && n.children.length > 0 && (
                <FileTree nodes={n.children} current={current} onSelect={onSelect} depth={depth + 1} />
              )}
            </li>
          );
        }
        const isCurrent = current === n.path;
        return (
          <li key={n.path}>
            <button
              onClick={() => onSelect(n.path)}
              className={cn(
                "flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-xs",
                isCurrent
                  ? "bg-blue-100 dark:bg-blue-950/50 text-blue-800 dark:text-blue-300 font-semibold"
                  : "text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800",
              )}
              title={n.path}
            >
              <FileText className="h-3 w-3 text-slate-400 dark:text-slate-500" />
              <span className="truncate">{n.name.replace(/\.md$/, "")}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function resolveWikilink(target: string, tree: TreeNode[]): string | null {
  // Prefer an exact vault-relative path before the existing alias fallback.
  const norm = target.replace(/\.md$/, "");
  let found: string | null = null;
  const visit = (nodes: TreeNode[], exact: boolean) => {
    for (const n of nodes) {
      if (found) return;
      if (n.is_dir && n.children) visit(n.children, exact);
      else if (!n.is_dir) {
        const path = n.path;
        const stem = path.replace(/\.md$/, "");
        if (stem === norm || (!exact && (stem.endsWith("/" + norm) || n.name.replace(/\.md$/, "") === norm))) {
          found = path;
        }
      }
    }
  };
  visit(tree, true);
  if (!found) visit(tree, false);
  return found;
}

export default function WikiPage() {
  return (
    <DashboardShell>
      <DashboardSidebar />
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <SignedIn>
          <WikiContent />
        </SignedIn>
        <SignedOut>
          <SignedOutPanel
            message="Sign in to access the wiki."
            forceRedirectUrl="/wiki"
          />
        </SignedOut>
      </div>
    </DashboardShell>
  );
}

function WikiContent() {
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [currentPath, setCurrentPath] = useState<string | null>(null);
  const [page, setPage] = useState<Page | null>(null);
  const [backlinks, setBacklinks] = useState<Backlink[]>([]);
  const [search, setSearch] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const refreshTree = useCallback(async () => {
    try {
      const t = await api<TreeNode[]>("/api/v1/wiki/tree");
      setTree(t);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "tree fetch failed");
    }
  }, []);

  useEffect(() => { void refreshTree(); }, [refreshTree]);

  const selectPage = useCallback(async (p: string) => {
    setLoading(true);
    setCurrentPath(p);
    setEditing(false);
    try {
      const [pg, bl] = await Promise.all([
        api<Page>(`/api/v1/wiki/page/${encodeURI(p)}`),
        api<Backlink[]>(`/api/v1/wiki/backlinks/${encodeURI(p)}`).catch(() => []),
      ]);
      setPage(pg);
      setBacklinks(bl);
      setHits(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "page fetch failed");
      setPage(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const savePage = useCallback(async () => {
    if (!currentPath) return;
    setSaving(true);
    try {
      await api<Page>(`/api/v1/wiki/page/${encodeURI(currentPath)}`, {
        method: "PUT",
        body: JSON.stringify({ content: draft }),
      });
      setEditing(false);
      await refreshTree();
      const pg = await api<Page>(`/api/v1/wiki/page/${encodeURI(currentPath)}`);
      setPage(pg);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }, [currentPath, draft, refreshTree]);

  const newPage = useCallback(async () => {
    const name = window.prompt("New page path (e.g. concepts/my-note):");
    if (!name) return;
    const cleaned = name.trim().replace(/^\/+/, "").replace(/\.md$/, "");
    if (!cleaned) return;
    setSaving(true);
    try {
      await api<Page>(`/api/v1/wiki/page/${encodeURI(cleaned + ".md")}`, {
        method: "PUT",
        body: JSON.stringify({ content: `# ${cleaned.split("/").pop()}\n\n` }),
      });
      await refreshTree();
      await selectPage(cleaned + ".md");
      setEditing(true);
      setDraft(`# ${cleaned.split("/").pop()}\n\n`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "create failed");
    } finally {
      setSaving(false);
    }
  }, [refreshTree, selectPage]);

  // search debounced
  useEffect(() => {
    if (!search.trim() || search.trim().length < 2) { setHits(null); return; }
    const id = setTimeout(async () => {
      try {
        const h = await api<SearchHit[]>(`/api/v1/wiki/search?q=${encodeURIComponent(search.trim())}`);
        setHits(h);
      } catch { /* ignore */ }
    }, 250);
    return () => clearTimeout(id);
  }, [search]);

  // pre-process content: convert [[wikilinks]] into a custom marker the markdown
  // renderer can pick up via the `a` component, since react-markdown doesn't
  // know about Obsidian syntax natively.
  const processedContent = useMemo(() => {
    if (!page) return "";
    return page.content.replace(/\[\[([^\]\|]+)(?:\|([^\]]+))?\]\]/g, (_, target, label) => {
      const display = (label || target).trim();
      const t = (target as string).trim();
      return `[${display}](wiki:${t})`;
    });
  }, [page]);

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar — T72 dark-mode aware */}
      <div className="border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Wiki</h1>
          <span className="text-xs text-slate-500 dark:text-slate-400">obsidian-wiki vault</span>
          <button
            onClick={() => void newPage()}
            className="ml-auto inline-flex items-center gap-1 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700"
          >
            <Plus className="h-3 w-3" /> New page
          </button>
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400 dark:text-slate-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search…"
              className="w-64 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 pl-7 pr-2 py-1 text-xs focus:border-blue-300 dark:focus:border-blue-500 focus:outline-none"
            />
          </div>
        </div>
        {error && <div className="mt-2 text-xs text-rose-600 dark:text-rose-400">{error}</div>}
      </div>

      {/* 3-pane: tree | content | backlinks */}
      <div className="grid flex-1 overflow-hidden grid-cols-[260px_minmax(0,1fr)_240px]">
        {/* Tree */}
        <aside className="border-r border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60 p-3 overflow-y-auto">
          <div className="mb-3 pb-2 border-b border-slate-200 dark:border-slate-700">
            <div className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 mb-1.5 font-semibold">Quick Nav</div>
            <div className="space-y-0.5">
              {[
                { label: "Architecture", path: "ARCHITECTURE.md", icon: "🏛️" },
                { label: "Plan First", path: "synthesis/PLAN-FIRST.md", icon: "📋" },
                { label: "Backend Index", path: "concepts/index-backend.md", icon: "⚙️" },
                { label: "Frontend Index", path: "concepts/index-frontend.md", icon: "🖥️" },
                { label: "Protocols Index", path: "concepts/index-protocols.md", icon: "📐" },
                { label: "Infrastructure", path: "concepts/index-infrastructure.md", icon: "🔧" },
                { label: "OmniRoute", path: "concepts/omniroute-integration.md", icon: "🚦" },
                { label: "Providers Tab", path: "concepts/tab-providers.md", icon: "🔌" },
              ].map((item) => (
                <button
                  key={item.path}
                  onClick={() => void selectPage(item.path)}
                  className={cn(
                    "w-full text-left text-[11px] px-1.5 py-0.5 rounded hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors",
                    currentPath === item.path
                      ? "bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 font-semibold"
                      : "text-slate-600 dark:text-slate-300",
                  )}
                >
                  <span className="mr-1">{item.icon}</span>
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <button
            onClick={() => { setCurrentPath(null); setPage(null); setHits(null); setSearch(""); }}
            className="mb-2 flex w-full items-center gap-1 rounded px-1 py-0.5 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <Home className="h-3 w-3" /> README
          </button>
          {tree.length === 0 ? (
            <div className="text-xs text-slate-400 dark:text-slate-500">vault empty or unreachable</div>
          ) : (
            <FileTree nodes={tree} current={currentPath} onSelect={selectPage} />
          )}
        </aside>

        {/* Content / search results */}
        <main className="overflow-y-auto bg-white dark:bg-slate-950">
          {hits ? (
            <div className="p-6">
              <h2 className="mb-3 text-sm font-semibold text-slate-700 dark:text-slate-200">
                Search results for &quot;{search}&quot; ({hits.length})
              </h2>
              <ul className="space-y-2">
                {hits.map((h) => (
                  <li
                    key={h.path}
                    onClick={() => { setSearch(""); void selectPage(h.path); }}
                    className="cursor-pointer rounded border border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-3 hover:bg-blue-50 dark:hover:bg-blue-950/30"
                  >
                    <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">{h.title}</div>
                    <div className="text-[10px] text-slate-400 dark:text-slate-500">{h.path} · score {h.score}</div>
                    <div className="mt-1 text-xs text-slate-600 dark:text-slate-300">…{h.snippet}…</div>
                  </li>
                ))}
              </ul>
            </div>
          ) : page && editing ? (
            <div className="flex h-full flex-col">
              <div className="flex items-center gap-2 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60 px-4 py-2">
                <span className="text-xs text-slate-600 dark:text-slate-300">Editing <code className="rounded bg-white dark:bg-slate-800 px-1 text-slate-700 dark:text-slate-200">{page.path}</code></span>
                <button
                  onClick={() => void savePage()}
                  disabled={saving}
                  className="ml-auto inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2 py-1 text-xs text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  <Save className="h-3 w-3" /> {saving ? "Saving…" : "Save"}
                </button>
                <button
                  onClick={() => { setEditing(false); setDraft(page.content); }}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700"
                >
                  <X className="h-3 w-3" /> Cancel
                </button>
              </div>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                className="flex-1 resize-none bg-white dark:bg-slate-950 text-slate-800 dark:text-slate-100 p-6 font-mono text-sm leading-relaxed focus:outline-none"
                spellCheck={false}
              />
            </div>
          ) : page ? (
            <article className="prose prose-slate dark:prose-invert max-w-none p-6">
              <div className="not-prose mb-3 flex items-center justify-end">
                <button
                  onClick={() => { setEditing(true); setDraft(page.content); }}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700"
                >
                  <Edit3 className="h-3 w-3" /> Edit
                </button>
              </div>
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkBreaks]}
                urlTransform={(url) => url.startsWith("wiki:") ? url : defaultUrlTransform(url)}
                components={{
                  a: ({ href, children, ...rest }) => {
                    if (href && href.startsWith("wiki:")) {
                      const target = href.slice(5);
                      const resolved = resolveWikilink(target, tree);
                      if (resolved) {
                        return (
                          <a
                            href="#"
                            onClick={(e) => { e.preventDefault(); void selectPage(resolved); }}
                            className="text-blue-600 dark:text-blue-400 underline cursor-pointer"
                          >
                            {children}
                          </a>
                        );
                      }
                      return <span className="text-rose-500 dark:text-rose-400 underline decoration-dotted" title={`unresolved: ${target}`}>{children}</span>;
                    }
                    return <a href={href} {...rest}>{children}</a>;
                  },
                  code: ({ className, children, ...rest }) => {
                    // Inline code — no language class, single-line short text
                    const isInline = !className && typeof children === "string" && !children.includes("\n");
                    if (isInline) {
                      return <CodeBlock inline>{children}</CodeBlock>;
                    }
                    // Block code — pass through to pre wrapper via className
                    return (
                      <code className={className} {...rest}>
                        {children}
                      </code>
                    );
                  },
                  pre: ({ children }) => {
                    // Extract className + raw text from the code child.
                    // react-markdown types are imprecise for component props — use any.
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    const codeEl = children as any;
                    const className = codeEl?.props?.className as string | undefined;
                    const raw = codeEl?.props?.children as string | undefined;
                    const langMatch = className?.match(/language-(\w+)/);
                    const lang = langMatch?.[1];
                    return (
                      <CodeBlock className={className} raw={raw} lang={lang}>
                        {raw}
                      </CodeBlock>
                    );
                  },
                }}
              >
                {processedContent}
              </ReactMarkdown>
              <hr className="my-6 border-slate-200 dark:border-slate-700" />
              <div className="text-[10px] text-slate-400 dark:text-slate-500">
                {page.path} · {(page.size / 1024).toFixed(1)} KB ·
                modified {new Date(page.mtime * 1000).toLocaleString("de-DE")}
              </div>
            </article>
          ) : (
            <div className="p-6">
              <h2 className="text-lg font-bold text-slate-800 dark:text-slate-100">Knowledge Vault</h2>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                Single source of truth for OpenClaw Mission Control.
                Before any code change: read <strong className="dark:text-slate-200">Plan First</strong> then <strong className="dark:text-slate-200">Architecture</strong>.
              </p>

              <div className="mt-5 grid grid-cols-2 gap-3">
                {[
                  { title: "Architecture", desc: "System overview & design", path: "ARCHITECTURE.md", icon: "🏛️", color: "border-blue-200 dark:border-blue-700 bg-blue-50 dark:bg-blue-950/40" },
                  { title: "Plan First", desc: "Mandatory pre-implementation protocol", path: "synthesis/PLAN-FIRST.md", icon: "📋", color: "border-violet-200 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/40" },
                  { title: "Backend", desc: "API routers, models, services", path: "concepts/index-backend.md", icon: "⚙️", color: "border-emerald-200 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-950/40" },
                  { title: "Frontend", desc: "Pages, components, UI", path: "concepts/index-frontend.md", icon: "🖥️", color: "border-sky-200 dark:border-sky-700 bg-sky-50 dark:bg-sky-950/40" },
                  { title: "Infrastructure", desc: "Docker, gateway, deployment", path: "concepts/index-infrastructure.md", icon: "🔧", color: "border-amber-200 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40" },
                  { title: "Protocols", desc: "Markers, gates, invariants", path: "concepts/index-protocols.md", icon: "📐", color: "border-rose-200 dark:border-rose-700 bg-rose-50 dark:bg-rose-950/40" },
                  { title: "OmniRoute", desc: "Unified AI proxy + sync (T71)", path: "concepts/omniroute-integration.md", icon: "🚦", color: "border-teal-200 dark:border-teal-700 bg-teal-50 dark:bg-teal-950/40" },
                  { title: "Providers Tab", desc: "Provider/model CRUD UI", path: "concepts/tab-providers.md", icon: "🔌", color: "border-indigo-200 dark:border-indigo-700 bg-indigo-50 dark:bg-indigo-950/40" },
                ].map((card) => (
                  <button
                    key={card.path}
                    onClick={() => void selectPage(card.path)}
                    className={`text-left p-3 rounded-lg border hover:shadow-sm hover:-translate-y-0.5 transition-all ${card.color}`}
                  >
                    <div className="text-lg mb-1">{card.icon}</div>
                    <div className="text-sm font-semibold text-slate-800 dark:text-slate-100">{card.title}</div>
                    <div className="text-[10px] text-slate-500 dark:text-slate-300 mt-0.5">{card.desc}</div>
                  </button>
                ))}
              </div>

              <hr className="my-5 border-slate-200 dark:border-slate-700" />

              <p className="text-xs text-slate-500 dark:text-slate-400">
                Vault maintained by the <code className="rounded bg-slate-100 dark:bg-slate-800 dark:text-slate-200 px-1">obsidian-wiki</code> agent skills.
                Humans can edit any <code className="rounded bg-slate-100 dark:bg-slate-800 dark:text-slate-200 px-1">.md</code> file directly.
                Pick a page from the tree, or search for a term.
              </p>
            </div>
          )}
          {loading && <div className="p-6 text-xs text-slate-400 dark:text-slate-500">loading…</div>}
        </main>

        {/* Backlinks */}
        <aside className="border-l border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60 p-3 overflow-y-auto">
          <h3 className="mb-2 text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400 flex items-center gap-1">
            <Link2 className="h-3 w-3" /> Backlinks
          </h3>
          {!page ? (
            <div className="text-xs text-slate-400 dark:text-slate-500">select a page</div>
          ) : backlinks.length === 0 ? (
            <div className="text-xs text-slate-400 dark:text-slate-500">none</div>
          ) : (
            <ul className="space-y-1">
              {backlinks.map((b) => (
                <li key={b.path}>
                  <button
                    onClick={() => void selectPage(b.path)}
                    className="text-left text-xs text-blue-700 dark:text-blue-400 hover:underline"
                    title={b.path}
                  >
                    {b.title}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {page && (
            <>
              <h3 className="mt-4 mb-2 text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">Outgoing</h3>
              {page.links.length === 0 ? (
                <div className="text-xs text-slate-400 dark:text-slate-500">none</div>
              ) : (
                <ul className="space-y-1">
                  {page.links.map((l) => {
                    const resolved = resolveWikilink(l, tree);
                    return (
                      <li key={l}>
                        {resolved ? (
                          <button onClick={() => void selectPage(resolved)} className="text-left text-xs text-blue-700 dark:text-blue-400 hover:underline">
                            {l}
                          </button>
                        ) : (
                          <span className="text-xs text-rose-500 dark:text-rose-400 line-through" title="unresolved">{l}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
