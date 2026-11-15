# Database

PostgreSQL 16 holds everything: records, the queues the workers claim from, the agent's runs, sessions and the audit
log. Redis holds only short-lived data (the session cache, rate-limit counters, the live event streams and the
worker wake-up signal). The schema is the migration chain in `backend/alembic/versions/`; the SQLAlchemy models in
`backend/app/models/` mirror it and are not a second source of truth.

## Migrations

Thirty-one tables are created by the chain. The revisions in the tree are `0001`, `0003` to `0006`, `0010` to
`0019`; there is no file for `0002`, `0007`, `0008` or `0009`, and the chain is unbroken because each revision names
its predecessor in `down_revision`. Migrations run as the owner role, straight to PostgreSQL (not through PgBouncer),
under the advisory lock 724101, one transaction per migration (`backend/alembic/env.py`). The `migrate` service of
`docker-compose.yml` runs them with `python -m app.bootstrap` before the API and workers start.

| Revision | Adds |
|---|---|
| 0001 | `app_current_org()`, `app_is_system()`; `orgs`, `users`, `sessions`, `audit_log`; row-level security on `users` and `audit_log`; the audit log made append-only |
| 0003 | `evidence`: stored query results and computations that a report cites |
| 0004 | `llm_settings`: an organisation's model settings, with the key encrypted |
| 0005 | `runs`, `steps`, `approvals`: the agent's queue, its committed steps and the tool calls waiting for a decision; the `evidence` to `runs` foreign key |
| 0006 | `scheduled_jobs`: one row per recurring job |
| 0010 | `vendors`, `vendor_aliases`, `vendor_bank_accounts` |
| 0011 | `documents` (also the document queue) and `inbox_keys` |
| 0012 | `document_pages`: every word of every page with its box |
| 0013 | `extractions`: what extraction found for each document |
| 0014 | `purchase_orders`, `po_lines`, `po_changes`, `goods_receipts`, `receipt_lines`, `invoices`, `invoice_lines`, `field_provenance` |
| 0015 | `org_ap_settings`, `matches`, `match_lines` |
| 0016 | `exceptions` |
| 0017 | `invoice_approvals`; `invoices.pending_since` and `escalated_at`; the trigger that starts the approval clock |
| 0018 | `drafts`: supplier messages drafted by the agent |
| 0019 | Two list indexes, `purchase_orders (org_id, created_at DESC)` and `invoices (org_id, status, created_at DESC)` |

Migration 0019 came out of the scale measurement: `docs/SCALE.md`, section 3, reports the two slow lists that
`backend/load/explain.py` found at 100,000 invoices, and the migration as their fix. The migration's docstring says
the same.

## Roles, connections and the pooler

Three paths reach the database, and each has its own role.

| Role | Created by | Used by | Notes |
|---|---|---|---|
| `procuremind_owner` | `infra/postgres/init.sh` | Migrations (`alembic/env.py`), the load and demo scripts; `DATABASE_URL_OWNER` | Owns the database and every table. Connects straight to PostgreSQL on port 5432. The API and workers never use it |
| `procuremind_app` | `infra/postgres/init.sh` | The API and the workers; `DATABASE_URL` | Not an owner and not `BYPASSRLS`, so row-level security always applies to it. Gets `SELECT, INSERT, UPDATE, DELETE` on tables and usage on sequences through `ALTER DEFAULT PRIVILEGES` for the owner |

`init.sh` also revokes every privilege on the `public` schema from `PUBLIC`, and installs the `pg_trgm` extension, which
the matcher uses (`similarity()` in `backend/app/matching/match.py`) to pair invoice lines with order lines by
description.

**PgBouncer** (`infra/pgbouncer/entrypoint.sh`) runs in transaction pooling mode: a server connection is held only
for one transaction (default pool 40, client limit 5000). Two consequences shape the code. Nothing set at session
level survives to the next transaction, so the tenant is set with `set_config(..., true)`, which lasts for one
transaction (`backend/app/db.py`). And server-side prepared statements cannot be used, so the engine is created with
`prepare_threshold: None`. PgBouncer also sends `DISCARD ALL` when it returns a connection.

## Row-level security

Tenant tables are protected by the database, not by queries that remember to filter.

- `app_current_org()` returns the organisation set for the transaction, or NULL when none is set. `app_is_system()`
  is true when `app.system` is `on`. Both are `STABLE` SQL functions, and `procuremind_app` may execute them.
- `enable_rls(table, system_access=...)` in `backend/app/rls.py` enables and **forces** RLS on the table and creates
  one policy, `tenant_isolation`, with `USING` and `WITH CHECK` both equal to `org_id = app_current_org()`. With
  `system_access`, `OR app_is_system()` is added to both.
- `FORCE` makes the policy apply to the table owner as well. The owner role therefore sees only the rows of the
  organisation set for its transaction, unless it sets the same variables.
- `tenant_session(org_id)` (`backend/app/db.py`) starts a transaction and runs
  `SELECT set_config('app.org_id', <org>, true)`. Every request, and every worker step for a document or run,
  uses it.
- `system_session()` sets `app.system` to `on` for one transaction. It is used only where the organisation is not
  yet known or where the work spans organisations: sign-in and the session lookup, inbox key lookup, claiming runs and
  documents and renewing their leases, the scheduler, the cross-organisation daily cost sum, and the metrics scrape.
  It is also used to read an organisation's name for the agent's context. A system session sees every row of the four
  tables that have `system_access`, and sees the tables with no RLS as any other session does; it sees no rows of the
  other tenant tables.

Tables with `system_access`: `users` (sign-in finds the user by e-mail before the organisation is known), `runs` and
`documents` (workers claim across organisations), and `inbox_keys` (a key arrives before anything is known about its
organisation).

Tables with **no** row-level security: `orgs` (the list of organisations, read by the scheduler and the agent's
organisation name), `sessions` (looked up by token hash before any organisation is set), and `scheduled_jobs`
(not tenant data). Because `sessions` is not protected by the database, any application connection can read every
session row, including the CSRF token. The protection there is the code in `backend/app/security/sessions.py`.

Tables with tenant RLS (28): `users`, `audit_log`, `evidence`, `llm_settings`, `runs`, `steps`, `approvals`,
`vendors`, `vendor_aliases`, `vendor_bank_accounts`, `documents`, `inbox_keys`, `document_pages`, `extractions`,
`org_ap_settings`, `matches`, `match_lines`, `invoice_approvals`, `exceptions`, `drafts`, `purchase_orders`,
`po_lines`, `po_changes`, `goods_receipts`, `receipt_lines`, `invoices`, `invoice_lines`, `field_provenance`.

A record of another organisation is simply not visible, so the application answers `404`.

One gap: a foreign key is checked by a trigger that runs without row-level security. A row in one organisation can
therefore name a parent row of another organisation by its id, and the database accepts it. The route that sets
`invoices.vendor_id` does not check the vendor (see `docs/API.md`), so this can happen today. Not tested.

## Audit log

`audit_log` is append-only for the application: `0001` runs `REVOKE UPDATE, DELETE ON audit_log FROM
procuremind_app`, and `TRUNCATE` is not granted by the default privileges. Each change writes its audit line in the
same transaction as the change (`audit()` in `backend/app/api/deps.py`), so a change and its record commit or roll
back together. Details are masked by `redact()` before they are stored: keys ending in `key`, `token`, `secret`,
`password`, `authorization`, `cookie` and similar, and values shaped like provider keys or bearer tokens. The owner
role could still change the table; nothing in the application does.

## Tables

Columns are listed for their meaning; the data types and every `CHECK` are in the migrations. "RLS" means tenant
row-level security; "system" means `system_access` as well.

### Identity and audit

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `orgs` | Organisations (tenants) | `id`, `name` | Primary key only. No RLS |
| `users` | People who sign in | `org_id` → `orgs`, `email` UNIQUE across all organisations, `password_hash` (scrypt), `role` CHECK viewer, clerk, approver, admin, `is_active` | `users_org_idx (org_id)` for per-organisation lists. RLS, system |
| `sessions` | Server-side sessions | `user_id` → `users` ON DELETE CASCADE, `token_hash` UNIQUE (SHA-256 of the cookie token), `csrf_token`, `expires_at` | `sessions_user_idx (user_id)` so that ending all of a user's sessions is one indexed delete. The unique `token_hash` is the lookup on every request. No RLS |
| `audit_log` | Who did what, and from where | `id` identity, `user_id` → `users` ON DELETE SET NULL, `action`, `target`, `detail` jsonb (masked), `ip` | `audit_org_time_idx (org_id, created_at DESC)` for the audit screen. Append-only (see above). RLS |

### Model settings and the agent's runs

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `llm_settings` | The organisation's model, one row per organisation | `org_id` primary key, `provider` CHECK (openai_compat, anthropic, ollama, stub), `model`, `base_url`, `api_key_enc` (Fernet, see `docs/PROVIDERS.md`), `fallback_model`, `price_input_per_mtok`, `price_output_per_mtok`, `updated_by` | Primary key. RLS. A missing row means the environment applies |
| `runs` | One question put to the agent, and how far it has got | `user_id` → `users` (NULL for automatic investigations), `role`, `question`, `status` CHECK (queued, running, waiting_approval, waiting_user, completed, failed, cancelled, budget_exceeded), `result` (the report), `pending`, `steps_used`, `tokens_used`, `cost_usd`, `active_ms`, `cancel_requested`, `lease_owner`, `lease_expires_at`, `attempts` | `runs_claim_idx (created_at) WHERE status IN ('queued', 'running')`: the claim query reads only live runs. `runs_org_active_idx (org_id) WHERE status IN ('queued', 'running')`: the per-organisation queue and concurrency counts. `runs_org_created_idx (org_id, created_at DESC)`: run lists and usage. RLS, system |
| `steps` | Every model call, tool call and runtime note of a run, committed before the next action | `run_id` → `runs` ON DELETE CASCADE, `idx`, `kind` CHECK (model, tool, note), `state` CHECK (started, awaiting_approval, awaiting_user, done), `call_id`, `input`, `output`, `error`, token and cost columns, `idempotency_key` UNIQUE, UNIQUE (`run_id`, `idx`) | The UNIQUE (`run_id`, `idx`) gives the ordered read of a run, and makes a second write of the same step fail, which the runtime turns into a conflict for the worker that lost the lease. `idempotency_key` UNIQUE is a hash of the step's identity. RLS |
| `approvals` | Tool calls waiting for an administrator's (or approver's) decision | `run_id` → `runs`, `step_idx`, `tool`, `arguments` jsonb (masked), `status` CHECK (pending, approved, rejected), `requested_by`, `decided_by`, `reason`, UNIQUE (`run_id`, `step_idx`) | `approvals_org_status_idx (org_id, status, created_at DESC)`: the approvals screen. No shipped tool needs approval yet, so this table is empty in practice. RLS |
| `evidence` | Results and computations a report cites, by handle (`ev_` + 8 hex) | `id` text primary key, `run_id` → `runs` ON DELETE CASCADE, `kind` CHECK (query, compute, chart), `sql`, `payload` jsonb, `row_count` | `evidence_run_idx (run_id)`: the evidence of one run. RLS |
| `scheduled_jobs` | Recurring jobs (the escalation job) | `name` primary key, `next_run_at`, `last_started_at`, `last_finished_at`, `last_error` | Primary key only. A worker runs a job only when it moves `next_run_at` forward. No RLS |

### Intake: documents, pages and extraction

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `documents` | Every file received, by upload or inbox; also each document's place in the queue | `sha256` char(64), `content_type`, `size_bytes`, `pages`, `kind` CHECK (invoice, credit_note, purchase_order, goods_receipt, statement, other), `status` CHECK (queued, processing, extracted, needs_review, failed, dead), `attempts`, `lease_owner`, `lease_expires_at`, `error`, `source` CHECK (upload, inbox), `uploaded_by` | UNIQUE (`org_id`, `sha256`): the same file twice is one document. `documents_queue_idx (received_at) WHERE status IN ('queued', 'processing')`: the claim. `documents_org_idx (org_id, received_at DESC)`: the list. `failed` is allowed by the CHECK but nothing writes it. RLS, system |
| `inbox_keys` | Keys that suppliers' mail systems use | `key_hash` char(64) UNIQUE (SHA-256 of the whole key), `prefix` (the first part, shown in the list), `revoked_at` | The UNIQUE `key_hash` is the lookup on every inbox request. A revoked key stays as a row with `revoked_at`. RLS, system |
| `document_pages` | The words of every page, with their boxes | PRIMARY KEY (`document_id`, `page`), `source` CHECK (text_layer, ocr, sheet), `width`, `height`, `text`, `words` jsonb | The primary key is the access path. Rewritten on each read of a document, so a retry starts clean. RLS |
| `extractions` | What extraction found: fields with where they were printed (or why not), lines, issues, flags | `document_id` primary key, `kind`, `fields` jsonb, `lines` jsonb, `issues` jsonb, `flags` jsonb (`disagreement`, `model_not_on_page`), `model_used` | Primary key. Upserted by the extraction step. RLS |

### Vendors

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `vendors` | Suppliers | `name` (1–200), `tax_id`, `email`, `default_currency` char(3) CHECK three capitals, `payment_terms_days` (0–365, default 30) | UNIQUE index `vendors_name_key (org_id, lower(name))`: no two vendors with the same name in a case-insensitive way. RLS |
| `vendor_aliases` | Other names a vendor's documents use | `vendor_id` → `vendors` ON DELETE CASCADE, `alias` (1–200) | UNIQUE `vendor_aliases_key (org_id, lower(alias))`: an alias names one vendor per organisation. RLS |
| `vendor_bank_accounts` | Bank accounts, with the two-person change process | `iban_or_account` (4–64), `bank_name`, `status` CHECK (pending, active, retired, rejected), `requested_by`, `decided_by`, `decided_at` | Partial UNIQUE `vendor_one_active_account (vendor_id) WHERE status = 'active'`: a vendor has at most one active account. Retired and rejected rows remain for the record. RLS |

### Purchase orders, receipts and invoices

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `purchase_orders` | Orders | `vendor_id` → `vendors`, `po_number`, `po_number_norm`, `currency`, `status` CHECK (open, closed), `document_id` (the order's document, if any) | UNIQUE (`org_id`, `po_number_norm`): one order per normalised number. `purchase_orders_org_idx (org_id, created_at DESC)` (0019): the order list. RLS |
| `po_lines` | Lines of an order | `po_id` → `purchase_orders` ON DELETE CASCADE, `line_no`, `item_code`, `description`, `quantity` (> 0, three decimals), `unit_price` (≥ 0, four decimals) | UNIQUE (`po_id`, `line_no`). RLS |
| `po_changes` | The history of an order: a snapshot before and after each change | `po_id`, `changed_by`, `at`, `before` jsonb, `after` jsonb | No index of its own. Read per order, which has few changes. RLS |
| `goods_receipts` | A delivery against an order | `po_id`, `receipt_no`, `received_at`, `document_id` | No UNIQUE on `receipt_no`, so the same number can be recorded twice for one order. RLS |
| `receipt_lines` | Quantities received per order line | `receipt_id` → `goods_receipts`, `po_line_id` → `po_lines`, `quantity` (> 0) | `receipt_lines_po_line_idx (po_line_id)`: the received total of each line, which every match reads. RLS |
| `invoices` | Invoices and credit notes; amounts as printed, a credit note counts against | `document_id` UNIQUE (one invoice per document), `vendor_id` → `vendors`, `vendor_name_printed`, `kind` CHECK (invoice, credit_note), `invoice_number`, `invoice_number_norm`, `invoice_date`, `due_date`, `po_number`, `currency`, `subtotal`, `tax`, `total` (14 digits, two decimals), `bank_account`, `status` CHECK (eight values, below), `issues` jsonb, `entered_by`, `corrected_by` uuid[], `pending_since`, `escalated_at` | `invoices_org_idx (org_id, created_at DESC)`. `invoices_vendor_number_idx (vendor_id, invoice_number_norm)`: the duplicate-number check. `invoices_pending_idx (org_id, pending_since) WHERE status = 'pending_approval'` (0017): the approval queue, oldest first. `invoices_org_status_idx (org_id, status, created_at DESC)` (0019): the lists filtered by status. RLS |
| `invoice_lines` | Lines of an invoice | `invoice_id` → `invoices` ON DELETE CASCADE, `line_no`, `item_code`, `description`, `quantity`, `unit_price` (four decimals), `amount` (two decimals) | UNIQUE (`invoice_id`, `line_no`). RLS |
| `field_provenance` | Where each extracted field was found | PRIMARY KEY (`invoice_id`, `field`), `located`, `page`, `bbox` real[], `confidence`, `candidate` (the rejected value when missing), `reason` | The primary key is the access path for the review screen. RLS |

### Matching, approvals and exceptions

| Table | Purpose | Key columns and constraints | Indexes and why |
|---|---|---|---|
| `org_ap_settings` | Tolerances, approval tiers and escalation delay, one row per organisation | `price_tolerance_pct` (default 2.00), `qty_tolerance_pct` (0.00), `total_tolerance_abs` (1.00), `approval_tiers` jsonb, `escalation_hours` (default 48, 1–720) | Primary key. Without a row, the defaults in `backend/app/matching/tolerances.py` apply. RLS |
| `matches` | The current match of each invoice | `invoice_id` UNIQUE → `invoices` ON DELETE CASCADE, `po_id` → `purchase_orders` ON DELETE SET NULL, `status` CHECK (matched, matched_with_variance, exception), `reasons` jsonb, `manual`, `matched_by` | UNIQUE on `invoice_id` (one current match). Each match replaces the row. RLS |
| `match_lines` | Per invoice line, what it was matched to | `match_id` → `matches`, `invoice_line_id`, `po_line_id`, `quantity`, `unit_price`, `price_variance_pct` | `match_lines_po_line_idx (po_line_id)`: the billed quantity already charged against each order line, for the next invoice. RLS |
| `invoice_approvals` | Each person's decision on an invoice | `invoice_id` → `invoices` ON DELETE CASCADE, `approver_id` → `users`, `approver_role`, `decision` CHECK (approved, rejected), `reason`, `at` | UNIQUE (`invoice_id`, `approver_id`): one decision per person. RLS |
| `exceptions` | Invoices held for a person to decide | `invoice_id` → `invoices` ON DELETE CASCADE, `reasons` jsonb (the match's reasons), `findings` jsonb, `status` CHECK (open, investigating, recommended, escalated, resolved), `recommendation` jsonb, `resolution` CHECK (six outcomes), `note`, `resolved_by`, `resolved_at` | `exceptions_one_open (invoice_id) WHERE status IN (open, investigating, recommended, escalated)`: at most one open exception per invoice. `exceptions_org_idx (org_id, created_at DESC)`: the list. RLS |
| `drafts` | Supplier messages drafted by the agent, for a person to copy | `exception_id` → `exceptions` ON DELETE CASCADE, `run_id` → `runs` ON DELETE SET NULL, `kind` CHECK (credit_note_request, corrected_invoice_request), `body` | No index of its own; read per exception. Nothing sends a draft. RLS |

## Triggers and functions

- **`invoices_pending_since`** (migration 0017), `BEFORE UPDATE OF status ON invoices`. When an update moves an
  invoice from another status into `pending_approval`, it sets `pending_since = now()` and clears `escalated_at`. An
  update that leaves an invoice in `pending_approval` does not restart the clock. The trigger covers every code path
  that moves an invoice into approval, which is why it is a trigger and not application code.
- **The escalation job** is not a trigger. `escalate_overdue_invoices` (`backend/app/approvals/chain.py`) runs every
  900 seconds on one worker, through the scheduler. It sets `escalated_at` on pending invoices older than the
  organisation's `escalation_hours`. Escalated invoices sort first in the approval queue.
- **`app_current_org()`** and **`app_is_system()`** (migration 0001) are the two functions of row-level security.

## Invoice lifecycle

Eight statuses, all in the `invoices.status` CHECK. The transitions below are every write to `status` in the code.

| From | Event | To | Where |
|---|---|---|---|
| (new) | Extraction is turned into an invoice; required fields missing, or the arithmetic does not agree | `needs_review` | `ap/records.py` `create_invoice`, `ap/review.py` |
| (new) | Same, with nothing wrong | `ready` | `ap/review.py` |
| `needs_review`, `ready`, `exception`, `matched`, `on_hold` | A person corrects a field (the stored match is deleted first) | `needs_review` or `ready`, recomputed | `api/invoices.py` `correct` |
| `ready`, `matched`, `on_hold`, `exception`, `pending_approval` | A match run (pipeline, or by hand or again through `POST .../match`) finds reasons | `exception` | `matching/match.py` `store_match` |
| The same statuses | A match run finds no reasons | `pending_approval`; a credit note becomes `matched` (applied, never paid) | `matching/match.py` `store_match` |
| `ready`, `matched`, `pending_approval` | A check scores 0.6 or more, or the match has reasons | `exception` | `checks/run.py` `open_exception` |
| `pending_approval` | Approver approves; the tier is satisfied | `approved` | `approvals/chain.py` `decide` |
| `pending_approval` | Any approver rejects | `rejected` (ends the invoice) | `approvals/chain.py` `decide` |
| `pending_approval` | Approver sends it back; approvals and the match are cleared | `ready` | `api/invoices.py` `send_back` |
| `approved` | Administrator reopens it with a reason; approvals are cleared | `pending_approval` | `api/invoices.py` `reopen` |
| `exception` | Resolved `approve_with_variance` | `pending_approval` (credit note: `matched`) | `api/exceptions.py` |
| `exception` | Resolved `hold`, `request_credit_note` or `request_corrected_invoice` | `on_hold` | `api/exceptions.py` |
| `exception` | Resolved `reject_duplicate` | `rejected` | `api/exceptions.py` |

A `needs_review` invoice cannot be matched; it must be corrected first (`409`, `api/invoices.py` `match`).

Points that matter in practice:

- `approved` and `rejected` have no transition back in the application, except the administrator's reopen of an
  approved invoice.
- A match run on a `pending_approval` invoice does not clear its approval decisions. The corrective paths
  (correction, send-back, reopen) do. See `docs/API.md`.
- Escalation does not change `status`. It changes `escalated_at`.

## Exception lifecycle

Five statuses. An exception is opened when the match has reasons or a check scores 0.6 or more
(`checks/run.py` `open_exception`). At most one open exception exists per invoice. Its `status` moves as follows.

| From | Event | To | Where |
|---|---|---|---|
| (none) | Match or check finds a reason | `open` | `checks/run.py` `open_exception` |
| `open`, `investigating`, `recommended`, `escalated` | A new check run refreshes `reasons` and `findings` | unchanged | `checks/run.py` |
| `open`, `recommended` | A person starts an investigation | `investigating` | `api/exceptions.py` `investigate` |
| `open` | Automatic investigation (`AUTO_INVESTIGATE=true`) starts with the exception | `investigating` | `checks/run.py` |
| `open`, `investigating` | The agent calls `recommend` | `recommended` (stays `escalated` if it was) | `exceptions/tools.py` |
| `open`, `investigating`, `recommended` | A person resolves it with `escalate` | `escalated`; the invoice is unchanged | `api/exceptions.py` `resolve_exception` |
| `open`, `investigating`, `recommended`, `escalated` | A person resolves it with an outcome, or accepts the recommendation | `resolved`, with `resolution`, `note`, `resolved_by`, `resolved_at` | `api/exceptions.py` |

An escalated exception can be decided only by an administrator. If an investigation fails before it recommends
anything, the exception stays `investigating`; investigating again is allowed.

## Agent run lifecycle

A run is queued by the API (`POST /api/investigations`, the investigate endpoint or the automatic path). A worker
claims the oldest queued run, or a running one whose lease has expired, and writes its own id and a 30-second lease
(`backend/app/worker/run.py`). Each action (one model call, one tool call, one resolution) commits as one `steps`
row before the next starts, so the run continues from its last committed step after a crash.

- `queued` → `running` on a claim. A run is claimed only while its organisation has fewer than
  `ORG_MAX_CONCURRENT_RUNS` running.
- `running` → `waiting_user` when the agent asks a question; `waiting_user` → `queued` when the person answers.
- `running` → `waiting_approval` when a tool needs a decision; → `queued` when it is decided.
- `running` → `completed` when the agent calls `finish`; `failed` on a model error, a bad tool call twice in a row, a
  run that does not converge in 400 iterations, or a run claimed more than five times (its worker kept dying);
  `budget_exceeded` when a per-run step, token, time or cost limit, or the organisation's or installation's daily
  cost limit, is reached (checked before each model call); `cancelled` when the person cancels.

## Document lifecycle

`queued` → `processing` on a claim (120-second lease) → then the steps run in order: text and classification, extraction,
records, matching, checks. Extraction sets `extracted` when every required field is located and the arithmetic
agrees, and `needs_review` otherwise. A step error puts the document back in `queued` with a back-off of 5 seconds
per attempt, and after five attempts in `dead`, with the reason in `error` (`backend/app/intake/queue.py`).
