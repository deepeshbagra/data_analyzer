# Business Document Intelligence Platform

Multi-tenant SaaS that ingests business documents (purchase invoices, sales
invoices, receipts, expenses, ledgers, bank statements), extracts structured
data, reconciles records across documents, and surfaces KPIs plus risk findings.
India-first, SMB and CA-firm customers.

---

## Current phase

**Phase 1 - ingest and extraction.** Scoped to purchase invoices and bank
statements only. Phase 0 is complete but still uncommitted.

Phase 1 is far larger than the ~10-file limit below, so it lands in slices.
Each slice is a reviewable unit that leaves the suite green.

| Slice | Scope | Status |
|-------|-------|--------|
| 1a | Normalisation primitives: money, dates, identifiers | done |
| 1b | S3 adapter, upload endpoint, `source_file` dedupe, ingest task | done |
| 1c | Extraction adapter interface, text and VLM adapters | not started |
| 1d | Persist to `document`/`line_item`, arithmetic validators | not started |
| 1e | Bank statement parsing | not started |
| 1f | Review API and keyboard-first review UI | not started |

<!-- Update this section as phases land. -->

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Repo, schema, RLS, isolation tests | done |
| 1 | Ingest + extraction (purchase invoices, bank statements) | in progress |
| 2 | Matching engine | not started |
| 3 | KPIs + findings | not started |
| 4 | Hardening, tenancy, integrations | not started |
| 5 | Narration + chat | not started |

---

## Architecture principles — these are non-negotiable

1. Three separate layers, never mixed:
   - EXTRACTION: documents -> structured rows
   - MATCHING: rows -> links between rows (deterministic + fuzzy, NO LLM)
   - ANALYSIS: linked rows -> KPIs and findings (SQL and pure functions, NO LLM)
2. An LLM is used in exactly two places: (a) document classification and field
   extraction, (b) narrating already-computed findings and text-to-SQL for
   chat. An LLM must NEVER compute, sum, compare, or reconcile a number.
   Any PR that does this is wrong.
3. Every number shown to a user must be traceable to a database row, and every
   extracted field must be traceable to a source file, page, and bounding box.
4. All feature code reads from the canonical schema, never from the PDF.
5. Extraction backends are swappable behind one interface. No provider SDK
   imported outside its adapter module.

---

## Rules for every session

- Read docs/DECISIONS.md before proposing an architectural change.
- Write tests before implementation for anything involving money, dates, or
  matching logic.
- Never use float for money. numeric(18,4) in the DB, Decimal in Python.
- Never add a tenant-scoped query without a tenant_id predicate.
- Never call an LLM outside worker/extract/vlm.py, api/narration/, api/chat/.
- Never let generated SQL run with write permissions.
- No provider SDK imported outside its adapter module.
- If a task needs more than ~10 files changed, stop and ask me to split it.
- Log every non-obvious decision to docs/DECISIONS.md with the reasoning and
  the alternative rejected.
- Do not commit. Show me the diff and let me commit.

---

## Stack

- Postgres 16 (row-level security), SQLAlchemy 2.x + Alembic
- Python 3.12, FastAPI, Pydantic v2
- Celery + Redis for the pipeline (Temporal later if workflows get complex)
- S3-compatible object storage (MinIO locally)
- Next.js 15 + TypeScript + Tailwind + ECharts for the frontend
- pytest, ruff, mypy strict
- Docker Compose for local dev

Python is pinned to 3.12 inside the Docker image regardless of the host
interpreter. Everything runs in containers.

---

## Commands

`make` is not installed on this host, so `tasks.ps1` mirrors every Makefile
target. Both drive the same `docker compose` commands; if you change one,
change the other.

| Task | PowerShell | make |
|------|-----------|------|
| Start the stack | `.\tasks.ps1 up` | `make up` |
| Stop | `.\tasks.ps1 down` | `make down` |
| Apply migrations | `.\tasks.ps1 migrate` | `make migrate` |
| New migration | `.\tasks.ps1 revision -Message "..."` | `make revision m="..."` |
| Tests | `.\tasks.ps1 test` | `make test` |
| Tests, no DB | `.\tasks.ps1 test-fast` | `make test-fast` |
| Lint | `.\tasks.ps1 lint` | `make lint` |
| Format | `.\tasks.ps1 fmt` | `make fmt` |
| Types | `.\tasks.ps1 typecheck` | `make typecheck` |
| Everything | `.\tasks.ps1 check` | `make check` |
| psql | `.\tasks.ps1 psql` | `make psql` |

One manual step per clone, because git does not enable hooks automatically:
`git config core.hooksPath .githooks`. The hook refuses to commit a `.env`, a
private key, a provider token, or any value live in your own `.env`.

Local endpoints: API `:8000` (`/docs`), web `:3000`, MinIO console `:9001`,
Postgres `:5432`.

---

## Repo layout

```
api/          FastAPI app: settings, db plumbing, models, routers per phase
worker/       Celery: ingest, extract, validate, matching, findings
migrations/   Alembic; 0001 schema, 0002 RLS and roles
tests/        pytest; isolation and schema-coverage tests live here
web/          Next.js 15 App Router
docs/         DECISIONS.md, later SECURITY.md
```

---

## Tenant isolation — read this before touching data access

Isolation is enforced by Postgres, not by application `WHERE` clauses.

- The app connects as **`app_rw`**, a non-superuser with no DDL rights.
  Superusers bypass RLS entirely, so connecting as `postgres` would silently
  disable every policy in the system. `GET /ready` fails if the runtime role
  turns out to be a superuser.
- Every tenant-scoped table has RLS **enabled and forced**, with a policy
  carrying both `USING` and `WITH CHECK`. `USING` filters reads; `WITH CHECK`
  blocks writes that forge a foreign `tenant_id`.
- Tenant context is a transaction-local setting, read by `current_tenant_id()`.
  If it is unset the function returns NULL, policies evaluate to NULL, and
  queries return **zero rows**. Isolation fails closed.

**The only sanctioned way to reach tenant data:**

```python
from api.db.session import tenant_session

with tenant_session(tenant_id, user_id=actor_id) as session:
    ...
```

Never construct a `Session` against the read-write engine directly.
`admin_session()` bypasses RLS and belongs only in migrations and test setup.

Four roles, four privilege levels:

| Role | Purpose | Rights |
|------|---------|--------|
| `postgres` | migrations, test seeding | owner; superuser, bypasses RLS |
| `app_rw` | runtime | DML on business tables; no DDL; RLS applies |
| `app_ro` | Phase 5 generated SQL | SELECT only; RLS applies |
| `app_auth` | login lookup only | `BYPASSRLS`, but SELECT on 3 tables and nothing else |

---

## Schema summary

Money is `numeric(18,4)` in Postgres and `Decimal` in Python. Everywhere. A
test asserts no `real`/`double precision` column exists anywhere in the schema.

**Identity (global, no `tenant_id`)**
- `tenant` — the isolation boundary. Own GSTIN/PAN, base currency,
  `fy_start_month` (4 = April).
- `app_user` — a human. Global because a CA-firm user works across many
  tenants. RLS: visible to self or to co-tenants.
- `membership` — user × tenant × role (`owner`/`accountant`/`reviewer`/
  `read_only`), plus `is_external_advisor` for CA-firm access. Wider RLS policy
  than other tables: a user can always see their own rows, because login must
  enumerate tenants before one is selected.

**Documents**
- `party` — vendor/customer/both/employee. `aliases` and `bank_accounts` as
  jsonb; bank account numbers stored **hashed**. Trigram index on
  `display_name` so Phase 2 blocking is index-backed.
- `source_file` — sha256 (unique per tenant, so re-upload is a no-op),
  storage_key, mime, page_count, has_text_layer, uploaded_by.
- `document` — type, number, doc_date, party, currency, subtotal, tax_total,
  grand_total, source_file, `page_range` (`int4range`, so a 20-invoice batch is
  one file and twenty documents), extraction_status, `field_confidence` jsonb
  (per-field score + page + bbox), plus `extractor_adapter` /
  `extractor_model_version` / `prompt_hash` for reproducibility.
- `line_item` — description, hsn_sku, qty, rate, tax_rate, amount, line_no.

**Accounting and banking**
- `ledger_entry` — account, debit, credit, entry_date, document, narration,
  voucher, `external_id` (idempotent Tally re-sync), `recorded_at` (vs
  `entry_date`, for the backdating rule). One-sided by check constraint.
- `bank_account` — masked number + hash, ifsc, `is_own`.
- `bank_txn` — account, txn_date, **positive** amount + `direction` enum,
  narration, balance, inferred_party_id, external_id. Trigram index on
  narration for document-number search.

**Graph and output**
- `link` — polymorphic `from_type`/`from_id` → `to_type`/`to_id`, link_type,
  confidence (0–1), matched_amount, status, `evidence` jsonb (per-signal
  breakdown), `matcher_version`.
- `finding` — rule_id, severity, entity_refs, evidence, status, `dedupe_key`
  (so a re-run updates rather than duplicates), resolved_by/at.
- `audit_log` — bigint identity PK, actor, action, entity, before/after jsonb.
  **Append-only**: UPDATE/DELETE revoked *and* trigger-blocked.

```
tenant ─┬─ membership ── app_user
        ├─ party ◄──────────┬── document ──┬── line_item
        │                   │              └── ledger_entry
        ├─ bank_account ── bank_txn ─┘
        ├─ source_file ◄── document.source_file_id
        ├─ link      (polymorphic both ends)
        ├─ finding   (entity_refs jsonb)
        └─ audit_log (append-only)
```

### Two deliberate absences

- **No unique constraint on `(tenant_id, party_id, type, number)`.** The
  findings engine must *detect* duplicate and repeated invoice numbers, so the
  schema has to let them exist. There is a test asserting this stays true.
- **No foreign keys from `link`/`finding` to their endpoints.** Both are
  polymorphic. Edge integrity is the job of Phase 2's `graph.py`.

---

## Code conventions

**Python**
- `from __future__ import annotations` everywhere; `str | None`, not `Optional`.
- Declare money as `Mapped[Money]` (from `api.db.base`), never
  `Mapped[Decimal]` with an ad-hoc type. There is no float mapping, so a float
  money column fails at import.
- mypy strict, ruff with `ANN`, `S`, `DTZ`, `B`. No `# type: ignore` without a
  reason on the same line.
- Timezone-aware datetimes only (`DTZ` enforces it). Dates for business dates,
  timestamps for events.
- Config comes from `api.settings.get_settings()`; nothing reads `os.environ`
  directly.
- Comments explain *why*, not *what*. Docstrings on every rule, validator and
  metric state the exact formula or condition.

**Migrations**
- Hand-written, not autogenerated, when the ordering or a deliberate absence
  matters. Autogenerate is a starting point, always reviewed.
- Explicit constraint names, matching the `NAMING_CONVENTION` in
  `api/db/base.py`.
- Any new tenant-scoped table must be added to `TENANT_SCOPED_TABLES` in the
  RLS migration in the same PR. `tests/test_schema_rls_coverage.py` fails
  otherwise.

**Tests**
- Anything involving money, dates or matching gets its test first.
- Mark DB-dependent tests `@pytest.mark.requires_db`.
- Tests that prove a security property must be able to fail. When you touch
  RLS, break a policy locally and confirm the suite goes red.

**Frontend**
- TypeScript strict with `noUncheckedIndexedAccess`.
- Every displayed number drills through to its rows and then to the source
  document page. If it cannot be drilled into, it does not ship.
- Review screens are keyboard-first: tab between fields, enter to accept, no
  mouse required.
