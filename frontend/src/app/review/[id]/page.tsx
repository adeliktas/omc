"use client";

export const dynamic = "force-dynamic";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { Markdown } from "@/components/atoms/Markdown";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

interface Item {
  id: string;
  title: string;
  summary: string;
  reason: string;
  proposed_action: string;
  submitted_by: string;
  submitted_at: string;
  status: string;
  reviewer_note: string | null;
  task_id: string | null;
  task_board_id?: string | null;
  task_status?: string | null;
}

/** The full report behind a /review card: the verbose handoff lives here, not on the card. */
function ReportInner() {
  const { isSignedIn } = useAuth();
  const { id } = useParams<{ id: string }>();
  const [item, setItem] = useState<Item | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isSignedIn) return;
    const headers: Record<string, string> = {};
    const token = isLocalAuthMode() ? getLocalAuthToken() : null;
    if (token) headers.Authorization = `Bearer ${token}`;
    fetch(`${getApiBaseUrl()}/api/v1/review/items?include_snoozed=true`, { headers, cache: "no-store" })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((items: Item[]) => {
        const found = items.find((i) => i.id === id);
        if (found) setItem(found); else setError("Review item not found.");
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [id, isSignedIn]);

  return (
    <main className="flex-1 overflow-y-auto bg-slate-50">
      <div className="p-4 md:p-6 max-w-4xl mx-auto flex flex-col gap-3 text-sm text-slate-700">
        <Link className="text-xs text-blue-700 hover:underline" href="/review">← Back to decisions</Link>
        {error && <p className="text-red-600">{error}</p>}
        {item && (
          <>
            <h1 className="text-lg font-semibold text-slate-800">{item.title}</h1>
            <p className="text-xs text-slate-500">
              {item.status} · by {item.submitted_by} · {new Date(item.submitted_at).toLocaleString()}
              {item.task_status && <> · task {item.task_status}</>}
              {item.task_board_id && (
                <> · <Link className="text-blue-700 hover:underline" href={`/boards/${item.task_board_id}?taskId=${item.task_id}`}>open task</Link></>
              )}
            </p>
            <p><span className="font-medium">Reason: </span>{item.reason}</p>
            <p><span className="font-medium">Proposed action: </span>{item.proposed_action}</p>
            {item.reviewer_note && <p><span className="font-medium">Outcome: </span>{item.reviewer_note}</p>}
            <section className="rounded-xl border bg-white p-4 overflow-x-auto">
              <Markdown content={item.summary} variant="description" />
            </section>
          </>
        )}
      </div>
    </main>
  );
}

export default function ReviewReportPage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 p-10 text-center">
          <SignInButton mode="modal" forceRedirectUrl="/review" signUpForceRedirectUrl="/review">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <ReportInner />
      </SignedIn>
    </DashboardShell>
  );
}
