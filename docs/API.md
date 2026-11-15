# API

Every HTTP route of the FastAPI backend (`backend/app/main.py` includes the routers of `backend/app/api/`). The
interactive reference is at `/docs` on the API port; this file adds who may call each route, what it does, the
request fields and the errors that matter. Field limits are the pydantic constraints in each module.

## Conventions

**Addresses.** Routes are under `/api/`, except three that are not: `/healthz` and `/readyz` (liveness and
readiness), `/metrics` (Prometheus), and `POST /v1/inbox` (suppliers' mail systems, authenticated with an inbox
key). With Docker Compose the API is published on `127.0.0.1:8300`, and the interactive docs are served there
(`/docs`, `/redoc`, `/openapi.json`; the security headers exempt these paths from the strict content policy).

**Sessions.** `POST /api/auth/login` and `POST /api/auth/signup` set an `HttpOnly`, `SameSite=lax` cookie named
`procuremind_session`, with `Secure` when `COOKIE_SECURE=true`, valid for `SESSION_HOURS` (12). The browser keeps
a random token; the database keeps only its SHA-256 (`backend/app/security/sessions.py`). A resolved session is
cached in Redis for up to 60 seconds; logout, deactivation and password change delete the cache entry at once.

**CSRF.** Every request whose method is not `GET`, `HEAD` or `OPTIONS` must send the header
`X-CSRF-Token` with the value returned as `csrf_token` by signup, login or `GET /api/auth/me`. A missing or wrong
value is `403 Missing or invalid CSRF token.`. The cookie alone is not enough, because the browser sends it
automatically and the header is not. Signup and login are the two POSTs that do not need it, since they create the
session.

**Roles and row-level security.** Four roles: `viewer`, `clerk`, `approver`, `admin`. Each route lists the roles it
accepts (`require_role(...)` in `backend/app/api/deps.py`); "any signed-in" means `current_user`, which accepts
every role, viewers included. The roles are not a ranking: a route for `clerk` and `approver` does not admit a
viewer unless the table says so. Every query runs in a transaction bound to the caller's organisation, and
PostgreSQL row-level security enforces it (see `docs/DATABASE.md`). A record of another organisation therefore
answers `404` ("not found"), the same as a record that does not exist. The one exception is a foreign id written
into a field that is not looked up under row-level security (see the note under `PATCH /api/invoices/{id}/fields`).

**Errors.** Errors are `{"detail": ...}`. A string `detail` is a message for the person; validation of a request
body is FastAPI's `422` with a list of `{loc, msg, type}` objects. The codes the application raises:

| Code | Meaning in this application |
|---|---|
| 401 | No valid session (`Not signed in.`), or an inbox key that is missing, unknown or revoked |
| 403 | The role does not allow the action, the CSRF token is wrong, or a separation-of-duties rule applies |
| 404 | Not found in this organisation (also used for another organisation's ids) |
| 409 | The state does not allow it (already decided, wrong invoice status, last administrator, duplicate e-mail) |
| 413 | Body or file larger than the limit (25 MB per inbox message, 20 MB per file) |
| 415 | File type not accepted |
| 422 | Invalid value, or a rule the application applies after validation |
| 429 | Rate limit, or the organisation's run queue is full (`Retry-After` set) |
| 503 | See below: busy database pool, or the rate limiter cannot reach Redis |

**Overload.** When every database connection of an API process has been in use for the whole pool wait, the
response is `503` with body `{"detail": "The service is busy. Try again in a few seconds."}` and header
`Retry-After: 5` (`backend/app/main.py`, the `PoolTimeout` handler). It is not a `500`. Separately, if Redis cannot
answer the rate limiter the request gets `503 Rate limiter unavailable; try again shortly.`
(`backend/app/security/ratelimit.py`).

**Rate limits.** Sliding windows in Redis, shared by all API processes:

| Limit | Key | Default | Where |
|---|---|---|---|
| Sign-in attempts | per client address | 30 per minute (`LOGIN_IP_PER_MINUTE`) | `POST /api/auth/login` |
| Sign-in attempts | per e-mail address | 5 per minute (`LOGIN_ATTEMPTS_PER_MINUTE`) | `POST /api/auth/login` |
| Sign-ups | per client address | 10 per hour (`SIGNUP_IP_PER_HOUR`) | `POST /api/auth/signup` |
| New agent runs | per user | 10 per minute (`RUNS_PER_MINUTE`) | `POST /api/investigations`, `POST /api/exceptions/{id}/investigate` |
| Model test | per user | 6 per minute | `POST /api/settings/llm/test` |

The client address is the first value of `X-Forwarded-For` when `TRUSTED_PROXY=true`, which Compose sets by default.
Runs also have an organisation queue limit: when queued plus running runs reach `ORG_MAX_CONCURRENT_RUNS` plus
`ORG_MAX_QUEUED_RUNS` (5 + 15), a new run gets `429` with `Retry-After: 15`.

**Headers.** Requests may carry `X-Request-ID` (up to 64 characters); every response carries one, and the same
value appears in the log line. `/api/` responses are `Cache-Control: no-store`. Every response has
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a restrictive
`Content-Security-Policy`. CORS allows the origins in `CORS_ORIGINS`, with credentials.

**Values.** Money amounts are strings with two decimals (`"1234.50"`); quantities and unit prices are strings in
the shortest exact form (`"2"`, `"0.125"`). Timestamps are ISO 8601. Identifiers are UUIDs, except evidence handles
(`ev_` plus eight hex characters) and audit entries (integers). Cost figures are numbers.

**Paging.** Lists take `limit` where the table says so. A few lists are fixed (orders, exceptions: 500 rows;
vendors: all). Audit and agent runs page with `before`.

**Redis.** Sessions (cache), the rate limiter, the live event stream and the worker wake-up signal use Redis. If it
is down, the rate-limited routes return `503`, and the session lookup for any signed-in request raises an error
(`backend/app/security/sessions.py`); `GET /readyz` reports `redis` as failing. A signed-in request while Redis is down
reads its session from PostgreSQL instead, and a cache entry that cannot be removed at logout expires within 60 seconds.

---

## Health and metrics

| Method | Path | Who | What it does | Notes |
|---|---|---|---|---|
| GET | `/healthz` | anyone | Process is up. Returns `{"status": "ok"}` | Touches no dependency |
| GET | `/readyz` | anyone | Checks PostgreSQL (`SELECT 1` through `DATABASE_URL`) and Redis (`PING`), 2-second timeouts | `200 {"status": "ready"}`, or `503 {"status": "unavailable", "failing": ["database", "redis"]}` |
| GET | `/metrics` | anyone, or the bearer token when `METRICS_TOKEN` is set | Prometheus text: request latency per route template and status (per process); runs by status, oldest queued age, tokens and estimated cost of all runs (read from the database at scrape time) | `401` without the right `Authorization: Bearer` header when a token is set. Not in the OpenAPI document |

## Auth

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| POST | `/api/auth/signup` | anyone | Creates an organisation and its first user, who becomes `admin`; sets the session cookie | `email`, `password`, `name` (1–100), `org_name` (2–100) | `403` when `ALLOW_SIGNUP=false`; `409` e-mail already used; `422` weak password (at least 10 characters, a letter and a number, at most 200) or invalid e-mail; `429` per address per hour |
| POST | `/api/auth/login` | anyone | Verifies the password (always against a real hash, so a missing account takes the same time), sets the cookie, writes `auth.login` to the audit log | `email`, `password` (1–200) | `401 Incorrect e-mail or password.` for an unknown account, a wrong password or an inactive user alike; `429` |
| POST | `/api/auth/logout` | any signed-in | Deletes the session row and its cache entry, clears the cookie | none | `401`, `403` CSRF; returns `204` |
| GET | `/api/auth/me` | any signed-in | The user and the session's `csrf_token` | none | `401` |
| POST | `/api/auth/password` | any signed-in | Changes the password; ends the user's other sessions (this one is kept) | `current_password`, `new_password` (1–200) | `403 Current password is incorrect.`; `422` weak new password; returns `204` |

Responses from signup and login: `{"user": {id, org_id, email, name, role}, "csrf_token": "..."}`.

## Users

Administrators only.

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/users` | admin | Every user of the organisation, oldest first | none | |
| POST | `/api/users` | admin | Creates a user in the caller's organisation (`201`) | `email`, `name` (1–100), `role` (`viewer`, `clerk`, `approver`, `admin`), `password` (same rules as signup) | `409` e-mail already used anywhere (addresses are unique across organisations); `422` weak password |
| PATCH | `/api/users/{user_id}` | admin | Changes name, role or active flag. Deactivation ends the user's sessions; a role or name change refreshes cached sessions | `name`, `role`, `is_active` (all optional) | `404` (also for another organisation's user); `409` the organisation would have no active administrator left |

## Audit

| Method | Path | Who | What it does | Query | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/audit` | admin | Newest first. Each entry: `id`, `at`, `user` (e-mail), `action`, `target`, `detail` (secrets masked), `ip` | `limit` (1–200, default 50), `before` (an entry id), `action` (exact or prefix, such as `user.`) | returns `next_before` when more exist |

The log is append-only for the application role: it has no `UPDATE` or `DELETE` privilege (`docs/DATABASE.md`).
Inbox messages are not audited (see `POST /v1/inbox`).

## Matching and approval settings

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/ap-settings` | any signed-in | The organisation's tolerances, approval tiers and escalation delay; defaults until an administrator saves | none | |
| PUT | `/api/ap-settings` | admin | Saves the settings. Fields left out keep their value | `price_tolerance_pct` (0–100), `qty_tolerance_pct` (0–100), `total_tolerance_abs` (≥ 0), `approval_tiers` (1–10 items, see below), `escalation_hours` (1–720) | `422` when the tiers are not ordered: each tier's `up_to` must be above the one before, and only the last may be `null` |

A tier is `{"up_to": <amount or null>, "approvals": 1–5, "role": "approver" | "admin"}`. The invoice's total picks
the first tier whose `up_to` is not below it. The defaults are up to 1,000 (one approver), up to 10,000 (two
approvers) and above that (two approvers, one of them an administrator). A tier's `role` says who must give the
last approval of that tier.

Responses carry `price_tolerance_pct`, `qty_tolerance_pct` and `total_tolerance_abs` as two-decimal strings.
After a save, a tier's `up_to` comes back as a string (`"1000"`), while the defaults are numbers; the matcher reads
both.

## Documents and inbox

### Documents

| Method | Path | Who | What it does | Body or query | Notable errors |
|---|---|---|---|---|---|
| POST | `/api/documents` | clerk, approver, admin | Uploads 1–10 files as multipart form data (`201`). Each is checked by its content, not its name; the same file twice in an organisation is one document. Duplicates are answered with `duplicate_of` set | files | `422` 0 or more than 10 files, empty file, password-protected or unreadable PDF, more than 200 pages; `413` over 20 MB; `415` not PDF, PNG, JPEG, TIFF, XLSX or CSV. One bad file rejects the whole request and nothing is kept |
| GET | `/api/documents` | any signed-in | Newest first | `status` (queued, processing, extracted, needs_review, dead, ...), `limit` (1–200, default 50) | |
| GET | `/api/documents/{document_id}` | any signed-in | One document: `filename`, `content_type`, `size_bytes`, `pages`, `kind`, `status`, `error`, `source` (`upload` or `inbox`), `inbox_from` and `inbox_subject` (inbox documents only), `received_at` | | `404` |
| GET | `/api/documents/{document_id}/file` | any signed-in | The original file, as an attachment named by the first 12 characters of its SHA-256, with its sniffed type and `nosniff` | | `404` |
| GET | `/api/documents/{document_id}/pages` | any signed-in | For each page: `source` (`text_layer`, `ocr` or `sheet`), size, and every word with its box | | `404` |
| GET | `/api/documents/{document_id}/pages/{page}.png` | any signed-in | One page as a PNG (PDF at 110 dpi; images as frames; cached privately for an hour) | | `404` no such page, or a spreadsheet (no page image) |

### Inbox keys

Administrators manage keys. A key looks like `ik_<8 hex>_<secret>`. Only its SHA-256 is stored; the full key is
returned once, at creation.

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/inbox-keys` | admin | Keys with name, prefix, creation and revocation times | | |
| POST | `/api/inbox-keys` | admin | Creates a key (`201`); the response adds `key` | `name` (1–80) | |
| DELETE | `/api/inbox-keys/{key_id}` | admin | Revokes a key at once (`204`) | | `404` not found or already revoked |

### Inbox endpoint

| Method | Path | Who | What it does | Headers and body | Notable errors |
|---|---|---|---|---|---|
| POST | `/v1/inbox` | holder of an inbox key | Receives attachments the way a mailbox would. Each attachment becomes a document with `source: "inbox"`; the response is `200 {"documents": [...]}` | `Authorization: Bearer ik_...`. JSON body: `{"from": "<sender>", "subject": "<text>", "attachments": [{"filename": "...", "content_base64": "..."}]}`; 1–10 attachments; `from` up to 320 characters, `subject` up to 500 (optional) | `401` missing, unknown or revoked key; `413` body over 25 MB or a file over 20 MB; `422` malformed JSON, bad base64, or a file that fails the same checks as upload; the whole message is refused |

The sender and subject are stored with each document (`inbox_from`, `inbox_subject`) and returned by the documents API;
inbox receipts write no audit entry.
Attachments are processed exactly like uploads from that point on. The endpoint has no rate limit of its own.

## Invoices

Invoices and credit notes share one table; `kind` says which. Matching and approval logic is described in
`docs/DATABASE.md` (the lifecycle of an invoice).

| Method | Path | Who | What it does | Body or query | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/invoices` | any signed-in | Newest first | `status` (one of the eight statuses), `limit` (1–500, default 100) | |
| GET | `/api/invoices/{invoice_id}` | any signed-in | Fields, lines, and `provenance`: for each field, whether it was `located`, its `page`, `bbox`, `confidence`, the rejected `candidate` and the `reason` when missing | | `404` |
| PATCH | `/api/invoices/{invoice_id}/fields` | clerk, approver, admin | Corrects fields. Only the fields sent change. Deletes the stored match, then recomputes `needs_review` or `ready`. Records who corrected it | `vendor_id`, `invoice_number` (≤ 60), `invoice_date`, `due_date`, `po_number` (≤ 60), `currency` (three capitals), `subtotal`, `tax`, `total` (up to 14 digits, two decimals), `bank_account` (≤ 64) | `422 Nothing to change.`; `409` if the status is not one of needs_review, ready, exception, matched, on_hold |
| POST | `/api/invoices/{invoice_id}/match` | clerk, approver, admin | Runs the three-way match now (or by hand, against `po_id`), stores the result, and opens an exception when one is due. A manual match is audited as `invoice.matched_by_hand` | `po_id` (optional) | `409 Correct the invoice first.` when needs_review; `409` for any other status not in ready, matched, exception, pending_approval, on_hold; `404 Purchase order not found.` for an unknown `po_id` |
| GET | `/api/invoices/{invoice_id}/match` | any signed-in | The stored match: status, order, reasons, and per line the ordered, received, previously invoiced and invoiced quantities, both prices and the variance | | `404` not matched yet |
| POST | `/api/invoices/{invoice_id}/reopen` | admin | Takes an approved invoice back to `pending_approval`; clears every approval; the reason is recorded | `reason` (3–1000) | `409` only an approved invoice can be reopened |
| POST | `/api/invoices/{invoice_id}/send-back` | approver, admin | Returns a waiting invoice to `ready`, clearing approvals and the match, so it is checked again before approval | `reason` (3–1000) | `409` only an invoice waiting for approval can be sent back |

Note on `PATCH .../fields`: `vendor_id` is written as sent. The route does not check that the vendor belongs to the
caller's organisation, and the database's foreign-key check does not apply row-level security, so neither layer
rejects another organisation's vendor id. Matching then compares that id like any other. Not tested.

Note on `POST .../match`: it accepts `pending_approval` invoices and does not clear their approval decisions, which
the correction and send-back paths do clear.

## Purchase orders and receipts

Clerks, approvers and administrators write; everyone signed in reads.

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| POST | `/api/orders` | clerk, approver, admin | Creates an order with its lines (`201`), audited | `vendor_id`, `po_number` (1–60), `currency`, `lines` (1–500): each `item_code` (≤ 60, optional), `description` (1–300), `quantity` (> 0, three decimals), `unit_price` (≥ 0, four decimals) | `404 Vendor not found.` (a vendor of another organisation included); `409` the order number already exists (compared after normalisation, so `PO-0012` and `po 12` clash) |
| GET | `/api/orders` | any signed-in | Newest 500 orders | | |
| GET | `/api/orders/{po_id}` | any signed-in | The order with each line's `received` quantity, every recorded change (`before`, `after`, who, when) and the receipts | | `404` |
| PATCH | `/api/orders/{po_id}` | clerk, approver, admin | Opens or closes the order and/or changes lines. Each change stores a snapshot before and after | `status` (`open` or `closed`), `lines`: `{id, description?, quantity?, unit_price?}` | `404 Line not found on this purchase order.` |
| POST | `/api/orders/{po_id}/receipts` | clerk, approver, admin | Records a goods receipt (`201`). Locks the order while the receipt is written, so totals cannot race | `receipt_no` (1–60), `lines` (1–500): `po_line_id`, `quantity` (> 0) | `422` a line would exceed the quantity ordered; `404` line not on this order |

## Vendors

Reading is open to every signed-in role. Changing vendors and bank details is for administrators.

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/vendors` | any signed-in | All vendors, by name | | |
| GET | `/api/vendors/{vendor_id}` | any signed-in | The vendor, its aliases, and its bank accounts of every status with the full account number (see the note below) | | `404` |
| POST | `/api/vendors` | admin | Creates a vendor with aliases (`201`) | `name` (1–200), `tax_id` (≤ 40), `email` (≤ 200), `default_currency`, `payment_terms_days` (0–365, default 30), `aliases` (≤ 20) | `409` a vendor with this name or alias already exists (case-insensitive) |
| PATCH | `/api/vendors/{vendor_id}` | admin | Changes fields. Given `aliases`, replaces the whole alias list | the same fields, all optional | `409` as above |
| POST | `/api/vendors/{vendor_id}/bank-accounts` | admin | Requests a new bank account. It is `pending` until a second administrator decides (`201`) | `iban_or_account` (4–64; spaces removed), `bank_name` (1–120) | `404` vendor |
| POST | `/api/vendors/{vendor_id}/bank-accounts/{account_id}/decide` | admin | Approves or rejects a pending account. Approval makes it `active` and retires the previous active one | `decision` (`approved` or `rejected`), `reason` (≤ 500) | `403` the requester may not decide while another active administrator exists; `409` already decided; `404` |

A sole administrator may decide their own request; the audit entry records `sole_admin: true`.

Note on `GET /api/vendors/{vendor_id}`: the route returns `iban_or_account` in full to every signed-in role. This does
not match `docs/SECURITY.md`, which says the full account is shown only to administrators.

## Approvals

### Agent approvals (tool calls waiting for an administrator)

The `approvals` table holds tool calls of the Exception Agent that a person must allow. No tool of the shipped agent
is registered with the `needs_approval` level, so the table stays empty today. The routes work for any tool that
registers that level.

| Method | Path | Who | What it does | Query or body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/approvals` | clerk, approver, admin | Approvals by status, newest first. Administrators see all; other roles see only those they requested | `status` (`pending` default, `approved`, `rejected`, `all`), `limit` (1–200, default 50) | |
| POST | `/api/approvals/{approval_id}/decide` | approver, admin | Approves or rejects. A decision returns the waiting run to the queue | `decision`, `reason` (≤ 500) | `409` already decided; `403` the person who started the run may not decide it while another approver or administrator exists (a sole decider may, and it is recorded) |

### Invoice approvals (payment decisions)

| Method | Path | Who | What it does | Query or body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/ap-approvals` | clerk, approver, admin | Invoices waiting for approval: escalated first, then oldest waiting. Each with the approvals so far, the approvals still needed, `waiting_for`, vendor and total | `mine` (boolean): only the invoices this person may decide. For a clerk this is always empty | |
| POST | `/api/ap-approvals/{invoice_id}/decide` | approver, admin | Records one decision. An approval moves the invoice to `approved` only when the tier is satisfied; a rejection moves it to `rejected` | `decision`, `reason` (≤ 1000) | `409` not waiting for approval, or this person already decided; `403` separation of duties: the person entered, corrected, matched by hand, or accepted the variance of this invoice |

## Exceptions and the Exception Agent

An exception is an invoice held out of approval because the match failed or a check raised a finding (lifecycle in
`docs/DATABASE.md`).

| Method | Path | Who | What it does | Body or query | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/exceptions` | any signed-in | Newest 500. `open` means open, investigating, recommended or escalated | `status` (`open` or one status) | |
| GET | `/api/exceptions/{exception_id}` | any signed-in | One exception with its reasons, findings, recommendation, resolution, invoice summary | | `404` |
| POST | `/api/exceptions/{exception_id}/resolve` | approver, admin | Decides the exception. Effects per outcome below | `outcome`, `note` (≤ 1000) | `409` already resolved; `403` an escalated exception is decided by an administrator |
| POST | `/api/exceptions/{exception_id}/investigate` | clerk, approver, admin | Starts an Exception Agent run on it (`202` with `run_id`); the exception becomes `investigating` | none | `409` already resolved; `429` see the run limits |
| POST | `/api/exceptions/{exception_id}/accept-recommendation` | approver, admin | Applies the stored recommendation under the note "Accepted the agent's recommendation.", recorded with source `agent` | none | `409` no recommendation yet, or already resolved; `403` the person who started the investigation may not accept it while another approver or administrator exists |
| GET | `/api/exceptions/{exception_id}/drafts` | any signed-in | Supplier messages the agent drafted (`credit_note_request` or `corrected_invoice_request`), newest first. Nothing is sent | | `404` |

Outcomes of `resolve` (the same values the agent may recommend):

| Outcome | Exception becomes | Invoice becomes |
|---|---|---|
| `approve_with_variance` | resolved | `pending_approval`; a credit note becomes `matched` (applied, not paid) |
| `hold` | resolved | `on_hold` |
| `request_credit_note` | resolved | `on_hold` |
| `request_corrected_invoice` | resolved | `on_hold` |
| `reject_duplicate` | resolved | `rejected` |
| `escalate` | `escalated` (still open) | unchanged |

## Investigations (agent runs)

A run is one question put to the agent. Viewers may read runs; clerks, approvers and administrators may start and
steer them. Only the person who asked, or an administrator, may cancel or answer a run.

| Method | Path | Who | What it does | Body or query | Notable errors |
|---|---|---|---|---|---|
| POST | `/api/investigations` | clerk, approver, admin | Queues a run (`202` with `id`) | `question` (3–2000 characters after whitespace is collapsed) | `429` per-user limit, or the organisation queue is full (`Retry-After: 15`) |
| GET | `/api/investigations` | any signed-in | Runs, newest first | `limit` (1–100, default 30), `before` (an ISO timestamp), `mine` (boolean) | `422` `before` is not an ISO timestamp |
| GET | `/api/investigations/{run_id}` | any signed-in | The run: `status`, `error`, `pending` (what it waits for), counts of steps, tokens, cost; its `steps` (model and tool calls, with tool inputs redacted, summaries, latency); and `report` once finished | | `404` |
| GET | `/api/investigations/{run_id}/evidence/{handle}` | any signed-in | A stored query result or computation the report cites. Rows beyond 500 are omitted and counted in `rows_omitted` | | `404` |
| POST | `/api/investigations/{run_id}/cancel` | clerk, approver, admin | A queued or waiting run ends now. A running one stops before its next step; its status reads `cancelling` until then | none | `403` not the asker and not an administrator; a finished run returns its status unchanged |
| POST | `/api/investigations/{run_id}/answer` | clerk, approver, admin | Answers the agent's clarifying question; the run is queued again | `text` (1–1000) | `409` the run is not waiting for an answer; `403` as above |
| GET | `/api/investigations/{run_id}/events` | any signed-in | Live events over Server-Sent Events (see below) | `Last-Event-ID` header to resume | `404` |

### The run event stream

`GET /api/investigations/{run_id}/events` is a `text/event-stream`. Each event has an `id` (the Redis stream id), an
`event` name and a JSON `data` value. Events are published by the worker after each committed step, and by the API
when a run is queued, cancelled or requeued. Event names: `run.status` (`status`, and `error` when failed), `step.started`,
`step.finished` (index, kind, name, state, summary, handle, tokens, tool names), `approval.requested`,
`question.asked`, `report.ready`. Events are masked with the same rules as the stored steps.

The stream sends `retry: 2000` first, then `: keep-alive` comments every 15 seconds of silence, and closes when the
run reaches a terminal status (`completed`, `failed`, `cancelled`, `budget_exceeded`) or after 30 minutes. A browser
that reconnects sends `Last-Event-ID`, and the stream resumes after it. Events are kept for 24 hours and at most
2,000 per run. The stream is polled, not blocking, so an event can arrive up to 0.4 seconds late.

## Evaluation

| Method | Path | Who | What it does | Notable |
|---|---|---|---|---|
| GET | `/api/eval/results` | any signed-in | Reads the result files in `backend/eval/results/` and returns them grouped: `extraction`, `matching`, `results` (agent cases), `attacks`, and `unavailable` (runs in which the provider never answered, shown apart from scores) | Runs no model. The files are written by the scripts described in `docs/EVALUATION.md` |

## Settings: the organisation's model

Administrators only. The API key is write-only. Provider behaviour is in `docs/PROVIDERS.md`.

| Method | Path | Who | What it does | Body | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/settings/llm` | admin | The effective model settings: `provider`, `model`, `base_url`, `fallback_model`, the two prices, `api_key_set` (boolean), `source` (`organisation` when saved, otherwise `environment`) | | |
| PUT | `/api/settings/llm` | admin | Saves the organisation's model settings, replacing the environment for this organisation. Audited as `settings.llm_updated` without the key | `provider` (`openai_compat`, `anthropic`, `ollama`, `stub`), `model` (1–120), `base_url` (≤ 300), `api_key` (≤ 400; omit to keep the stored key, an empty string clears it), `fallback_model` (≤ 120), `price_input_per_mtok`, `price_output_per_mtok` (0–1000) | `422` `openai_compat` without a base URL; `422` a base URL that is not http(s), does not resolve, or points at a link-local, multicast or unspecified address, or at a private address when `ALLOW_PRIVATE_LLM_URLS=false` |
| POST | `/api/settings/llm/test` | admin | Sends one request, "Reply with the single word: ok", with 16 output tokens | none | Returns `200` in both cases: `{"ok": true, "model", "latency_ms", "reply"}` or `{"ok": false, "error"}`; `429` over six a minute |

## Usage and cost

| Method | Path | Who | What it does | Query | Notable errors |
|---|---|---|---|---|---|
| GET | `/api/usage` | clerk, approver, admin | Model calls, runs, tokens, estimated cost and average latency, grouped by `day`, `user` or `model`. Clerks and approvers see their own runs (`scope: "you"`); administrators see the organisation | `group` (`day` default, `user`, `model`), `from` and `to` (ISO timestamps; default the last 30 days; at most 366 days) | `422` the range is invalid. `budget` gives the daily organisation limit, what the organisation has spent today (administrators only), the per-run cost and step limits, and `cost_is_estimate: true` |

Counted: the Exception Agent's model calls (the steps of kind `model`) and the model calls made for document extraction
(stored in `model_spend`). Extraction calls have no user of their own, so under `group=user` they appear as
`(document extraction)` when the document came from the inbox. Both kinds count against the daily limits in `budget`;
once a limit is reached, extraction uses the rules alone and makes no model call.

---

## Summary of who may call what

| Role | Reads | Writes |
|---|---|---|
| viewer | Documents, invoices, orders, vendors, exceptions, runs, evaluation, matching settings | Nothing (its own session only) |
| clerk | As viewer, plus usage | Upload, invoice correction and matching, orders and receipts, start runs, answer and cancel own runs, approvals it requested (read) |
| approver | As clerk | Decide invoices, send back, resolve exceptions, accept recommendations, decide agent approvals |
| admin | Everything, including users, audit, model settings, inbox keys, all usage | Users, model settings, matching settings, vendors and bank accounts, reopen approved invoices, inbox keys |
