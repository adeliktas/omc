export interface TestOutcomeEntry {
  status: string;
  category: string;
  last_run: { detail: string | null } | null;
}

export type TestOutcomeFilter = "" | "not-verified" | string;

/** A vacuous pass means its precondition was absent, not that it was verified. */
export function isNotVerified(test: TestOutcomeEntry): boolean {
  return (
    test.status === "pass" && /\bvacuous\b/i.test(test.last_run?.detail ?? "")
  );
}

export function displayTestStatus(test: TestOutcomeEntry): string {
  return isNotVerified(test) ? "not-verified" : test.status;
}

export function filterTests<T extends TestOutcomeEntry>(
  tests: T[],
  filter: TestOutcomeFilter,
): T[] {
  if (!filter) return tests;
  return tests.filter((test) =>
    filter === "not-verified"
      ? isNotVerified(test)
      : test.status === filter || test.category === filter,
  );
}
