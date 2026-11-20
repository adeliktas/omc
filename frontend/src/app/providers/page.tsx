import { redirect } from "next/navigation";

// One provider/model registry and editor: OmniRoute, not a second MC catalog.
// Read at request time so each instance points at its own OmniRoute dashboard.
export const dynamic = "force-dynamic";

export default function ProvidersPage() {
  redirect(
    process.env.OMNIROUTE_DASHBOARD_URL ??
      "http://localhost:20128/dashboard/providers",
  );
}
