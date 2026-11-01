"use client";

/**
 * T88 (2026-05-16) — OpenClaw iframe tab.
 *
 * Embeds the openclaw-gateway web UI (default: this host, port 18789)
 * inside the MC shell so the owner doesn't have to open a separate tab.
 *
 * URL is configurable via `NEXT_PUBLIC_OPENCLAW_WEB_URL` env. Falls back
 * to a same-host probe (`http://<window.location.hostname>:18789`).
 */

import { useEffect, useState } from "react";
import { ExternalLink, Globe, RefreshCw } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";

export const dynamic = "force-dynamic";

const DEFAULT_PORT = 18789;

// T88b (2026-05-16): the gateway sends X-Frame-Options: DENY +
// Content-Security-Policy frame-ancestors 'none', which blocks any
// iframe embed. The MC backend now reverse-proxies the gateway UI at
// `/openclaw-ui/` and strips those headers in flight. So the iframe
// loads MC's own proxy URL, not the gateway directly.
//
// Two URLs returned:
//   - `proxyUrl`: same-host MC backend path the iframe loads
//   - `directUrl`: the real gateway address for the "open in new tab" link
function resolveOpenclawUrls(): { proxyUrl: string; directUrl: string } {
  if (typeof window === "undefined") {
    return {
      proxyUrl: "/openclaw-ui/",
      directUrl: `http://localhost:${DEFAULT_PORT}`,
    };
  }
  const env = process.env.NEXT_PUBLIC_OPENCLAW_WEB_URL;
  const directUrl = env || `http://${window.location.hostname}:${DEFAULT_PORT}`;
  // Resolve the backend API base; in development the FE runs on :3000
  // while the backend lives on :8000. `NEXT_PUBLIC_API_BASE_URL` is
  // configurable. Fall back to the current host on port 8000.
  const apiBase =
    process.env.NEXT_PUBLIC_API_BASE_URL
    || `${window.location.protocol}//${window.location.hostname}:8000`;
  return {
    proxyUrl: `${apiBase}/openclaw-ui/`,
    directUrl,
  };
}

export default function OpenClawPage() {
  const { isSignedIn } = useAuth();
  const [proxyUrl, setProxyUrl] = useState<string>("");
  const [directUrl, setDirectUrl] = useState<string>("");
  const [iframeKey, setIframeKey] = useState(0);

  useEffect(() => {
    const r = resolveOpenclawUrls();
    setProxyUrl(r.proxyUrl);
    setDirectUrl(r.directUrl);
  }, []);

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut>
          <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8">
            <Globe className="h-12 w-12 text-slate-300" />
            <p className="text-slate-500 text-lg font-medium">
              Sign in to view the openclaw web UI
            </p>
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
        <main className="h-[calc(100vh-64px)] overflow-hidden bg-slate-50 dark:bg-slate-950 flex flex-col">
          {/* Header */}
          <div className="border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-2.5 flex items-center gap-3">
            <Globe className="h-4 w-4 text-slate-500" />
            <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
              OpenClaw Gateway
            </span>
            <code
              className="text-[10px] font-mono text-slate-500 dark:text-slate-400 truncate max-w-[400px]"
              title={`Iframe loads via MC proxy: ${proxyUrl}\nDirect URL: ${directUrl}`}
            >
              {directUrl || "resolving…"}
              <span className="ml-2 px-1 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 text-[9px]">
                via /openclaw-ui proxy
              </span>
            </code>
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={() => setIframeKey((k) => k + 1)}
                title="Reload"
                className="inline-flex items-center gap-1 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700 px-2 py-1 text-[10px] text-slate-700 dark:text-slate-200"
              >
                <RefreshCw className="h-3 w-3" /> Reload
              </button>
              <a
                href={directUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700 px-2 py-1 text-[10px] text-slate-700 dark:text-slate-200"
                title="Open the gateway directly (bypasses MC proxy)"
              >
                <ExternalLink className="h-3 w-3" /> Open in new tab
              </a>
            </div>
          </div>

          {/* Iframe — loads MC's reverse-proxy URL (T88b) so the gateway's
              X-Frame-Options + CSP frame-ancestors don't block embedding. */}
          {proxyUrl ? (
            <iframe
              key={iframeKey}
              src={proxyUrl}
              title="OpenClaw Gateway"
              className="flex-1 w-full border-0 bg-white"
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"
              allow="clipboard-read; clipboard-write"
            />
          ) : (
            <div className="flex-1 flex items-center justify-center text-slate-400 italic">
              resolving openclaw URL…
            </div>
          )}
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
