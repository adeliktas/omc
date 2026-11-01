"use client";

/**
 * T87 (2026-05-16) — Chat tab.
 *
 * Simple multi-turn LLM chat against any registered provider+model.
 * Independent of the agent/task pipelines — owner uses it for direct
 * one-off questions or interactive prompting without spawning tasks.
 *
 * Sidebar: chat session history (newest first), "+ New" button.
 * Header: provider + model dropdowns (filtered by online status / models
 * present in openclaw.json).
 * Body:   message bubbles (user / assistant).
 * Input:  textarea + Send. Slash commands:
 *   /new                — start a fresh session
 *   /model <provider>/<model>  — switch model on the current session
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  MessageSquare,
  Pencil,
  Plus,
  Send,
  Sparkles,
  Trash2,
  User,
} from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { SettingsGear } from "@/components/ui/settings-gear";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

interface ProviderInfo {
  provider_name: string;
  status: string;
  base_url: string | null;
  last_error?: string | null;
}

interface ModelEntry {
  id: string;
  name?: string;
  disabled?: boolean;
  iq_score?: number | null;
}

interface ChatSession {
  id: string;
  title: string;
  provider: string;
  model: string;
  backend?: string;  // T88: "direct" | "openclaw-gateway"
  created_at: string;
  updated_at: string;
  message_count: number;
}

interface ChatMessage {
  id: number | null;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  model_used?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  duration_ms?: number | null;
}

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const t = getLocalAuthToken();
    if (t) h["Authorization"] = `Bearer ${t}`;
  }
  return h;
}

const base = () => getApiBaseUrl();

function relTime(iso: string): string {
  try {
    const d = new Date(iso);
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  } catch {
    return iso;
  }
}

export default function ChatPage() {
  const { isSignedIn } = useAuth();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [active, setActive] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [pickerProvider, setPickerProvider] = useState("");
  const [pickerModel, setPickerModel] = useState("");
  // T88: chat backend. `direct` hits the provider's base_url straight.
  // `openclaw-gateway` routes through the gateway's OpenAI-compat API
  // (same path as `openclaw infer model run --gateway`).
  const [pickerBackend, setPickerBackend] = useState<"direct" | "openclaw-gateway">("direct");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // T87c: slash-command autocomplete index. -1 = nothing selected.
  const [slashIndex, setSlashIndex] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const loadSessions = useCallback(async () => {
    const r = await fetch(`${base()}/api/v1/chat/sessions`, { headers: authHeaders() });
    if (r.ok) setSessions(await r.json());
  }, []);

  const loadProviders = useCallback(async () => {
    const r = await fetch(`${base()}/api/v1/providers`, { headers: authHeaders() });
    if (r.ok) {
      const arr: ProviderInfo[] = await r.json();
      setProviders(arr.filter((p) => p.status === "online" && p.base_url));
    }
  }, []);

  const loadModels = useCallback(async (providerName: string) => {
    if (!providerName) { setModels([]); return; }
    const r = await fetch(
      `${base()}/api/v1/providers/${encodeURIComponent(providerName)}/models`,
      { headers: authHeaders() },
    );
    if (r.ok) {
      const arr: ModelEntry[] = await r.json();
      setModels(arr.filter((m) => !m.disabled));
    }
  }, []);

  const loadSession = useCallback(async (id: string) => {
    const r = await fetch(`${base()}/api/v1/chat/sessions/${id}`, { headers: authHeaders() });
    if (!r.ok) return;
    const data = await r.json();
    setActive(data.session);
    setMessages(data.messages || []);
    setPickerProvider(data.session.provider);
    setPickerModel(data.session.model);
    setPickerBackend((data.session.backend === "openclaw-gateway") ? "openclaw-gateway" : "direct");
    await loadModels(data.session.provider);
    setTimeout(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "instant" });
    }, 0);
  }, [loadModels]);

  const newSession = useCallback(async () => {
    if (!pickerProvider || !pickerModel) {
      setError("Pick a provider + model first");
      return;
    }
    const r = await fetch(`${base()}/api/v1/chat/sessions`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        provider: pickerProvider,
        model: pickerModel,
        backend: pickerBackend,
      }),
    });
    if (!r.ok) {
      setError(`${r.status}: ${await r.text()}`);
      return;
    }
    const s: ChatSession = await r.json();
    setActive(s);
    setMessages([]);
    setError(null);
    await loadSessions();
    setTimeout(() => inputRef.current?.focus(), 0);
  }, [pickerProvider, pickerModel, loadSessions]);

  const deleteSession = useCallback(async (id: string) => {
    if (!confirm("Delete this chat session?")) return;
    await fetch(`${base()}/api/v1/chat/sessions/${id}`, {
      method: "DELETE", headers: authHeaders(),
    });
    if (active?.id === id) {
      setActive(null);
      setMessages([]);
    }
    await loadSessions();
  }, [active, loadSessions]);

  const switchModel = useCallback(async (provider: string, model: string, backend?: string) => {
    if (!active) return;
    const body: Record<string, string> = { provider, model };
    if (backend) body.backend = backend;
    await fetch(`${base()}/api/v1/chat/sessions/${active.id}`, {
      method: "PATCH",
      headers: authHeaders(),
      body: JSON.stringify(body),
    });
    await loadSession(active.id);
    await loadSessions();
  }, [active, loadSession, loadSessions]);

  const send = useCallback(async () => {
    const content = draft.trim();
    if (!content) return;
    // ── slash commands ──────────────────────────────────────────────
    if (content === "/new") {
      setDraft("");
      await newSession();
      return;
    }
    if (content.startsWith("/model ")) {
      const rest = content.slice(7).trim();
      const [prov, ...mod] = rest.split("/");
      if (prov && mod.length) {
        setDraft("");
        await switchModel(prov.trim(), mod.join("/").trim());
      } else {
        setError("Usage: /model <provider>/<model>");
      }
      return;
    }
    if (!active) {
      // Auto-create a session on first send.
      if (!pickerProvider || !pickerModel) {
        setError("Pick a provider + model first");
        return;
      }
      await newSession();
      return;
    }
    setSending(true);
    setError(null);
    setDraft("");
    // Optimistic append.
    const tempUser: ChatMessage = {
      id: null, role: "user", content,
      created_at: new Date().toISOString(),
    };
    setMessages((arr) => [...arr, tempUser]);
    try {
      const r = await fetch(
        `${base()}/api/v1/chat/sessions/${active.id}/messages`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ content }),
        },
      );
      if (!r.ok) {
        const body = await r.text();
        setError(`${r.status}: ${body.slice(0, 300)}`);
        return;
      }
      const data = await r.json();
      setMessages((arr) => [
        ...arr.filter((m) => m.id !== null || m.created_at !== tempUser.created_at),
        data.user_message,
        data.assistant_message,
      ]);
      await loadSessions();
      setTimeout(() => {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
      }, 50);
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }, [draft, active, pickerProvider, pickerModel, newSession, switchModel, loadSessions]);

  useEffect(() => {
    if (!isSignedIn) return;
    void loadSessions();
    void loadProviders();
  }, [isSignedIn, loadSessions, loadProviders]);

  useEffect(() => {
    if (pickerProvider) void loadModels(pickerProvider);
  }, [pickerProvider, loadModels]);

  // T87c (2026-05-16): slash-command catalog. Each entry: token used to
  // match, full snippet to insert, description for the dropdown row.
  const SLASH_COMMANDS: { cmd: string; insert: string; description: string }[] = [
    { cmd: "/new",   insert: "/new",            description: "Start a new chat session with the picker model" },
    { cmd: "/model", insert: "/model ",          description: "Switch model: /model <provider>/<model>" },
  ];

  // Only the first whitespace-bounded token at the very start of the draft
  // is treated as a slash command. Subsequent text disables autocomplete.
  const slashSuggestions = (() => {
    if (!draft.startsWith("/")) return [];
    const firstSpace = draft.indexOf(" ");
    if (firstSpace !== -1) return [];  // already past the command token
    const q = draft.toLowerCase();
    return SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q));
  })();

  const applySlashSuggestion = (idx: number) => {
    const pick = slashSuggestions[idx];
    if (!pick) return;
    setDraft(pick.insert);
    setSlashIndex(0);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashSuggestions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, slashSuggestions.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        // Tab → complete and stay; Enter → complete + (if no-arg command) send
        e.preventDefault();
        const pick = slashSuggestions[slashIndex];
        if (!pick) return;
        if (e.key === "Enter" && pick.insert.trim() === pick.cmd) {
          // No-arg command (e.g. /new) → execute immediately.
          setDraft(pick.cmd);
          setTimeout(() => void send(), 0);
        } else {
          applySlashSuggestion(slashIndex);
        }
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setDraft("");
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  // Reset selection when the suggestion list shape changes.
  useEffect(() => {
    if (slashIndex >= slashSuggestions.length) setSlashIndex(0);
  }, [slashSuggestions.length, slashIndex]);

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut>
          <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8">
            <MessageSquare className="h-12 w-12 text-slate-300" />
            <p className="text-slate-500 text-lg font-medium">Sign in to chat</p>
            <SignInButton />
          </div>
        </SignedOut>
      </DashboardShell>
    );
  }

  return (
    <DashboardShell>
      <SignedIn>
        <DashboardSidebar />
        {/* T87b (2026-05-16): pin to viewport height so the chat input
            stays glued to the bottom of the screen and only the message
            list scrolls. DashboardShell's grid uses `min-h-[calc(100vh-64px)]`
            which would otherwise let the cell grow with content (input
            falls below the fold). */}
        <main className="h-[calc(100vh-64px)] overflow-hidden bg-slate-50 dark:bg-slate-950 flex">
          {/* Sessions sidebar */}
          <aside className="w-64 shrink-0 border-r border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 flex flex-col">
            <div className="border-b border-slate-200 dark:border-slate-700 p-3">
              <button
                onClick={() => void newSession()}
                disabled={!pickerProvider || !pickerModel}
                className="w-full inline-flex items-center justify-center gap-1.5 rounded-md bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white text-xs font-medium px-3 py-2"
                title={pickerProvider && pickerModel
                  ? `New chat with ${pickerProvider}/${pickerModel}`
                  : "Pick provider + model below first"}
              >
                <Plus className="h-3.5 w-3.5" /> New chat
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-1">
              {sessions.length === 0 && (
                <div className="text-[11px] text-slate-400 dark:text-slate-500 italic p-2">
                  No chats yet. Pick a model and start one.
                </div>
              )}
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={cn(
                    "group rounded-md px-2 py-1.5 text-xs cursor-pointer flex items-start gap-1.5",
                    active?.id === s.id
                      ? "bg-blue-50 dark:bg-blue-950/40 border-l-2 border-blue-500"
                      : "hover:bg-slate-100 dark:hover:bg-slate-800",
                  )}
                  onClick={() => void loadSession(s.id)}
                >
                  <MessageSquare className="h-3 w-3 mt-0.5 shrink-0 text-slate-400" />
                  <div className="flex-1 min-w-0">
                    <div className="truncate text-slate-800 dark:text-slate-100 font-medium">
                      {s.title}
                    </div>
                    <div className="text-[9px] text-slate-400 dark:text-slate-500 font-mono truncate">
                      {s.provider}/{s.model.replace(/^vllm\//, "")} · {s.message_count} msg · {relTime(s.updated_at)}
                    </div>
                  </div>
                  {/* Title only: provider/model go through the picker (switchModel). */}
                  <SettingsGear
                    path={`/api/v1/chat/sessions/${s.id}`}
                    title={s.title}
                    values={{ title: s.title }}
                    omit={["provider", "model", "backend"]}
                    onSaved={() => {
                      void loadSessions();
                      if (active?.id === s.id) void loadSession(s.id);
                    }}
                    className="h-5 w-5 opacity-0 group-hover:opacity-100 focus:opacity-100"
                  />
                  <button
                    onClick={(e) => { e.stopPropagation(); void deleteSession(s.id); }}
                    className="opacity-0 group-hover:opacity-100 text-slate-300 hover:text-rose-500 transition-opacity p-0.5"
                    title="Delete"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          </aside>

          {/* Main chat panel */}
          <div className="flex-1 flex flex-col overflow-hidden">
            {/* Top: provider + model picker */}
            <div className="border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-2.5 flex items-center gap-2 text-xs flex-wrap">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400" title="Coding harness subscriptions execute board tasks and are managed on Ranking">
                API endpoints
              </span>
              {/* T88: backend toggle — direct provider vs openclaw-gateway */}
              <div
                className="inline-flex rounded border border-slate-200 dark:border-slate-700 overflow-hidden text-[10px] font-medium"
                title="Direct: POST straight to provider · Gateway: route through openclaw-gateway (= `openclaw infer model run --gateway`)"
              >
                <button
                  type="button"
                  onClick={() => setPickerBackend("direct")}
                  className={cn(
                    "px-2 py-1",
                    pickerBackend === "direct"
                      ? "bg-blue-600 text-white"
                      : "bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700",
                  )}
                >
                  Direct
                </button>
                <button
                  type="button"
                  onClick={() => setPickerBackend("openclaw-gateway")}
                  className={cn(
                    "px-2 py-1 border-l border-slate-200 dark:border-slate-700",
                    pickerBackend === "openclaw-gateway"
                      ? "bg-violet-600 text-white"
                      : "bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700",
                  )}
                >
                  Gateway
                </button>
              </div>
              <Bot className="h-4 w-4 text-slate-500" />
              {pickerBackend === "openclaw-gateway" ? (
                // T88: gateway backend uses agent ids, not provider/model
                // strings. "openclaw" = default routing; "openclaw/<agentId>"
                // = pin to a specific agent. Free-text input matches the
                // CLI's `--model` flag semantics.
                <>
                  <input
                    type="text"
                    value={pickerProvider}
                    onChange={(e) => setPickerProvider(e.target.value)}
                    placeholder="(gateway — provider unused)"
                    className="rounded border border-slate-200 dark:border-slate-700 bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500 px-2 py-1 text-xs w-32"
                    title="Provider field is informational only when using the gateway backend. The model id below picks the agent."
                  />
                  <input
                    type="text"
                    value={pickerModel}
                    onChange={(e) => setPickerModel(e.target.value)}
                    placeholder="openclaw OR openclaw/<agentId>"
                    className="rounded border border-violet-300 dark:border-violet-700 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 px-2 py-1 text-xs flex-1 min-w-0"
                  />
                  {!pickerModel && (
                    <button
                      type="button"
                      onClick={() => { setPickerModel("openclaw"); setPickerProvider("openclaw"); }}
                      className="rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 px-2 py-1 text-[10px] font-medium"
                    >
                      Default
                    </button>
                  )}
                </>
              ) : (
                <>
                  <select
                    value={pickerProvider}
                    onChange={(e) => {
                      setPickerProvider(e.target.value);
                      setPickerModel("");
                    }}
                    className="rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 px-2 py-1 text-xs"
                  >
                    <option value="">Provider…</option>
                    {providers.map((p) => (
                      <option key={p.provider_name} value={p.provider_name}>
                        {p.provider_name}
                      </option>
                    ))}
                  </select>
                  <select
                    value={pickerModel}
                    onChange={(e) => setPickerModel(e.target.value)}
                    disabled={!pickerProvider}
                    className="rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 px-2 py-1 text-xs flex-1 min-w-0"
                  >
                    <option value="">Model…</option>
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}{m.iq_score != null ? ` (IQ ${m.iq_score})` : ""}
                      </option>
                    ))}
                  </select>
                </>
              )}
              {active && (
                pickerProvider !== active.provider
                || pickerModel !== active.model
                || pickerBackend !== (active.backend || "direct")
              ) && pickerProvider && pickerModel && (
                <button
                  onClick={() => void switchModel(pickerProvider, pickerModel, pickerBackend)}
                  className="rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 px-2 py-1 text-[10px] font-medium"
                  title="Apply selected backend + model to this chat"
                >
                  Switch
                </button>
              )}
              {active && (
                <span
                  className="ml-auto text-[10px] text-slate-400 dark:text-slate-500 font-mono truncate max-w-[300px]"
                  title={active.title}
                >
                  {active.title}
                </span>
              )}
            </div>

            {/* Messages */}
            <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4">
              {!active && (
                <div className="text-center text-slate-400 dark:text-slate-500 italic py-12">
                  <Sparkles className="h-8 w-8 mx-auto mb-2 opacity-50" />
                  Pick a provider + model above and start a new chat.<br />
                  <span className="text-[11px]">Slash commands: <code>/new</code>, <code>/model &lt;provider&gt;/&lt;model&gt;</code></span>
                </div>
              )}
              {active && messages.length === 0 && (
                <div className="text-center text-slate-400 dark:text-slate-500 italic py-12">
                  Type something to start.
                </div>
              )}
              {messages.map((m, i) => (
                <div key={m.id ?? `tmp-${i}`} className={cn(
                  "flex gap-3",
                  m.role === "user" ? "flex-row-reverse" : "",
                )}>
                  <div className={cn(
                    "h-7 w-7 shrink-0 rounded-full flex items-center justify-center",
                    m.role === "user"
                      ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
                      : "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300",
                  )}>
                    {m.role === "user" ? <User className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
                  </div>
                  <div className={cn(
                    "max-w-[75%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words",
                    m.role === "user"
                      ? "bg-blue-50 dark:bg-blue-950/30 text-slate-800 dark:text-slate-100"
                      : "bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100",
                  )}>
                    {m.content}
                    {m.model_used && (
                      <div className="text-[9px] text-slate-400 dark:text-slate-500 font-mono mt-1.5 pt-1.5 border-t border-slate-200 dark:border-slate-700 flex flex-wrap items-center gap-x-2">
                        <span>{m.model_used}</span>
                        {m.duration_ms != null && <span>· {m.duration_ms}ms</span>}
                        {m.completion_tokens != null && <span>· {m.completion_tokens} tokens</span>}
                        {m.completion_tokens != null && m.duration_ms != null && m.duration_ms > 0 && (
                          <span
                            className="px-1.5 py-0.5 rounded bg-emerald-50 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-700 font-semibold"
                            title={`tokens-per-second: ${m.completion_tokens} ÷ ${m.duration_ms}ms`}
                          >
                            {(m.completion_tokens / (m.duration_ms / 1000)).toFixed(1)} tps
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              ))}
              {sending && (
                <div className="flex gap-3 items-center text-xs text-slate-400 italic">
                  <Bot className="h-3.5 w-3.5 animate-pulse" />
                  thinking…
                </div>
              )}
            </div>

            {/* Input */}
            <div className="border-t border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3">
              {error && (
                <div className="text-[11px] text-rose-600 dark:text-rose-400 mb-2 break-all">
                  {error}
                </div>
              )}
              {/* T87c: slash-command autocomplete dropdown. Floats above
                  the textarea when the draft begins with `/` and no space
                  has been typed yet. Up/Down to navigate, Tab to complete,
                  Enter to complete-and-send (for no-arg cmds), Esc to dismiss. */}
              {slashSuggestions.length > 0 && (
                <div className="mb-2 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-sm overflow-hidden">
                  {slashSuggestions.map((s, i) => (
                    <button
                      key={s.cmd}
                      type="button"
                      onMouseDown={(e) => { e.preventDefault(); applySlashSuggestion(i); }}
                      onMouseEnter={() => setSlashIndex(i)}
                      className={cn(
                        "w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 border-b border-slate-100 dark:border-slate-800 last:border-b-0",
                        i === slashIndex
                          ? "bg-blue-50 dark:bg-blue-950/40"
                          : "hover:bg-slate-50 dark:hover:bg-slate-800",
                      )}
                    >
                      <code className="font-mono font-semibold text-blue-700 dark:text-blue-300">{s.cmd}</code>
                      <span className="text-slate-500 dark:text-slate-400">{s.description}</span>
                    </button>
                  ))}
                  <div className="px-3 py-1 text-[9px] text-slate-400 dark:text-slate-500 bg-slate-50 dark:bg-slate-800/50 font-mono">
                    ↑↓ navigate · Tab complete · Enter run · Esc dismiss
                  </div>
                </div>
              )}
              <div className="flex items-end gap-2">
                <textarea
                  ref={inputRef}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={active
                    ? "Type a message… (Enter to send, Shift+Enter for newline, /new for new chat)"
                    : "Pick a model and start a chat — or type /new"}
                  rows={2}
                  disabled={sending}
                  className="flex-1 rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 px-3 py-2 text-sm focus:outline-none focus:border-blue-400 disabled:opacity-60"
                />
                <button
                  onClick={() => void send()}
                  disabled={sending || !draft.trim()}
                  className="rounded-md bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 text-white px-4 py-2 text-sm font-medium inline-flex items-center gap-1.5"
                >
                  <Send className="h-3.5 w-3.5" /> Send
                </button>
              </div>
            </div>
          </div>
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
