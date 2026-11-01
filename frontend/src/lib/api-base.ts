export function getApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (raw && raw.toLowerCase() !== "auto") {
    const normalized = raw.replace(/\/+$/, "");
    if (!normalized) {
      throw new Error("NEXT_PUBLIC_API_URL is invalid.");
    }
    return normalized;
  }

  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "https" : "http";
    const host = window.location.hostname;
    if (host) {
      const port = process.env.NEXT_PUBLIC_BACKEND_PORT?.trim() || "8000";
      return `${protocol}://${host}:${port}`;
    }
  }

  throw new Error(
    "NEXT_PUBLIC_API_URL is not set and cannot be auto-resolved outside the browser.",
  );
}

// Server-side: "localhost" would send a remote browser to itself, so default to the host it used.
export function getOmnirouteDashboardUrl(requestHost: string | null): string {
  const configured = process.env.OMNIROUTE_DASHBOARD_URL?.trim();
  if (configured) return configured;
  const host = requestHost?.replace(/:\d+$/, "") ?? "";
  const safeHost = /^([\w.-]+|\[[\da-f:.]+\])$/i.test(host) ? host : "localhost";
  const port = process.env.OMNIROUTE_PORT?.trim() || "20128";
  return `http://${safeHost}:${port}/dashboard/providers`;
}
