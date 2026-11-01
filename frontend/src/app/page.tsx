"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/auth/clerk";
import { LandingHero } from "@/components/organisms/LandingHero";
import { LandingShell } from "@/components/templates/LandingShell";

export default function Page() {
  const { isSignedIn } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isSignedIn) router.replace("/dashboard");
  }, [isSignedIn, router]);

  return (
    <LandingShell>
      <LandingHero />
    </LandingShell>
  );
}
