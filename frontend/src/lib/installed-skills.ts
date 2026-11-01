import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { getApiBaseUrl } from "@/lib/api-base";

/** Shared by the installed-skills list and file pages (owner 2026-10-28). */
export interface InstalledSkill {
  id: string;
  source: string;
  name: string;
  description: string;
  package: string | null;
  host_path: string;
  dir: string;
  files: string[];
  fleet_engines: string[];
}

export interface SkillRoot {
  source: string;
  host_path: string;
  loaded_by: string;
  present: boolean;
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}
export const apiUrl = (path: string) => `${getApiBaseUrl()}${path}`;
export const fileHref = (source: string, path: string) =>
  `/skills/installed/view?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`;

