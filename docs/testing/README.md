# Testing

> **Contributor test guide — baseline `006f95f8`.** Commands checked against `Makefile` and frontend package scripts, not executed in this docs-only review. See the current-state snapshot for supplied 1152/11/1 host results, exact-SHA independent Sol QA and prior frontend 133/34/build evidence. Legacy lint/TypeScript defects mean these results are not a new green `make check`.

This guide describes how to run Mission Control tests locally.

## Quick start (repo root)

```bash
make setup
make check
```

`make check` is the closest thing to “CI parity”:

- backend: lint + typecheck + unit tests (with scoped coverage gate)
- frontend: lint + typecheck + unit tests (Vitest) + production build

## Backend tests

From repo root:

```bash
make backend-test
make backend-coverage
```

Or from `backend/`:

```bash
cd backend
uv run pytest
```

Notes:

- PostgreSQL-only concurrency/lifecycle tests need a **disposable** `/mc_audit`
  database via `MC_TEST_POSTGRES_URL`; do not aim tests at production. Skips do not
  exercise row locks. Fake CLI/API boundaries prove local behavior, not live provider success.
- `make backend-coverage` enforces 100% statement/branch coverage only on
  `app.core.error_handling` and `app.services.mentions`, not the whole backend.
- Source-grep assertions, behavioral fixtures, actual provider QA and final acceptance
  are different evidence; do not equate a successful harness exit with QA PASS.

## Frontend tests

From repo root:

```bash
make frontend-test
```

Or from `frontend/`:

```bash
cd frontend
npm run test
npm run test:watch
```

## End-to-end (Cypress)

The frontend has Cypress configured in `frontend/cypress/`.

Typical flow:

1) Start the stack (or start backend + frontend separately)
2) Run Cypress

Example (two terminals):

```bash
# terminal 1
cp .env.example .env
docker compose -f compose.yml --env-file .env up -d --build
```

```bash
# terminal 2
cd frontend
npm run e2e
```

Or run interactively:

```bash
cd frontend
npm run e2e:open
```
