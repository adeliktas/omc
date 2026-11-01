import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({
  ClerkProvider: () => null,
  SignedIn: () => null,
  SignedOut: () => null,
  SignInButton: () => null,
  SignOutButton: () => null,
  useAuth: () => ({}),
  useUser: () => ({}),
}));

vi.stubEnv("NEXT_PUBLIC_AUTH_MODE", "local");

import { setLocalAuthToken, clearLocalAuthToken } from "@/auth/localAuth";
import { useAuth, useUser } from "@/auth/clerk";

describe("local-auth hooks keep a stable identity", () => {
  beforeEach(() => {
    clearLocalAuthToken();
    setLocalAuthToken("t".repeat(50));
  });

  // Regression: a fresh object per render put every page that lists getToken
  // in a dependency array into an unbounded refetch loop.
  it("useAuth returns the same object and getToken across re-renders", () => {
    const { result, rerender } = renderHook(() => useAuth());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
    expect(result.current.getToken).toBe(first.getToken);
  });

  it("useUser returns the same object across re-renders", () => {
    const { result, rerender } = renderHook(() => useUser());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
