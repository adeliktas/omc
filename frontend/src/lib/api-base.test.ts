import { afterEach, describe, expect, it, vi } from "vitest";

import { getApiBaseUrl, getOmnirouteDashboardUrl } from "./api-base";

describe("getApiBaseUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("returns normalized explicit URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com///");

    expect(getApiBaseUrl()).toBe("https://api.example.com");
  });

  it("auto-resolves from browser host when set to auto", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "auto");

    expect(getApiBaseUrl()).toBe("http://localhost:8000");
  });

  it("uses the instance backend port instead of contacting the original instance", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "auto");
    vi.stubEnv("NEXT_PUBLIC_BACKEND_PORT", "13001");

    expect(getApiBaseUrl()).toBe("http://localhost:13001");
  });

  it("auto-resolves from browser host when unset", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");

    expect(getApiBaseUrl()).toBe("http://localhost:8000");
  });
});

describe("getOmnirouteDashboardUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("prefers the configured URL", () => {
    vi.stubEnv("OMNIROUTE_DASHBOARD_URL", "https://or.example.com/dashboard/providers");

    expect(getOmnirouteDashboardUrl("192.0.2.10:13000")).toBe(
      "https://or.example.com/dashboard/providers",
    );
  });

  it("sends the browser to the host it used, on the instance's OmniRoute port", () => {
    vi.stubEnv("OMNIROUTE_DASHBOARD_URL", "");
    vi.stubEnv("OMNIROUTE_PORT", "13004");

    expect(getOmnirouteDashboardUrl("192.0.2.10:13000")).toBe(
      "http://192.0.2.10:13004/dashboard/providers",
    );
  });

  it("falls back to localhost for a missing or malformed host", () => {
    vi.stubEnv("OMNIROUTE_DASHBOARD_URL", "");

    expect(getOmnirouteDashboardUrl(null)).toBe("http://localhost:20128/dashboard/providers");
    expect(getOmnirouteDashboardUrl("evil.example/x@y")).toBe(
      "http://localhost:20128/dashboard/providers",
    );
  });
});
