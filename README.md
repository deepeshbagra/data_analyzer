# Business Document Intelligence Platform

Multi-tenant SaaS that ingests business documents — purchase invoices, sales
invoices, receipts, expenses, ledgers, bank statements — extracts structured
data from them, reconciles records across documents, and surfaces KPIs and risk
findings. India-first, aimed at SMBs and CA firms.

> **Status: early.** Phase 0 (foundation) is complete. Phase 1 (ingest and
> extraction) is in progress. This is not yet a working product — see
> [Current state](#current-state).

---

## The idea

Most document-AI tools hand a PDF to a model and ask it for answers. This one
does not, because a model that is asked to *compute* a number will produce one
that looks right and cannot be checked.

Instead the work is split into three layers that never mix:

| Layer | Input → output | Uses an LLM? |
|-------|----------------|--------------|
| **Extraction** | documents → structured rows | Yes — classification and field extraction only |
| **Matching** | rows → links between rows | **Never.** Deterministic + fuzzy scoring |
| **Analysis** | linked rows → KPIs and findings | **Never.** SQL and pure functions |

An LLM is permitted in exactly two places: reading fields off a document, and
narrating findings that have *already* been computed. It never sums, compares
or reconciles a number.

Two consequences fall out of that, and they are the point of the whole design:

- **Every number shown to a user traces to a database row**, and every extracted
  field traces to a source file, page and bounding box.
- **Every reconciliation carries its evidence** — the per-signal breakdown that
  produced the score is stored with the link, so "why does the system think
  this payment settles this invoice?" always has an answer.

## Tenant isolation

Isolation is enforced by Postgres row-level security, not by application
`WHERE` clauses. The app connects as a non-superuser role; every tenant-scoped
table has RLS **enabled and forced** with both `USING` and `WITH CHECK`
policies; tenant context is a transaction-local setting.

If that setting is missing, policies evaluate to NULL and queries return **zero
rows**. Forgetting to scope a session is a visible bug, not a silent breach.

The test that motivated this design proves tenant A cannot read tenant B's rows
*even when a foreign `tenant_id` is injected directly into raw SQL* — which is
only satisfiable if the filter sits below the query, where no application code
and no injected predicate can reach it.

## Stack

Postgres 16 · SQLAlchemy 2 + Alembic · Python 3.12 · FastAPI · Pydantic v2 ·
Celery + Redis · S3-compatible object storage (MinIO locally) · Next.js 15 +
TypeScript + Tailwind · pytest, ruff, mypy strict · Docker Compose

Everything runs in containers. Python is pinned to 3.12 in the image regardless
of the host interpreter, and the full transitive dependency set is pinned in
`constraints.txt` so two builds a month apart are the same build.

## Quick start

Requires Docker. `make` is optional — `tasks.ps1` mirrors every target for
Windows hosts.

```bash
cp .env.example .env          # local dev credentials; never commit .env
make up                       # or: .\tasks.ps1 up
make test                     # or: .\tasks.ps1 test
```

| Task | make | PowerShell |
|------|------|-----------|
| Start the stack | `make up` | `.\tasks.ps1 up` |
| Apply migrations | `make migrate` | `.\tasks.ps1 migrate` |
| Tests | `make test` | `.\tasks.ps1 test` |
| Lint + types + tests | `make check` | `.\tasks.ps1 check` |

Local endpoints: API `:8000` (`/docs`), web `:3000`, MinIO console `:9001`,
Postgres `:5432`.

> **Note:** `api`, `worker` and `migrate` each build from the same Dockerfile
> but are separate images. Use bare `docker compose build`, not
> `docker compose build api`, or you will leave two of them stale.

## Current state

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Repo, schema, RLS, isolation tests | done |
| 1 | Ingest + extraction (purchase invoices, bank statements) | in progress |
| 2 | Matching engine | not started |
| 3 | KPIs + findings | not started |
| 4 | Hardening, tenancy, integrations | not started |
| 5 | Narration + chat | not started |

Phase 1 is landing in slices:

| Slice | Scope | Status |
|-------|-------|--------|
| 1a | Normalisation: money, dates, GSTIN/PAN/IFSC | done |
| 1b | Object storage, upload endpoint, `source_file` ingest | done |
| 1c | Extraction adapter interface, text and VLM adapters | not started |
| 1d | Persist to `document`/`line_item`, arithmetic validators | not started |
| 1e | Bank statement parsing | not started |
| 1f | Review API and keyboard-first review UI | not started |

**There is no authentication yet** (it is Phase 4). The upload endpoint takes
the tenant from a request header and returns `503` in any environment other
than `local` or `test`, so the stopgap cannot reach production quietly.

## Repo layout

```
api/          FastAPI app: settings, db plumbing, models, routers
worker/       Celery: storage adapters, ingest, extraction, matching, findings
migrations/   Alembic; 0001 schema, 0002 RLS and roles
tests/        pytest; isolation, schema coverage, parsing, ingest
web/          Next.js 15 App Router
docs/         DECISIONS.md — one entry per non-obvious choice
```

## A note on the code

Some conventions are load-bearing rather than stylistic:

- **Money is `numeric(18,4)` in Postgres and `Decimal` in Python, always.**
  There is no float mapping in the ORM's type map, so a float money column
  fails at import rather than in review. A test asserts no `real` or
  `double precision` column exists anywhere in the schema.
- **Parsers return an exact value or raise.** There is no best-guess path. An
  amount that reads as either `150` or `1.50` is refused and routed to human
  review, because a plausible wrong number survives every downstream check.
- **`docs/DECISIONS.md` records why**, including what was rejected. Read it
  before proposing an architectural change — several apparent omissions in the
  schema are deliberate and have tests defending them.

## Licence

Proprietary — all rights reserved. See [LICENSE](LICENSE). The source is public
for reference and transparency; no right to use it is granted.
