# Architecture decisions

One entry per non-obvious choice: what we decided, why, and what we rejected.
Read this before proposing an architectural change. Append; do not rewrite
history. If a decision is reversed, add a new entry that supersedes the old one
and mark the old one.

Status values: **accepted**, **superseded by #n**, **revisited in Phase n**.

---

## 1. Tenant isolation via session variable + FORCE RLS

**Status:** accepted (Phase 0)

Isolation lives in Postgres. Every tenant-scoped table has RLS enabled *and
forced*, with policies comparing `tenant_id` to `current_tenant_id()`, which
reads the transaction-local `app.tenant_id` setting. The application connects as
`app_rw`, a non-superuser.

**Why:** the bootstrap requirement is a test proving tenant A cannot see tenant
B's rows *even with a raw SQL injection of a different tenant_id*. That is only
satisfiable if the filter is below the query, where no application code and no
injected predicate can reach it. Application-level `WHERE tenant_id = ?` fails
this test by construction: injection replaces the predicate.

Two properties fall out and both matter:

- `SET LOCAL` semantics mean the setting dies with the transaction, so a pooled
  connection cannot leak tenant context between requests.
- An unset variable yields NULL, so policy comparisons are NULL, so queries
  return zero rows. Forgetting to scope a session is a visible bug rather than a
  silent breach. It fails closed.

**Rejected — per-tenant Postgres role:** stronger blast-radius isolation, but
role sprawl, connection-pool fragmentation (a pool per tenant), and migrations
that scale with customer count.

**Rejected — schema-per-tenant:** simple mental model, but cross-tenant work
becomes painful exactly where the product needs it — CA-firm multi-client views
— and every migration multiplies by customer count.

**Rejected — application-level filtering:** cannot satisfy the injection
requirement. Also relies on every future developer remembering, forever.

---

## 2. `WITH CHECK` on every policy, not just `USING`

**Status:** accepted (Phase 0)

**Why:** `USING` governs which rows are visible to read, update and delete.
`WITH CHECK` governs which rows may be *written*. A policy with only `USING`
lets a session scoped to tenant A insert a row stamped `tenant_id = B`. The row
is invisible to A (correct) and silently pollutes B's data (catastrophic) —
B's invoice totals shift with no trace of where the row came from.

Both halves are asserted per table by `tests/test_schema_rls_coverage.py`, not
just tested behaviourally, so a future policy that omits `WITH CHECK` fails
immediately.

---

## 3. `FORCE ROW LEVEL SECURITY` in addition to `ENABLE`

**Status:** accepted (Phase 0)

**Why:** plain `ENABLE` exempts the table owner, and the owner is the role
migrations run as. Without `FORCE`, a policy looks present in the catalog and
does nothing for anyone connecting as the owner.

Superusers bypass RLS regardless, which is why the app connects as `app_rw` and
why `GET /ready` returns 503 if the runtime role turns out to be a superuser — a
misconfigured `DATABASE_URL` pointing at `postgres` would disable every policy
in the system while every test still passed.

---

## 4. Four database roles

**Status:** accepted (Phase 0)

| Role | Used by | Rights |
|------|---------|--------|
| `postgres` | migrations, test seeding | owner, superuser |
| `app_rw` | runtime | DML on business tables, no DDL, RLS applies |
| `app_ro` | Phase 5 generated SQL | SELECT only, RLS applies |
| `app_auth` | login credential lookup | `BYPASSRLS`, SELECT on 3 tables only |

**Why `app_ro` exists now** rather than in Phase 5: the requirement is that
model-generated SQL runs with SELECT-only grants. Creating the role at
bootstrap means the guarantee is testable before the feature exists, and the
grant is a second barrier independent of the SQL-validating parser. A parser
bypass still cannot write.

**Why `app_auth` has `BYPASSRLS`:** login must find a user by email *before* any
tenant is known. The alternatives were worse. Widening the `app_user` policy to
admit unscoped sessions would create a hole usable by any code path that forgets
to scope, defeating decision #1's fail-closed property. Instead there is one
narrow role with `BYPASSRLS` and `SELECT` on exactly `app_user`, `membership`
and `tenant` — no write grants anywhere, so the worst case is disclosure of
email addresses and hashes to code that already handles them.

`membership` gets a deliberately wider policy (`tenant_id = current_tenant_id()
OR user_id = current_user_id()`) so an authenticated user can enumerate their
own tenants for the CA-firm switcher without any bypass at all.

---

## 5. Self-hosted JWT auth, `app_user` global with a `membership` join

**Status:** accepted (Phase 0), expanded in Phase 4

`app_user` carries no `tenant_id`. Access is expressed as `membership` rows
(user × tenant × role), with `is_external_advisor` distinguishing CA-firm access
from a client's own staff.

**Why:** the CA-firm requirement is one user reaching multiple client tenants
with an explicit switcher and separate audit trails. If identity were
tenant-scoped, that user would need N accounts and the switcher would be a
second login system. As a join table it is one query.

**Why self-hosted over Clerk/Auth0:** India-first product handling financial
documents; adding an identity vendor to that data path is a procurement and
compliance conversation with every CA-firm customer. Also keeps local dev and
tests free of network calls. The cost is real — password reset, MFA and session
management are Phase 4 work we would otherwise get free — and this is the entry
to revisit if enterprise SSO becomes a sales blocker.

---

## 6. Synchronous SQLAlchemy, not async

**Status:** accepted (Phase 0)

**Why:** RLS requires the tenant setting and the query to share one
transaction-pinned connection. That is expressible in either model, but Celery —
which runs extraction, matching and findings, i.e. most of the actual work — is
synchronous. Going async in the API would mean two implementations of every
data-access path, or an async-to-sync bridge at the boundary. FastAPI runs sync
endpoints in a threadpool, which is entirely adequate for a workload dominated
by document processing rather than connection concurrency.

**Rejected:** async SQLAlchemy + `asyncpg`. Revisit only if API connection
concurrency becomes the bottleneck, which for this workload it will not be
before other things break first.

---

## 7. Money is `numeric(18,4)` and `Decimal`, enforced structurally

**Status:** accepted (Phase 0)

`api/db/base.py` defines `Money = Annotated[Decimal, mapped_column(Numeric(18,
4))]`, and the declarative `type_annotation_map` has **no entry for `float`**.
A float-typed money column therefore fails at import, not in review.

**Why 4 decimal places** rather than 2: Indian tax computation produces
intermediate values below the paisa — per-unit rates, cess, TDS at 0.1%,
proportional apportionment across line items. Rounding to 2 during intermediate
arithmetic then summing produces a total that disagrees with the vendor's own
invoice. Store 4, round for display.

**Why `ROUND_HALF_UP`** (set in `api/settings.py`) rather than Python's default
`ROUND_HALF_EVEN`: banker's rounding disagrees with how Indian tax computation is
conventionally done, so on `x.xx5` cases our total would differ from the
customer's by a paisa. Being arithmetically defensible and different from the
customer's invoice is still wrong.

`tests/test_schema_rls_coverage.py` asserts no `real` or `double precision`
column exists anywhere, so this holds for tables added in later phases too.

---

## 8. Native Postgres enum types

**Status:** accepted (Phase 0)

**Why:** rows arrive from places that are not application code — migrations,
`psql`, the Tally sync agent, the Account Aggregator feed. A native enum rejects
a bad value at the boundary; a Python-side enum only validates the paths that
go through the ORM.

**Rejected — text + CHECK constraint:** easier to evolve, but the whole benefit
is being strict, and Postgres 16 permits `ALTER TYPE ... ADD VALUE` inside a
transaction, which was the historical objection. Removing a value still requires
a type rewrite; that is rare enough to accept.

Note: `values_callable` is set on every mapping so the *values*
(`purchase_invoice`) are persisted rather than the member *names*
(`PURCHASE_INVOICE`). What is in the database is what appears in JSON and in
hand-written SQL.

---

## 9. `document.page_range` as `int4range`

**Status:** accepted (Phase 0)

**Why:** Phase 1 must split a scanned batch of 20 invoices in one PDF into 20
documents. Both the splitter and the review UI need "which document owns page 7
of this file", which as a range is a containment query (`page_range @> 7`) backed
by a GiST index, and as two integer columns is a `BETWEEN` with more room to get
the boundary wrong. `NOT isempty(page_range)` and `lower(page_range) >= 1` are
enforced by check constraints.

**Cost:** `Range[int]` values in Python are slightly less obvious than two ints,
and the GiST index needs the `btree_gist` extension to combine a UUID with the
range in one index. Both acceptable.

**Deviation note:** Prompt 0 specified `page_range` without a representation.
This is the representation chosen, not a change of scope.

---

## 10. No unique constraint on `(tenant_id, party_id, type, number)`

**Status:** accepted (Phase 0) — **load-bearing absence**

**Why:** Phase 3 requires rules for *duplicate invoice* (same party, amount,
near date) and *repeated invoice number*. Both detect a condition the database
would have to permit in order for it to be detectable. A unique constraint here
would reject the fraudulent row at insert, the rule would never fire, and the
fraud would become invisible — the ingest would just report a failed upload.

There is an index for lookup performance and an explicit test
(`test_document_number_is_not_unique_per_party`) that fails if someone
"tightens" this later.

---

## 11. `bank_txn.amount` is always positive; direction is an enum

**Status:** accepted (Phase 0)

**Why:** statement formats disagree about sign conventions — some use negative
for debits, some use separate debit/credit columns, some use a `Dr`/`Cr` suffix.
Normalising to a signed amount pushes that ambiguity into every downstream
comparison, and a sign error in matching produces a confident wrong answer.
Magnitude plus an explicit direction makes the convention impossible to get
wrong silently, at the cost of one extra column in every amount comparison.

---

## 12. `link` and `finding` are polymorphic with no foreign keys

**Status:** accepted (Phase 0)

`link` has `from_type`/`from_id` and `to_type`/`to_id`; `finding.entity_refs` is
JSONB.

**Why:** an edge can connect a `bank_txn` to a `document`, a `ledger_entry` to a
`document`, or a document to a document. Expressing that with real foreign keys
needs either one nullable FK column per entity type (wide, and a check
constraint enforcing exactly-one-set) or a table per pair (a combinatorial
explosion that makes graph traversal a union of a dozen queries).

**Cost, stated plainly:** the database cannot prevent an edge pointing at a
deleted row. That is the job of Phase 2's `graph.py` and a periodic consistency
check. Accepted because the traversal code has to exist regardless.

Constraints that *are* enforced: `confidence` in [0,1], no self-links, and a
unique constraint on the full endpoint tuple so re-running the matcher updates
rather than duplicates.

---

## 13. `line_item.tenant_id` is denormalised

**Status:** accepted (Phase 0)

**Why:** `tenant_id` is derivable from `document_id`, but an RLS policy that has
to join to find the tenant runs that join on every row of every query. A local
column keeps the policy predicate index-backed. Same reasoning applies to every
child table.

**Cost:** the write path must set it consistently. Enforced by the FK to `tenant`
plus the `WITH CHECK` policy, which rejects any row whose `tenant_id` is not the
session's.

---

## 14. `audit_log` is append-only, enforced twice

**Status:** accepted (Phase 0), completed in Phase 4

UPDATE and DELETE are revoked from `app_rw`, *and* a `BEFORE UPDATE OR DELETE`
trigger raises.

**Why both:** the grant is the real control, but grants are easy to widen by
accident in a later migration or by a DBA. The trigger survives that.

**The one permitted deletion** is erasure of an entire tenant (a DPDP/GDPR
deletion request), which arrives as an `ON DELETE CASCADE` from `tenant`. The
trigger detects this by checking whether the parent tenant row still exists:
during a cascade Postgres has already removed it in the same transaction, so its
absence distinguishes full erasure from someone editing history.

**Also:** `bigint` identity PK rather than UUID, because this will be the largest
table and monotonic ordering gives cheap keyset pagination. And `actor_email` is
denormalised alongside `actor` so the trail survives user deletion.

---

## 15. Party bank account numbers are stored hashed

**Status:** accepted (Phase 0)

`party.bank_accounts` and `bank_account.account_number_hash` store a hash;
`account_number_masked` holds a display-safe form.

**Why:** the only rule needing account numbers is "vendor bank account matches
an employee account", which needs *equality*, not the value. Storing the
plaintext would add a high-value target to the database for no functional gain.

---

## 16. Migrations hand-written, not autogenerated

**Status:** accepted (Phase 0)

**Why:** autogenerate cannot express the things that matter here — statement
ordering around extensions and enum types, the RLS layer (invisible to
SQLAlchemy metadata), and deliberate *absences* like #10, which autogenerate
would happily "fix". Migration 0001 is written so a reviewer can read the schema
as a document.

`env.py` sets `include_object` so future autogenerate runs do not try to drop
the policies, functions, roles and triggers created in 0002.

---

## 17. RLS table list duplicated, and cross-checked by a test

**Status:** accepted (Phase 0)

Migration 0002 hardcodes `TENANT_SCOPED_TABLES`; `api/models/__init__.py`
derives the same list from the mapper registry. A test asserts they match.

**Why:** a migration must be frozen — deriving its table list from live model
code means re-running an old migration against a newer codebase does something
different than it did originally, which makes migrations unreproducible. So the
list is duplicated on purpose, and the drift is caught by a test rather than by
a reviewer noticing.

`test_no_table_is_left_unprotected` is the backstop: it reads the catalog and
fails on *any* public table without RLS that is not in an explicit allowlist, so
a table added in a later phase cannot ship unprotected even if nobody updates
either list.

---

## 18. DB tests skip locally, fail in CI

**Status:** accepted (Phase 0)

If the database is unreachable, `tests/conftest.py` skips with an actionable
message locally, and calls `pytest.fail` when `CI` is set.

**Why:** a security test that silently skips is worse than no test — it produces
a green run that proves nothing, which is exactly the failure mode you would
want to avoid on the isolation suite. Skipping locally is a developer
convenience; skipping in CI would be a lie.

**Corollary for reviewers:** when you change anything about RLS, break one
policy locally and confirm the suite goes red. A security test that cannot fail
is not evidence.

---

## 19. `tasks.ps1` mirrors the Makefile

**Status:** accepted (Phase 0)

**Why:** GNU make is not installed on the primary dev host, but the Makefile is
the right canonical entrypoint for CI and containers. The shim dispatches to
identical `docker compose` commands rather than reimplementing anything, so
drift between the two is a bug, not a variation.

**Rejected — `just`:** cross-platform and nicer syntax, but a third tool to
install for a wrapper that runs six commands.

---

## 20. Python pinned to 3.12 in the image

**Status:** accepted (Phase 0)

The host has 3.14; `pyproject.toml` declares `>=3.12,<3.13` and the Dockerfile
uses `python:3.12-slim`.

**Why:** the stack specifies 3.12, and the extraction dependencies arriving in
Phase 1 (pdfplumber, image handling, provider SDKs) have the least friction
there. Everything runs in containers, so the host interpreter is irrelevant to
the build.

---

## 21. `tasks.ps1` helpers take `$args`, with no `param()` block

**Status:** accepted (Phase 0, found on first execution)

`Invoke-Compose` and `Invoke-Tool` are plain functions that read the automatic
`$args`. They deliberately do **not** declare
`param([Parameter(ValueFromRemainingArguments)][string[]]$ComposeArgs)`.

**Why:** a `[Parameter()]` attribute makes a function *advanced*, which silently
adds PowerShell's common parameters. The binder then consumes any docker flag
that prefix-matches one, and passes the rest through with no error:

| Written | Actually ran | Consequence |
|---|---|---|
| `up -d db redis minio` | `up db redis minio` | `-d` → `-Debug`; `up` ran attached and hung tailing db logs, so migrations and `api`/`worker`/`web` never started |
| `down -v` | `down` | `-v` → `-Verbose`; `reset` preserved the volumes it exists to destroy |

`--rm`, `--no-deps`, `-f` and `-m` were unaffected — they match no common
parameter — which is what made this hard to see: most targets worked. The two
that broke were the two that matter, and both failed silently rather than
erroring.

This is the class of bug DECISIONS #19 predicts: the Makefile was correct, so
the shim had drifted from it. The fix restores parity; the Makefile is unchanged.

**Rejected — quoting the flags (`Invoke-Compose up '-d' ...`):** works, because
a quoted token is parsed as an argument rather than a parameter name, but it
leaves the trap armed for the next flag anyone adds. Removing `param()` disarms
it once.

**Rejected — `--%` (stop-parsing token):** disables variable expansion for the
rest of the line, which several targets rely on.

---

## 22. The Docker dependency layer builds against stub packages, with no fallback

**Status:** accepted (Phase 0, found on first execution)

`Dockerfile` creates empty `api/` and `worker/` packages before
`pip install -e ".[dev]"`, and the previous
`|| pip install --no-cache-dir ".[dev]"` fallback is gone.

**Why the stubs:** the dependency install is a separate layer from the source so
it is only rebuilt when `pyproject.toml` changes. But `pyproject.toml` declares
`packages = ["api", "worker"]` explicitly, so setuptools fails metadata
generation with `package directory 'api' does not exist` when only
`pyproject.toml` has been copied. The editable install records a path pointer to
`/app`, so the real files — `COPY`'d immediately after, bind-mounted in dev —
are what actually get imported.

**Why no fallback:** it could never have worked. A non-editable build needs the
same package directories the editable one was missing, so both halves of the
`||` failed for one reason, and the `||` only obscured which. Worse, had the
stubs existed without the editable flag, it would have *succeeded* — installing
two empty packages into site-packages to shadow the real source, turning a build
failure into an import mystery at runtime. Same reasoning as #18: a fallback
that can only convert a loud failure into a quiet wrong answer is not a
safety net.

**Note for the Phase 4 production image:** the dev bind mount is what makes an
editable install the natural choice. A production build should install
non-editable from the copied source, which is a different Dockerfile stage, not
a fallback on this line.

---

## 23. Version ranges in `pyproject.toml`, exact versions in `constraints.txt`

**Status:** accepted (Phase 0, after first execution)

Two files with two different jobs. `pyproject.toml` declares which packages are
needed and what range is *compatible*. `constraints.txt` pins the exact version
of all 78 transitive packages, and the Dockerfile installs with
`pip install -c constraints.txt -e ".[dev]"`.

**Why:** until first execution every range was open-ended, and the resolver had
quietly crossed four major versions — redis 5→6, pytest 8→9, mypy 1→2,
pytest-cov 6→7 — against code written for the earlier ones. It surfaced as a
`type: ignore` that a newer mypy called unused (#22's sibling), which is the
benign version of this problem. The malign version is a matching or money
behaviour that changes under a rebuild months from now, on a foundation whose
whole premise is that every number is reproducible and traceable.

An unbounded range means the build is a function of *when* you run it. That is
not a property this project can afford.

**Why both, rather than one:**

- Ranges alone still let a rebuild drift within the ceiling, and say nothing
  about transitive packages, which is where most surprise lives.
- A pin file alone loses the information about what the code actually requires,
  so a future upgrade has nothing to check the new version against.

Ceilings sit one major above what is running, not one above what was originally
written — writing `redis<6` today would be a downgrade of a version already
proven green. The ceiling records "we are on 6, and 7 needs a human".

**Rejected — uv / pip-tools / Poetry:** a proper resolver with a real lockfile
is better than a frozen `pip list`, and is the right move when this becomes a
team. Today it adds a tool to the toolchain to solve a problem that `pip -c`
already solves, and the project deliberately keeps its dependency surface small.
Revisit at Phase 4 hardening.

**Note:** `web/package-lock.json` covers the frontend and must be committed.
`package.json` already caps majors with `^`, so the npm side never had this
problem — only the Python side did.

---

## 24. `setuptools` finds packages instead of listing them

**Status:** accepted (Phase 1a)

`[tool.setuptools.packages.find] include = ["api*", "worker*"]` replaces
`packages = ["api", "worker"]`.

**Why:** the explicit list named only the two top-level packages. Phase 1a adds
`worker.extract`, and a subpackage that is not listed is simply absent from a
non-editable build. Dev would never have noticed -- DECISIONS #22 installs
editable, so imports resolve through a path pointer to `/app` and every
subpackage works. The Phase 4 production image, which #22 explicitly says
should install non-editable from copied source, would have shipped without
`worker.extract` and failed at the first `import`.

Same family as #22 and #23: a build that behaves differently from the one that
was tested. Fixed at the point where the first subpackage appears rather than
at the point where it would have hurt.

---

## 25. Normalisers return an exact value or raise -- never a best guess

**Status:** accepted (Phase 1a)

Every function in `worker/extract/` that turns a string into a typed value
either returns an exact, canonical result or raises a `NormalizationError`
carrying the raw input. There is no third path: no "confidence 0.4, here is our
best reading", no partial parse.

**Why:** a wrong number that reaches the database is indistinguishable from a
right one. It survives the arithmetic validators (it is internally consistent),
it survives matching (it matches something, just the wrong thing), and it
surfaces as a KPI a customer acts on. An exception, by contrast, is a routed
review task -- the system's designed response to uncertainty.

The concrete cases where this bites, all deliberate:

- **`1,50`** parses as neither Indian nor Western grouping. It is 150 under one
  reading and 1.50 under another, a hundredfold difference. Rejected.
- **`1.234.567`** (dot-grouped, no decimal) could be 1234567 or 1.234567.
  Rejected.
- **Digit grouping matching no convention at all** (`1,2345`) is the signature
  of OCR damage, so it is rejected rather than de-comma'd. Stripping the commas
  and moving on is what most parsers do, and it is exactly how a damaged figure
  becomes a confident wrong number.
- **GSTIN OCR confusions** (`O` for `0`) are reported, not repaired. Repairing
  would leave the check digit validating our own correction instead of the
  document.

**Rejected -- return `None` on failure:** loses why, and `None` at a call site
is easy to treat as "field absent" when it means "field present and
unreadable". Those route to different queues.

**Cost:** more fields land in review early on, before the extractor is tuned.
That is the correct direction for the error to point.

---

## 26. Day-first dates, with ambiguity reported rather than resolved silently

**Status:** accepted (Phase 1a)

`parse_date` returns `ParsedDate(value, ambiguous)`. `ambiguous` is True only
when day-first and month-first both yield valid *and different* dates, so
`15/04/2026` is unambiguous (15 is not a month), `04/04/2026` is unambiguous
(same answer either way), and `03/04/2026` is flagged.

**Why not just default to day-first and move on:** India writes day-first and so
does most Indian invoicing software, so the default is right the large majority
of the time. But the failure is invisible when it is wrong: the document lands
in the wrong month, and ageing buckets, GST period totals and the late-payment
rules all shift by a plausible-looking amount with nothing to indicate why. The
flag lets `document.field_confidence` mark exactly those fields for a human,
which is a bounded amount of review work rather than all dates or none.

`dayfirst=False` exists as a per-template setting for a known US-authored
vendor template. It is never a per-document guess.

**Rejected -- `dateutil`:** it will parse nearly anything, prefers returning a
value, and gives no signal that it made a choice. Its permissiveness is the
opposite of what #25 requires. The formats that actually occur fit in three
regexes.

**Two-digit year pivot is the constant 68/69, not a window around today.** A
relative pivot would make the same document parse to a different year on
re-processing, which contradicts the reproducibility that
`document.extractor_model_version` and `prompt_hash` exist to provide.

---

## 27. Amount-in-words is parsed, not skipped

**Status:** accepted (Phase 1a)

`parse_amount_in_words` handles the Indian scale words (lakh/lac, crore) and
both orderings of the paise clause.

**Why bother, when the figure is right there:** it is a second, independent
encoding of the same total, printed on essentially every Indian tax invoice.
Comparing the two catches the error OCR is most likely to make and least likely
to make visible -- a dropped or duplicated digit, which leaves a perfectly
well-formed number that is wrong by a factor of ten. No amount of arithmetic
validation on the line items catches that, because the line items were read by
the same pass that misread the total.

It also enforces that scale words strictly decrease, so `One Thousand Two Lakh`
is a transcription error rather than 201000.

---

## 28. `Dr`/`Cr` markers are reported, never folded into the sign

**Status:** accepted (Phase 1a), follows #11

`parse_amount` returns a `marker` field alongside a magnitude-and-sign amount.
It never applies the marker.

**Why:** #11 already establishes that `bank_txn.amount` is positive with an
explicit `direction`, because statement formats disagree about sign
conventions. Resolving `Cr` to a sign inside the parser would put one bank's
convention into the number at the earliest possible point, where it is least
recoverable. The statement parser knows which column the value came from and
what that bank means by it; the string parser does not.


---

## 29. Storage keys are content-addressed and tenant-prefixed

**Status:** accepted (Phase 1b)

`storage_key()` returns `{tenant_id}/raw/{sha256}{ext}`. The original filename
appears nowhere in it.

**Why tenant-prefixed:** isolation in this system is enforced by Postgres
(#1), and object storage is not Postgres. The prefix is what makes a per-tenant
bucket policy or a per-tenant KMS key expressible in Phase 4, and it is what
stops two tenants who upload byte-identical files from sharing one object.
Sharing would be tempting — same bytes, same hash, one copy — and it would mean
a DPDP erasure request from tenant A destroying tenant B's evidence, while B's
rows still claimed the file was there. `tests/test_ingest_service.py` asserts
two tenants uploading identical content get separate objects.

**Why content-addressed:** the key is a function of the bytes, so writing the
same file twice writes the same object with the same content. Uploads are then
idempotent at the storage layer as well as at the row layer, and a retry after
a half-completed ingest cannot leave two objects that differ.

**Why the filename is excluded:** it is attacker-controlled text —
`../../etc/passwd`, an embedded newline, four kilobytes of Unicode — and in a
key it would be a path. It is stored on the row, where it is data and not a
location. There is a test with a traversal filename asserting the key is
unaffected.

---

## 30. Object first, row second

**Status:** accepted (Phase 1b)

`ingest_file` validates, then writes the object, then inserts the
`source_file` row and its audit entry in one transaction.

**Why:** a write spanning an object store and a database has two possible
half-states, and they are not equally bad.

- *Object stored, row missing* — an orphan blob. Invisible to the product,
  reclaimable by a sweep, and healed by the next upload of the same file
  because the key is content-addressed (#29).
- *Row stored, object missing* — a `source_file` the pipeline will try to
  extract and cannot. It fails far from its cause, and until then the row
  asserts that a file exists which does not.

The ordering makes the window leak storage rather than integrity. Validation
happens before either write, so a rejected upload leaves neither.

**Rejected — two-phase commit / an outbox:** correct, and disproportionate. The
failure this avoids is already benign, and an outbox table would add a
tenant-scoped table plus a sweeper to protect against a leaked object.

---

## 31. A file's type comes from its bytes, never from the client

**Status:** accepted (Phase 1b)

`identify()` sniffs magic bytes. It accepts no filename and no declared
`Content-Type` — not as a policy, but structurally: there is no parameter
through which a caller could supply one.

**Why:** the mime decides which extractor runs. If the client could set it, the
client could choose which of our code paths processes their bytes — feeding a
PNG to a PDF parser, or a PDF to the VLM adapter, is a decision an uploader
should not get to make. It also removes a whole class of confusion where a file
renamed `.pdf` by a user's mail client is treated as a PDF and silently yields
nothing.

Phase 1 accepts PDF, PNG, JPEG and TIFF. CSV and XLSX bank statements are
deliberately excluded here rather than half-supported: they have no pages, and
`document.page_range` is NOT NULL, so they need a different ingest shape. That
is Phase 1e's problem, and it should be solved on purpose.

---

## 32. Page count and text-layer detection run on the worker, not in the request

**Status:** accepted (Phase 1b)

`POST /uploads` stores the file and returns. `source_file.page_count` and
`has_text_layer` stay NULL until `ingest.probe_source_file` fills them in.

**Why:** profiling parses the whole PDF, and a year of bank statements is
hundreds of pages. Doing it inline would make upload latency a function of
document size, and would put a CPU-bound parse in the API process. The columns
were already nullable in the Phase 0 schema for exactly this ("NULL until
detection runs").

**How `has_text_layer` is decided:** a bounded, evenly spaced sample of at most
10 pages; a page counts as text at 20+ non-whitespace characters; the document
has a text layer if at least half the sampled pages do.

- *Not "any text at all":* scanned pages routinely carry a stray text object —
  a folio number, a date stamp, a fax header. A threshold of one character
  would classify every scan as a text document and route it to a parser that
  returns nothing.
- *Not every page:* a 500-page statement must not cost 500 text extractions to
  answer one boolean. The page **count** is never sampled, though — that is
  exact, because `document.page_range` is validated against it.
- *Both ends of the document are always sampled*, because covering letters and
  scanned annexures live at the edges.

`pages_sampled` and `pages_with_text` are kept in the audit entry so a
surprising classification can be explained without re-running anything.

**Encrypted PDFs get their own error class.** Indian banks mail statements
locked to PAN plus date of birth, so this is routine rather than exceptional.
The uploader can fix it in seconds if told what is wrong and cannot if it
arrives as a generic parse failure. Accepting a password is Phase 1e.

---

## 33. The Phase 1 tenant header fails closed outside local and test

**Status:** accepted (Phase 1b) — **temporary, and load-bearing that it stays so**

`require_tenant` reads `X-Tenant-Id`. Authentication is Phase 4 (#5), so until
then any caller can name any tenant. The dependency therefore returns **503 on
every request** when `environment` is anything other than `local` or `test`.

**Why not just leave a TODO:** a stopgap that works everywhere is a stopgap
that ships. This one cannot: a deployment that reaches staging or production
with it still in place is inert rather than wide open, and the failure is
immediate and total instead of quiet and selective. The cost of being wrong
here is a cross-tenant breach in a product whose entire premise is tenant
isolation, so the guard is worth more than the inconvenience.

There is a test that monkeypatches the environment to `production` and asserts
the 503, so removing the guard turns the suite red.

**Note:** RLS is unaffected by any of this. A forged `X-Tenant-Id` selects a
tenant, but every query still runs inside `tenant_session` under `app_rw`, so
it can only reach that tenant's rows. The header is an authentication gap, not
an isolation one — which is why a 503 is sufficient rather than a redesign.

---

## 34. No working credential is ever a default

**Status:** accepted (Phase 1b hardening)

The repository is public. Before this change, `.env.example` contained values
that *worked* — `app_rw_dev_password`, `minioadmin`,
`change-me-in-every-environment` — and migration 0002 carried the same strings
as fallbacks when the environment variables were unset.

**Why that combination is the dangerous one:** either half alone is fine. A
published placeholder that does not work is harmless. A working default that is
private is merely untidy. Published *and* working means a deployment that
forgets one environment variable comes up green, passes its health checks, and
is reachable with a password anyone can read on GitHub. Every failure mode is
silent, which is the property this project treats as disqualifying (#18, #22,
#25).

Four changes, in increasing order of durability:

1. **`.env.example` holds `__GENERATE__` placeholders** and the one-liner that
   generates real values. Copying it without editing produces a configuration
   that refuses to start.
2. **Migration 0002 has no password fallback.** An unset, published, short, or
   quote-containing `APP_*_PASSWORD` raises before any role is created. Loud,
   early, and trivially fixable — versus a role whose credentials are public.
3. **`check_secret_strength()` refuses to start** outside `local`/`test` when
   any secret is on the published blocklist or under 24 characters. The
   blocklist catches what we thought of; the length floor catches what we did
   not. The local/test exemption exists because forcing 32-character passwords
   on a developer's laptop buys nothing and makes onboarding worse.
4. **Local credentials were rotated** and the volumes destroyed and rebuilt, so
   nothing running uses a value that was ever published.

**Why the guard is not a pydantic validator.** It was, and that leaked.
Pydantic appends `input_value={...}` to every `ValidationError`, and the input
to a settings model is the raw environment — so the guard printed the very
secrets it existed to protect into the crash log of the one boot that failed
it. It now raises `WeakSecretError` from plain Python in `get_settings()`,
where the message is exactly what we wrote. There is a test asserting the error
names the variable and never its value.

The cost, stated plainly: `Settings(...)` constructed directly no longer
self-validates. That is why the project rule is that configuration comes from
`get_settings()`.

**Editing a shipped migration, once.** DECISIONS #16 and #17 say migrations are
frozen, because re-running an old one against a newer codebase must do what it
did originally. Changing 0002 breaks that rule. It was done anyway, deliberately
and once, because 0002 had been applied to exactly one throwaway development
database and to nothing else, and because the alternative — a 0003 that rotates
passwords while 0002 keeps creating published ones for every fresh clone — is
strictly worse. This is the last moment the edit is free. It does not set a
precedent.

**Not addressed here:** the old values remain in git history and always will.
That is acceptable because they now unlock nothing: local credentials are
rotated, and the blocklist means those exact strings can never be used again in
a real environment. Rewriting history was rejected as security theatre for
placeholder values.
