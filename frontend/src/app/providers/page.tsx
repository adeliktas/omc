import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { getOmnirouteDashboardUrl } from "@/lib/api-base";

// One provider/model registry and editor: OmniRoute, not a second MC catalog.
// Read at request time so each instance points at its own OmniRoute dashboard.
export const dynamic = "force-dynamic";

export default async function ProvidersPage() {
  redirect(getOmnirouteDashboardUrl((await headers()).get("host")));
}
