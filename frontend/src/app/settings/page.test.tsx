import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import SettingsPage from "./page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

vi.mock("@/auth/clerk", () => ({
  useAuth: () => ({ isSignedIn: true }),
  useUser: () => ({ user: null }),
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/components/templates/DashboardPageLayout", () => ({
  DashboardPageLayout: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
}));

vi.mock("@/api/generated/users/users", () => ({
  getGetMeApiV1UsersMeGetQueryKey: () => ["/api/v1/users/me"],
  useGetMeApiV1UsersMeGet: () => ({
    data: {
      status: 200,
      data: { name: "Local User", email: "admin@home.local", timezone: "UTC" },
    },
  }),
  useUpdateMeApiV1UsersMePatch: () => ({
    isPending: false,
    mutateAsync: vi.fn(),
  }),
  useDeleteMeApiV1UsersMeDelete: () => ({ isPending: false, mutate: vi.fn() }),
}));

describe("/settings local authentication", () => {
  const originalAuthMode = process.env.NEXT_PUBLIC_AUTH_MODE;

  beforeEach(() => {
    process.env.NEXT_PUBLIC_AUTH_MODE = "local";
  });

  afterEach(() => {
    process.env.NEXT_PUBLIC_AUTH_MODE = originalAuthMode;
  });

  it("does not offer deletion of the shared local identity", () => {
    render(<SettingsPage />);

    expect(screen.getByText("Shared local identity")).toBeInTheDocument();
    expect(
      screen.getByText(
        /shared operator identity managed by server configuration/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Organization" })).toHaveAttribute(
      "href",
      "/organization",
    );
    expect(
      screen.queryByRole("button", { name: /delete account/i }),
    ).not.toBeInTheDocument();
  });
});
