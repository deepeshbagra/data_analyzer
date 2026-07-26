# Session handoff — resume here

**Written:** 2026-07-25, end of Phase 0 (bootstrap).
**Updated:** 2026-07-25, after first execution. Everything below the next
section is kept as the historical record of what was predicted; the predictions
are annotated with what actually happened.
**Read this together with `CLAUDE.md` and `docs/DECISIONS.md`.** This file is
transient state; those two are the durable contract. Delete this file once
Phase 0 is committed.

---
---

## RESUME HERE (2026-07-26, after Phase 1b)

Phase 0 is unchanged and **still uncommitted**. Slices 1a and 1b are done.

Slice 1b added the ingest vertical:

* `worker/storage/{base,s3}.py` -- `ObjectStore` protocol plus the only module
  in the repo that imports boto3. Keys are `{tenant}/raw/{sha256}.{ext}`.
* `worker/ingest/{files,service,tasks}.py` -- sniffing, hashing, dedupe,
  the `source_file` row, and the Celery probe task.
* `api/deps.py`, `api/routers/uploads.py` -- `POST /uploads`, idempotent by
  content: same bytes twice returns 200 and the existing row, not 201.
* `pypdf` added and pinned (6.14.2). The api image needs a rebuild.
* DECISIONS #29-#33 appended.

Verified in the pinned 3.12 container: ruff, ruff format (42 files), mypy
strict (37 files), **349 tests** against live Postgres *and* live MinIO.

Three mutations were run to prove the new tests can fail, then reverted:

| Mutation | Result |
|---|---|
| storage key loses its tenant prefix | 4 red |
| dedupe lookup always returns None | 2 red |
| production guard removed from `require_tenant` | 1 red |

An end-to-end smoke run through the real stack -- uvicorn, MinIO, Redis and the
Celery worker -- was also done by hand: 201 then 200 on the same bytes, 415 on
an xlsx, one object under the tenant prefix, and the worker filling in
`page_count=3, has_text_layer=t` plus a `source_file.profiled` audit row. The
smoke tenant and its object were then deleted.

### Open items carried forward

* **A file that fails probing is only visible in `audit_log`.** `source_file`
  has no status column, so an encrypted or damaged PDF sits with NULL
  `page_count` and nothing surfaces it in the product. Slice 1c creates the
  `document` row whose `extraction_status` can carry the failure -- decide
  there whether that is enough or whether `source_file` needs its own status.
* **`POST /uploads` has no authentication.** By design, and it returns 503
  outside local/test (DECISIONS #33). It must not reach staging as-is.
* **Starlette warns that `TestClient` wants `httpx2`.** Surfaced by the first
  API test in the repo. Cosmetic today, a dependency decision later.
* 13 npm advisories (12 high), still unreviewed.
* **`docker compose build api` is not enough.** `api`, `worker` and `migrate`
  each have their own `build:` block over the same Dockerfile, so building one
  leaves the other two on the old image. After the pypdf change the worker
  crash-looped on `ModuleNotFoundError: pypdf` while every test was green,
  because the tests run in the `api` image. Use bare `docker compose build`.
  (`tasks.ps1 up` does the right thing; this only bites when building by hand.)

Next: **slice 1c** -- the extraction adapter interface, the pdfplumber text
adapter and the VLM adapter. `worker/extract/vlm.py` is one of only three
places in the system permitted to call an LLM.

Everything below this section is the earlier record and remains accurate.

---

## RESUME HERE (2026-07-26, after Phase 1a)

Phase 0 is unchanged and **still uncommitted**. Phase 1 has started, in slices,
because it is far larger than the ~10-file rule allows. Slice 1a is done:

* `worker/extract/{__init__,common,money,dates,identifiers}.py` -- normalisation
  primitives. No DB, no LLM, no I/O.
* `tests/test_extract_{money,dates,identifiers}.py` -- 212 tests, written first.
* `pyproject.toml` switched to `packages.find` (DECISIONS #24). The api image
  needs a rebuild for this; it has been rebuilt and verified.
* DECISIONS #24-#28 appended.

Verified in the pinned 3.12 container, not just on the host: `ruff check`,
`ruff format --check` (30 files), `mypy` strict (25 files), and **305 tests**
pass (93 from Phase 0, 212 new) against live Postgres.

Two mutations were run to prove the new tests can fail, then reverted:

| Mutation | Result |
|---|---|
| accept any digit grouping (strip commas blindly) | 6 red |
| resolve date ambiguity silently (`ambiguous=False`) | 2 red |

One real bug was caught by the tests during the slice: affix stripping took the
hyphen of the `/-` suffix as a minus sign, so `1,23,456/-` parsed negative. The
ordering fix carries a comment saying why the order is load-bearing.

Next actions, in order:

1. **Commit Phase 0**, then Phase 1a separately. Still yours, not Claude's.
2. **Slice 1b**: S3 adapter, upload endpoint, `source_file` sha256 dedupe,
   Celery ingest task, page-count and text-layer detection.

Everything below this section is the Phase 0 record and remains accurate.


## Where things stand — VERIFIED

Phase 0 has been executed end to end. `.\tasks.ps1 check` is green from a clean
database: ruff, ruff format, mypy strict, and 93 tests. `/ready` reports
`role: app_rw`. `npm install` resolved and Next.js 15.5.21 serves on :3000.

The isolation suite has been **proven able to fail** — see "The one test that
matters" below, which is done.

`git init` has been run. **There are still no commits.** Everything is
untracked and awaiting your review.

Four bugs were found and fixed on first execution, none of them catchable
statically:

| File | Bug | Logged |
|---|---|---|
| `tasks.ps1` | `[Parameter()]` made the helpers advanced functions; PowerShell ate `-d` (→`-Debug`) and `-v` (→`-Verbose`). `up` hung attached; `reset` preserved volumes. Silent. | DECISIONS #21 |
| `Dockerfile` | editable install ran before `api/`/`worker/` existed; the `\|\|` fallback could never have worked | DECISIONS #22 |
| `tests/test_schema_rls_coverage.py` | `:t::regclass` — SQLAlchemy never binds `:t` before a `::` cast, so the literal reached Postgres. Silent. | inline comment |
| `api/settings.py` | `type: ignore[call-arg]` now unused under mypy 2.3 | inline comment |

Dependencies were then pinned (DECISIONS #23): `constraints.txt` holds all 78
transitive packages at exact versions, the Dockerfile installs with
`pip install -c constraints.txt`, and `pyproject.toml` gained upper bounds. A
`--no-cache` rebuild plus `.\tasks.ps1 check` was green, and the installed set
diffs byte-identical against `constraints.txt`.

---

## RESUME HERE (after the 2026-07-26 reboot)

Nothing is in flight. The tree is clean in the sense that every change is
finished and verified; it is untracked in the sense that **nothing is
committed**. A reboot loses none of it.

1. **Start Docker first.** `com.docker.service` is Manual and will be stopped:
   ```powershell
   Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
   docker info --format '{{.ServerVersion}}'   # allow 30-60s
   ```
2. **Confirm nothing rotted:** `.\tasks.ps1 up` then `.\tasks.ps1 check`.
   Expect green: ruff, ruff format, mypy strict (17 files), 93 tests.
3. **Then commit Phase 0.** This is the actual next action, and it is the
   user's, not Claude's. Two files are new and easy to miss in a 16-path
   untracked tree: `constraints.txt` and `web/package-lock.json`.
4. **Then Phase 1** in a fresh session. Update the phase table in `CLAUDE.md`
   first, and delete this file once the commit lands.

Still open, deliberately untouched:

- 13 npm advisories (12 high), unreviewed. `npm audit fix --force` can move
  Next 15 to a new major — do it in its own commit, not folded into Phase 0.
- `pip -c` rather than a real resolver (`uv`, pip-tools). DECISIONS #23 argues
  for revisiting at Phase 4, not now.

---

## The blocker — RESOLVED

WSL2 is installed (default version 2, Ubuntu) and Docker server 29.6.2 works.

One recurring gotcha: `com.docker.service` is set to **Manual** and is stopped
after a reboot, so `docker info` fails until Docker Desktop is launched:

```powershell
Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
docker info --format '{{.ServerVersion}}'     # allow 30-60s before it answers
```

---

## First commands after WSL is working

```powershell
cd "E:\data analyzer 2.0"
.\tasks.ps1 up          # db + redis + minio, migrations, api + worker + web
.\tasks.ps1 test        # the isolation suite -- the point of Phase 0
.\tasks.ps1 typecheck   # mypy strict; never yet run, expect some fixes
```

`make` is not installed on this host; `tasks.ps1` mirrors every Makefile target.

Expect the first `up` to take a while: it builds the Python image and runs
`npm install` for the web container.

---

## What was verified, and what was not

Verified without a database (via a throwaway venv, SQLAlchemy's mock engine, and
`alembic upgrade head --sql`):

| Check | Result |
|---|---|
| `docker compose config` | valid |
| ORM metadata builds | 13 tables |
| Float columns anywhere in the schema | none |
| Money columns are `numeric(18,4)` | all 11 |
| `document` unique constraints | none (the deliberate absence holds) |
| Model table list vs. migration RLS list | no drift |
| `alembic upgrade head --sql` offline | 506 lines, exit 0 |
| `FORCE ROW LEVEL SECURITY` statements | 13 |
| `CREATE POLICY` statements | 13 |
| `ruff check` / `ruff format --check` | clean, 21 files |

**Was not verified — now all done, with outcomes:**

1. ~~The migrations have never been applied to a real Postgres.~~ **Applied
   clean.** All three predicted failure points — the `btree_gist` GiST index on
   `source_file_id` + `page_range`, the `citext` column, and the `DO $$ ... $$`
   role blocks — were accepted by Postgres 16 without change. Live counts match
   the offline ones exactly: 13 forced-RLS tables, 13 policies, `app_rw`/`app_ro`
   non-superuser, `app_auth` with `BYPASSRLS`.
2. ~~No test has ever run.~~ **93 pass.** The first run was 37 red, all from the
   single `:t::regclass` bind-param bug in the test code — the schema itself was
   never at fault.
3. ~~`mypy` has never run.~~ **Clean, 17 files.** `Mapped[Range[int]]` and the
   `pg_enum` helper both typed fine; the only error was one stale
   `type: ignore`.
4. ~~`npm install` has never run.~~ **Resolved**, Next.js 15.5.21 ready. It
   reports 13 npm advisories (12 high) — unreviewed, worth a look before the
   web app does anything real.

---

## The one test that matters — DONE

Both directions were exercised on `document`, then restored and re-verified
green:

| Break | Result | Mode |
|---|---|---|
| policy only (the procedure below) | 7 red, 86 pass | fails **closed** — zero rows visible |
| policy + `ENABLE` + `FORCE` | 14 red, 79 pass | fails **open** — `app_ro` read another tenant's rows, and an UPDATE modified a foreign row |

`test_scoped_session_sees_only_its_own_rows` and
`test_every_protected_table_has_a_policy` went red in both. The second variant
is the stronger evidence: it proves the suite catches a real cross-tenant leak,
not merely a deny-all.

Repeat this whenever you touch RLS. The original procedure:

1. Comment out one `CREATE POLICY` in
   `migrations/versions/0002_rls_and_roles.py`.
2. `.\tasks.ps1 reset` (destroys local volumes), then `.\tasks.ps1 test`.
3. Confirm `test_scoped_session_sees_only_its_own_rows` and
   `test_every_protected_table_has_a_policy` go **red**.
4. Restore the policy and re-run.

A security test that has never been observed failing is not evidence. Do not
skip this step, and do not commit Phase 0 before doing it.

---

## Known rough edges to expect

- `tests/conftest.py` calls `pytest.fail` instead of skipping when the `CI`
  environment variable is set. Deliberate (DECISIONS #18), but it means CI needs
  a database service or every run fails.
- ~~`Dockerfile` does `pip install -e ".[dev]"` with a `||` fallback.~~ This
  was right to be suspicious of: it was broken, and the fallback was dead code
  that could only have hidden which half failed. Now installs against stub
  `api/`/`worker/` packages with no fallback. DECISIONS #22.
- The `audit_log` append-only trigger permits deletes when the parent `tenant`
  row is already gone, so tenant erasure cascades work. The fixture teardown in
  `conftest.py` depends on this. If teardown starts failing with
  "audit_log is append-only", that trigger logic is why.
- `api/settings.py` sets `ROUND_HALF_UP` globally at import. If any future code
  depends on Python's default banker's rounding, this is where the surprise
  comes from.

---

## Then: Phase 1

Start a **fresh session** (`/clear`), and paste Prompt 1 from the prompt set.
Scope is purchase invoices and bank statements only.

Before starting it, update the `## Current phase` table in `CLAUDE.md`.

Two things in Phase 1 already have a home in the schema, so don't re-invent
them:

- Per-field confidence, page and bounding box go in `document.field_confidence`
  (jsonb). Reproducibility goes in `document.extractor_adapter`,
  `extractor_model_version` and `prompt_hash`.
- The 20-invoices-in-one-PDF splitter writes one `source_file` and N `document`
  rows with disjoint `document.page_range` (`int4range`, 1-based, half-open).
  "Which document owns page 7" is a containment query against the GiST index.

Phase 1's money-parsing tests come first: lakh/crore separators (`1,23,456.78`),
`Rs`/`INR`/`₹` prefixes, amounts in words, negatives in parentheses.

---

## Housekeeping

- A throwaway venv used for verification lives in the session scratchpad, not in
  the repo. It is disposable; `.gitignore` already covers `.venv/`.
- `.env` was created from `.env.example` and is gitignored. The dev passwords in
  it are also the fallbacks hardcoded in migration 0002 — fine locally, and
  DECISIONS #4 notes the rotation requirement for real environments.
- The Phase 0 plan is at
  `C:\Users\deepe\.claude\plans\serene-sleeping-gosling.md`.
