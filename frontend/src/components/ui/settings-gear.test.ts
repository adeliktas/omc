import { describe, expect, it } from "vitest";

import { buildPatch, fieldsFor, matchTemplate } from "./settings-gear";

const nullable = (s: object) => ({ anyOf: [s, { type: "null" }] });
const spec = {
  paths: {
    "/api/v1/agents/{agent_id}": {
      get: {},
      patch: {
        requestBody: {
          content: { "application/json": { schema: { $ref: "#/components/schemas/AgentUpdate" } } },
        },
      },
    },
    "/api/v1/agents/summary": { get: {} },
  },
  components: {
    schemas: {
      AgentUpdate: {
        properties: {
          name: { ...nullable({ type: "string" }), title: "Name" },
          board_id: nullable({ type: "string", format: "uuid" }),
          is_gateway_main: nullable({ type: "boolean" }),
          tier: nullable({ type: "integer" }),
          heartbeat_config: nullable({ type: "object" }),
          soul_template: nullable({ type: "string" }),
          api_key: nullable({ type: "string" }),
          lane: { enum: ["agent", "harness"] },
          comment: nullable({ type: "string" }),
        },
      },
    },
  },
} as unknown as Parameters<typeof fieldsFor>[0];

describe("settings gear schema logic", () => {
  it("matches the concrete path to its template, literal paths first", () => {
    const t = Object.keys(spec.paths);
    expect(matchTemplate(t, "/api/v1/agents/abc-123")).toBe("/api/v1/agents/{agent_id}");
    expect(matchTemplate(t, "/api/v1/agents/summary")).toBe("/api/v1/agents/summary");
    expect(matchTemplate(t, "/api/v1/agents/a/b")).toBeNull();
  });

  it("derives one input kind per field and drops coordination fields", () => {
    const kinds = Object.fromEntries(
      fieldsFor(spec, "/api/v1/agents/{agent_id}").map((f) => [f.key, f.kind]),
    );
    expect(kinds).toEqual({
      name: "text",
      board_id: "ref",
      is_gateway_main: "bool",
      tier: "int",
      heartbeat_config: "json",
      soul_template: "longtext",
      api_key: "secret",
      lane: "enum",
    });
  });

  it("sends only changed fields, typed, and never a blank secret", () => {
    const fields = fieldsFor(spec, "/api/v1/agents/{agent_id}");
    const initial = { name: "Atlas", tier: 2, heartbeat_config: { every: "10m" }, is_gateway_main: false };
    const untouched = {
      name: "Atlas", board_id: "", is_gateway_main: false, tier: "2",
      heartbeat_config: JSON.stringify({ every: "10m" }, null, 2), soul_template: "", api_key: "", lane: "",
    };
    expect(buildPatch(fields, initial, untouched)).toEqual({});
    expect(
      buildPatch(fields, initial, { ...untouched, tier: "3", name: "", heartbeat_config: '{"every":"5m"}' }),
    ).toEqual({ tier: 3, name: null, heartbeat_config: { every: "5m" } });
    expect(buildPatch(fields, initial, { ...untouched, api_key: "sk-new" })).toEqual({ api_key: "sk-new" });
    expect(() => buildPatch(fields, initial, { ...untouched, heartbeat_config: "{bad" })).toThrow(/JSON/);
  });

  it("discovers nullability (anyOf, $ref, plain) and refuses to clear NOT NULL fields", () => {
    const strict = {
      paths: { "/b/{id}": { patch: { requestBody: { content: { "application/json": { schema: { $ref: "#/components/schemas/U" } } } } } } },
      components: {
        schemas: {
          U: {
            properties: {
              flag: { type: "boolean", title: "Flag" },
              cap: { type: "integer", minimum: 0 },
              mode: { $ref: "#/components/schemas/Mode" },
              maybe_mode: { anyOf: [{ $ref: "#/components/schemas/Mode" }, { type: "null" }] },
              note: { anyOf: [{ type: "string" }, { type: "null" }] },
            },
          },
          Mode: { enum: ["a", "b"], type: "string" },
        },
      },
    } as unknown as Parameters<typeof fieldsFor>[0];
    const fields = fieldsFor(strict, "/b/{id}");
    expect(Object.fromEntries(fields.map((f) => [f.key, f.nullable]))).toEqual({
      flag: false, cap: false, mode: false, maybe_mode: true, note: true,
    });
    const initial = { flag: true, cap: 1, mode: "a", maybe_mode: "a", note: "x" };
    const same = { flag: true, cap: "1", mode: "a", maybe_mode: "a", note: "x" };
    expect(buildPatch(fields, initial, same)).toEqual({});
    expect(buildPatch(fields, initial, { ...same, maybe_mode: "", note: "" })).toEqual({ maybe_mode: null, note: null });
    expect(() => buildPatch(fields, initial, { ...same, flag: null })).toThrow(/cannot be cleared/);
    expect(() => buildPatch(fields, initial, { ...same, mode: "" })).toThrow(/cannot be cleared/);
    expect(() => buildPatch(fields, initial, { ...same, cap: "" })).toThrow(/cannot be cleared/);
    expect(() => buildPatch(fields, initial, { ...same, cap: "abc" })).toThrow(/valid number/);
  });
});
