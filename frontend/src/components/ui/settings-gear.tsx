"use client";

import { useContext, useEffect, useState } from "react";
import { QueryClientContext } from "@tanstack/react-query";
import { Settings, X } from "lucide-react";

import { customFetch } from "@/api/mutator";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

/**
 * One gear for every object.
 *
 * The form is built from the backend's own OpenAPI schema for the object's
 * PATCH (or PUT) endpoint, so every field the API accepts is editable and a
 * new backend field shows up here without a frontend change. Only fields the
 * owner actually changed are sent.
 */

type Json = Record<string, unknown>;
type Schema = Json & {
  $ref?: string;
  type?: string;
  anyOf?: Schema[];
  enum?: unknown[];
  format?: string;
  title?: string;
  description?: string;
  examples?: unknown[];
  properties?: Record<string, Schema>;
};
type Spec = { paths: Record<string, Record<string, Json>>; components?: { schemas?: Record<string, Schema> } };

export type FieldKind =
  | "bool" | "enum" | "int" | "number" | "datetime" | "ref" | "secret" | "longtext" | "text" | "json";

export interface FieldDef {
  key: string;
  label: string;
  kind: FieldKind;
  hint?: string;
  /** The API accepts null (clears the value); false for NOT NULL fields. */
  nullable: boolean;
  placeholder?: string;
  options?: string[];
  refList?: string;
}

// Coordination fields that ride along on updates; they are not settings.
const DEFAULT_OMIT = ["expected_status", "expected_updated_at", "comment"];
const SECRET = /(api_key|token|secret|password)$/;
const LONG = /(description|template|objective|notes|context|content|prompt)/;
// uuid fields that point at another object: list endpoint to pick from.
const REFS: Record<string, string> = {
  board_id: "/api/v1/boards",
  gateway_id: "/api/v1/gateways",
  board_group_id: "/api/v1/board-groups",
  agent_id: "/api/v1/agents",
  assigned_agent_id: "/api/v1/agents",
};

let specPromise: Promise<Spec> | null = null;
function loadSpec(): Promise<Spec> {
  specPromise ??= customFetch<{ data: Spec }>("/openapi.json", { method: "GET" })
    .then((r) => r.data)
    .catch((e) => {
      specPromise = null; // retry on next open
      throw e;
    });
  return specPromise;
}

/** The OpenAPI path template a concrete path belongs to (fewest params wins). */
export function matchTemplate(templates: string[], path: string): string | null {
  const clean = path.split("?")[0];
  const hits = templates.filter((t) => {
    const re = t.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{[^}]+\}/g, "[^/]+");
    return new RegExp(`^${re}$`).test(clean);
  });
  hits.sort((a, b) => (a.match(/\{/g)?.length ?? 0) - (b.match(/\{/g)?.length ?? 0));
  return hits[0] ?? null;
}

function deref(spec: Spec, s: Schema | undefined): Schema {
  if (!s) return {};
  if (s.$ref) return deref(spec, spec.components?.schemas?.[s.$ref.split("/").pop()!]);
  return s;
}

/** Unwrap `anyOf: [X, null]` to X. */
function unwrap(spec: Spec, s: Schema): Schema {
  const r = deref(spec, s);
  const nonNull = r.anyOf?.map((x) => deref(spec, x)).filter((x) => x.type !== "null");
  return nonNull?.length === 1 ? { ...r, ...nonNull[0], anyOf: undefined } : r;
}

/** Does the schema (directly, via $ref, anyOf/oneOf, or a type list) accept null? */
function acceptsNull(spec: Spec, s: Schema | undefined): boolean {
  const r = deref(spec, s);
  const types = Array.isArray(r.type) ? (r.type as string[]) : [r.type];
  return (
    types.includes("null") ||
    r.nullable === true ||
    (r.enum?.includes(null) ?? false) ||
    [...(r.anyOf ?? []), ...((r.oneOf as Schema[] | undefined) ?? [])].some((x) => acceptsNull(spec, x))
  );
}

export function fieldsFor(spec: Spec, template: string, omit: string[] = []): FieldDef[] {
  const ops = spec.paths[template] ?? {};
  const op = (ops.patch ?? ops.put) as Json | undefined;
  const body = (op?.requestBody as Json | undefined)?.content as Record<string, Json> | undefined;
  const root = deref(spec, body?.["application/json"]?.schema as Schema);
  const skip = new Set([...DEFAULT_OMIT, ...omit]);
  return Object.entries(root.properties ?? {})
    .filter(([key]) => !skip.has(key))
    .map(([key, raw]) => {
      const s = unwrap(spec, raw);
      const base = {
        key,
        nullable: acceptsNull(spec, raw),
        label: s.title ?? raw.title ?? key,
        hint: s.description ?? raw.description,
        placeholder: s.examples?.[0] != null ? String(s.examples[0]) : undefined,
      };
      if (s.enum) return { ...base, kind: "enum", options: s.enum.map(String) } as FieldDef;
      if (s.type === "boolean") return { ...base, kind: "bool" } as FieldDef;
      if (s.type === "integer") return { ...base, kind: "int" } as FieldDef;
      if (s.type === "number") return { ...base, kind: "number" } as FieldDef;
      if (s.format === "date-time") return { ...base, kind: "datetime" } as FieldDef;
      if (s.format === "uuid" && REFS[key]) return { ...base, kind: "ref", refList: REFS[key] } as FieldDef;
      if (s.type === "string" && SECRET.test(key)) return { ...base, kind: "secret" } as FieldDef;
      if (s.type === "string") return { ...base, kind: LONG.test(key) ? "longtext" : "text" } as FieldDef;
      return { ...base, kind: "json" } as FieldDef;
    });
}

/** Draft value (what an input holds) for a stored value. */
function toDraft(kind: FieldKind, v: unknown): unknown {
  if (v === null || v === undefined) return kind === "bool" ? null : "";
  if (kind === "json") return JSON.stringify(v, null, 2);
  if (kind === "datetime") return String(v).slice(0, 16);
  if (kind === "secret") return ""; // never prefill secrets
  return kind === "bool" ? Boolean(v) : String(v);
}

/** Only the fields the owner changed, converted back to API types. Throws on bad JSON. */
export function buildPatch(fields: FieldDef[], initial: Json, draft: Json): Json {
  const out: Json = {};
  for (const f of fields) {
    const d = draft[f.key];
    if (d === toDraft(f.kind, initial[f.key])) continue;
    if (f.kind === "secret" && d === "") continue; // blank = keep current secret
    if (d === "" || d === null) {
      if (!f.nullable) throw new Error(`${f.label}: cannot be cleared`);
      out[f.key] = null;
    } else if (f.kind === "int" || f.kind === "number") {
      const n = f.kind === "int" ? parseInt(String(d), 10) : Number(d);
      if (!Number.isFinite(n)) throw new Error(`${f.label}: not a valid number`);
      out[f.key] = n;
    }
    else if (f.kind === "json") {
      try {
        out[f.key] = JSON.parse(String(d));
      } catch {
        throw new Error(`${f.label}: not valid JSON`);
      }
    } else out[f.key] = d;
  }
  return out;
}

function RefSelect({
  list, value, onChange, nullable,
}: { list: string; value: string; onChange: (v: string) => void; nullable: boolean }) {
  const [items, setItems] = useState<{ id: string; name: string }[]>([]);
  useEffect(() => {
    customFetch<{ data: { items?: Json[] } | Json[] }>(`${list}?limit=200`, { method: "GET" })
      .then(({ data }) => {
        const rows = Array.isArray(data) ? data : (data.items ?? []);
        setItems(rows.map((r) => ({ id: String(r.id), name: String(r.name ?? r.id) })));
      })
      .catch(() => setItems([]));
  }, [list]);
  return (
    <select className={inputCls} value={value} onChange={(e) => onChange(e.target.value)}>
      {(nullable || !value) && <option value="" disabled={!nullable}>— none —</option>}
      {value && !items.some((i) => i.id === value) && <option value={value}>{value}</option>}
      {items.map((i) => (
        <option key={i.id} value={i.id}>
          {i.name}
        </option>
      ))}
    </select>
  );
}

const inputCls = "mt-1 w-full rounded border border-slate-200 bg-white px-2 py-1 text-xs";

function FieldInput({ f, value, set }: { f: FieldDef; value: unknown; set: (v: unknown) => void }) {
  const str = (value ?? "") as string;
  switch (f.kind) {
    case "bool":
      return (
        <select className={inputCls} value={value === null ? "" : String(value)}
          onChange={(e) => set(e.target.value === "" ? null : e.target.value === "true")}>
          {(f.nullable || value === null) && <option value="" disabled={!f.nullable}>— unset —</option>}
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      );
    case "enum":
      return (
        <select className={inputCls} value={str} onChange={(e) => set(e.target.value)}>
          {(f.nullable || !str) && <option value="" disabled={!f.nullable}>— unset —</option>}
          {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    case "ref":
      return <RefSelect list={f.refList!} value={str} onChange={set} nullable={f.nullable} />;
    case "longtext":
    case "json":
      return (
        <textarea className={cn(inputCls, "min-h-20", f.kind === "json" && "font-mono")} value={str}
          placeholder={f.kind === "json" ? "{ }" : f.placeholder} onChange={(e) => set(e.target.value)} />
      );
    default: {
      const type = { int: "number", number: "number", datetime: "datetime-local", secret: "password" }[
        f.kind as string] ?? "text";
      return (
        <input className={inputCls} type={type} step={f.kind === "number" ? "any" : undefined} value={str}
          placeholder={f.kind === "secret" ? "unchanged — type to replace" : f.placeholder}
          autoComplete="off" onChange={(e) => set(e.target.value)} />
      );
    }
  }
}

export function SettingsGear({
  path,
  title,
  values,
  omit,
  onSaved,
  className,
}: {
  /** Concrete API path of the object, e.g. `/api/v1/agents/<id>`. */
  path: string;
  title: string;
  /** Current values; if omitted they are fetched with GET on `path`. */
  values?: Json;
  omit?: string[];
  onSaved?: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [fields, setFields] = useState<FieldDef[]>([]);
  const [initial, setInitial] = useState<Json>({});
  const [draft, setDraft] = useState<Json>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Optional: the gear also renders outside a QueryClientProvider (tests).
  const queryClient = useContext(QueryClientContext);

  // Loaded on open, not in an effect: parents re-render with fresh `values`
  // literals on every poll, and that must not reset a half-edited form.
  const openDialog = async () => {
    setOpen(true);
    setError(null);
    setFields([]);
    try {
      const spec = await loadSpec();
      const template = matchTemplate(Object.keys(spec.paths), path);
      if (!template) throw new Error(`No API schema for ${path}`);
      const defs = fieldsFor(spec, template, omit);
      if (!defs.length) throw new Error("This object has no editable fields.");
      const current =
        values ?? (spec.paths[template].get ? (await customFetch<{ data: Json }>(path, { method: "GET" })).data : {});
      setFields(defs);
      setInitial(current);
      setDraft(Object.fromEntries(defs.map((f) => [f.key, toDraft(f.kind, current[f.key])])));
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const patch = buildPatch(fields, initial, draft);
      if (Object.keys(patch).length) {
        const spec = await loadSpec();
        const method = spec.paths[matchTemplate(Object.keys(spec.paths), path)!].patch ? "PATCH" : "PUT";
        await customFetch(path, { method, body: JSON.stringify(patch) });
        // Refetch whatever lists show this object, wherever the gear sits.
        void queryClient?.invalidateQueries();
        onSaved?.();
      }
      setOpen(false);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const changed = (() => {
    try {
      return Object.keys(buildPatch(fields, initial, draft)).length;
    } catch {
      return 1;
    }
  })();

  // Gears sit inside clickable rows and cards, and React events bubble out of
  // the dialog's portal into them: stop clicks (would open the row) and keys
  // (cards swallow Space/Enter). Pointer events must pass — Radix listens for
  // outside clicks on the document.
  return (
    <span className="contents" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <button
        type="button"
        aria-label={`Settings: ${title}`}
        title="Settings"
        draggable={false}
        onClick={(e) => {
          e.preventDefault();
          void openDialog();
        }}
        className={cn(
          "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 transition hover:bg-slate-100 hover:text-slate-700",
          className,
        )}
      >
        <Settings className="h-4 w-4" />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="truncate">Settings — {title}</DialogTitle>
            <DialogDescription>
              Every field the API accepts for this object. Only what you change is saved.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 sm:grid-cols-2">
            {fields.map((f) => (
              <label key={f.key} className={cn("block", (f.kind === "longtext" || f.kind === "json") && "sm:col-span-2")}>
                <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{f.label}</span>
                <FieldInput f={f} value={draft[f.key]} set={(v) => setDraft((p) => ({ ...p, [f.key]: v }))} />
                {f.hint && <span className="mt-0.5 block text-[10px] text-slate-400">{f.hint}</span>}
              </label>
            ))}
            {!fields.length && !error && <p className="text-xs text-slate-500">Loading…</p>}
          </div>
          {error && (
            <p className="flex items-start gap-1 text-xs text-rose-700">
              <X className="mt-0.5 h-3 w-3 shrink-0" />
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy} className="text-xs">
              Cancel
            </Button>
            <Button onClick={() => void save()} disabled={busy || !changed} className="text-xs">
              {busy ? "Saving…" : changed ? `Save ${changed} change${changed > 1 ? "s" : ""}` : "No changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </span>
  );
}
