import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import WikiPage from "./page";

vi.mock("@/auth/clerk", () => ({
  SignedIn: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SignedOut: () => null,
}));
vi.mock("@/auth/localAuth", () => ({ isLocalAuthMode: () => true, getLocalAuthToken: () => null }));
vi.mock("@/lib/api-base", () => ({ getApiBaseUrl: () => "http://unit.test" }));
vi.mock("@/components/templates/DashboardShell", () => ({
  DashboardShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/organisms/DashboardSidebar", () => ({ DashboardSidebar: () => null }));

afterEach(() => vi.unstubAllGlobals());

it("navigates aliased wiki citations without allowing unsafe external URL schemes", async () => {
  const fetch = vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/tree")) return new Response(JSON.stringify([
      { name: "_raw", path: "_raw", is_dir: true, children: [
        { name: "README.md", path: "_raw/README.md", is_dir: false },
      ] },
      { name: "entry.md", path: "entry.md", is_dir: false },
      { name: "current-state.md", path: "docs/current-state.md", is_dir: false },
      { name: "README.md", path: "README.md", is_dir: false },
    ]));
    if (url.includes("/backlinks/")) return new Response("[]");
    const path = url.split("/page/")[1];
    return new Response(JSON.stringify({ path, title: path, size: 100, mtime: 0, links: [],
      content: path === "entry.md"
        ? "[[README|Root navigation]]\n\n[[docs/current-state|Cited report]]\n\n[[missing-page|Missing citation]]\n\n[Unsafe](javascript:alert%281%29)\n\n[External](https://example.org)"
        : path === "README.md" ? "# Root navigation target"
          : path === "_raw/README.md" ? "# Wrong raw target" : "# Verified report target",
    }));
  });
  vi.stubGlobal("fetch", fetch);
  render(<WikiPage />);
  fireEvent.click(await screen.findByRole("button", { name: "entry" }));
  fireEvent.click(await screen.findByRole("link", { name: "Root navigation" }));
  await screen.findByRole("heading", { name: "Root navigation target" });
  expect(fetch.mock.calls.some(([url]) => String(url).includes("/page/_raw/README.md"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "entry" }));
  const citation = await screen.findByRole("link", { name: "Cited report" });
  expect(citation).toHaveAttribute("href", "#");
  expect(screen.getByText("Missing citation")).toHaveAttribute("title", "unresolved: missing-page");
  expect(screen.getByText("Unsafe").closest("a")?.getAttribute("href") || "").toBe("");
  expect(screen.getByRole("link", { name: "External" })).toHaveAttribute("href", "https://example.org");
  fireEvent.click(citation);
  await screen.findByRole("heading", { name: "Verified report target" });
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    "http://unit.test/api/v1/wiki/page/docs/current-state.md", expect.anything(),
  ));
  expect(fetch.mock.calls.some(([url]) => String(url).includes("missing-page"))).toBe(false);
});
