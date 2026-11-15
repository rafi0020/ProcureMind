# Scale: what was measured

Measured on one laptop on 2026-11-14, with the load generator on the same machine as the system under test. It
shows how ProcureMind behaves and where it slows down on this hardware; it is not a capacity claim for a
production deployment.

**Machine:** 16 logical cores (Intel i9-11900H), 31 GB RAM, Linux, Docker 29. PostgreSQL 16 (`shared_buffers`
512 MB), PgBouncer in transaction mode (pool of 40), Redis 7, nginx. One uvicorn process per API container,
20 database connections per process (10 plus 10 overflow).

**Read this first: the machine was shared.** Throughout every measurement an unrelated workload on the same
machine (another project's container, not mine to stop) used 5 to 7 of the 16 cores. All figures here ran beside
it. They are lower bounds, and the read-load figures in particular would be higher on a quiet machine.

**Data:** a separate database (`procuremind_load`), so the demo and the evaluation were never touched
(`backend/load/create_load_db.sh`, `docker-compose.load.yml`). `backend/load/bulk.py` wrote, in SQL through the
owner role under each organisation's row-level security, 100,000 invoices: per organisation 250 vendors, 50,000
purchase orders, each received and invoiced once, the invoice matched and approved; one in a hundred left waiting
for approval, one in two hundred an open exception, one in five hundred needing review. 92 seconds for both
organisations.

## 1. Documents through the pipeline

`backend/load/intake_load.py` sets up a vendor and a received purchase order for each invoice through the API,
uploads all the invoices at once, then reads the queue from the database every second until it is empty. Every
document goes the whole way: text (OCR for scans), classification, extraction (rules; the load stack's model is
the built-in script), the invoice record, matching and checks. Each worker process takes one document at a time;
OCR runs in a thread so the worker's other work is not blocked.

| Workers | Documents | Kind | Drained in | Per minute | Every document extracted |
|---|---|---|---|---|---|
| 1 | 40 | digital PDF | 10.9 s | 221 | yes |
| 1 | 24 | clean scan (OCR) | 89.8 s | 16 | yes |
| 4 | 80 | digital PDF | 6.7 s | 715 | yes |
| 4 | 48 | clean scan (OCR) | 43.6 s | 66 | yes |

What this says:

- **OCR is the cost.** A scan takes about 3.6 s of one core; a digital PDF about 0.2 s. A mailbox that is mostly
  scans needs workers in proportion: at 66 scans a minute, four workers clear about 4,000 an hour.
- **Workers scale out.** Four workers process scans 4.1 times as fast as one (each scan is independent CPU work)
  and digital PDFs 3.2 times as fast (at that speed the 1-second polling and the database round trips show).
- **When uploads outrun the workers, uploads are not slowed.** All 80 documents were accepted in 0.5 s; the queue
  then drained steadily. A queued document is a row with a lease, so a worker that dies mid-document loses
  nothing: the lease expires and another worker takes it (`tests/test_intake.py`).

## 2. Matching and checks at 100,000 invoices

`backend/load/match_load.py` creates a fresh received order and its invoice for one of the load vendors, then
times exactly what the pipeline does after extraction: the three-way match (find the order by its number, what
was received and what other invoices already billed, tolerances), storing it, and every check (duplicates by
number and by amount, bank account, price drift over 180 days of that vendor's lines, split under a limit),
opening an exception when one is due. One invoice in ten was priced 6 % over its order.

| Invoices | Exceptions opened | Match and checks, p50 | p95 | max |
|---|---|---|---|---|
| 300 | 38 | 36.6 ms | 59.9 ms | 84.3 ms |

About 20 small queries per invoice, each on an index; the time is round trips through PgBouncer, not work in
the database. Matching is not what limits throughput: OCR is, by two orders of magnitude.

## 3. Query plans at 100,000 invoices

`backend/load/explain.py` runs 20 request-path and pipeline queries as the application role (row-level security
applies) with `EXPLAIN ANALYZE`, and fails any that reads more than 10,000 rows sequentially.

**First run: one failure, and two slow lists.**

| Query | Before | After migration 0019 |
|---|---|---|
| purchase order list (newest 500) | 64 ms, sequential scan of 100,000 orders (both organisations, filtered by RLS) and a sort | 0.65 ms |
| invoice list, needs review | 14 ms, walked the organisation's whole index to find 100 rows | 0.44 ms |
| invoice list, exceptions | 25 ms, same | 0.32 ms |

Migration `0019_list_indexes` adds `purchase_orders (org_id, created_at DESC)` and
`invoices (org_id, status, created_at DESC)`. Every other query was already on an index: the stored match, the
billed quantity on an order line, duplicates by number and by amount, the split check, price drift, the agent's
vendor history, claiming a document; all under 4 ms warm.

## 4. One request at a time

`backend/load/latency.py`: one signed-in approver, each endpoint 40 times in a row, at 50,000 invoices in the
organisation.

| Request | p50 | p95 |
|---|---|---|
| invoice list, newest 100 | 22 ms | 363 ms (the first call after a restart) |
| invoice list, needs review | 26 ms | 38 ms |
| invoice with its lines and where each field was found | 21 ms | 42 ms |
| the stored match of an invoice | 37 ms | 61 ms |
| open exceptions (200) | 45 ms | 68 ms |
| my approvals (about 500 waiting) | **71 ms** (was 1,239 ms) | 106 ms |
| purchase order list (500) | 31 ms | 42 ms |
| purchase order with receipts | 40 ms | 55 ms |
| who am I | 7 ms | 11 ms |

**Found here:** "my approvals" took 1.2 seconds. For every waiting invoice it loaded the approval tiers, that
invoice's approvals and two separation-of-duties lookups: about 2,000 queries for 500 invoices. It now loads each
once for the whole queue (`app/approvals/chain.py::states`, `app/approvals/policy.py::conflicts`), and a test
holds the number of queries constant however many invoices wait
(`tests/test_ap_approvals.py::test_the_queue_costs_the_same_number_of_queries_however_many_invoices_wait`).

## 5. A thousand people at once

`backend/load/locustfile.py`: 1,000 simulated people (clerks, approvers, viewers of both organisations) sign in
and browse invoices, open invoices with their match, exceptions, their approvals, orders and vendors, with one
to three seconds between requests. Two minutes, 100 new people a second.

| API containers | Requests a second | p50 | p95 | p99 | Failed |
|---|---|---|---|---|---|
| 4 | 93 | 7.9 s | 23 s | 31 s | 26 (500: no database connection free within 30 s) |
| 8 | 176 | 2.3 s | 14 s | 21 s | 14 |
| 4, after the fix below | 94 | 6.0 s | 24 s | 34 s | 58, all 503 "busy, retry in 5 s" |

What this says:

- **1,000 active people is beyond four API processes on this machine.** A thousand people pausing one to three
  seconds ask for roughly 500 requests a second; four processes serve about 93. Each API process was at 100 %
  of a core throughout; PostgreSQL was not the limit (under one core).
- **It scales out.** Eight API containers served 1.9 times as much. The API holds no state (sessions and events
  are in PostgreSQL and Redis), so more containers behind nginx is the lever, then more cores.
- **Found here:** when a request waited 30 seconds for a database connection it got a 500, as if something were
  broken. Overload now answers 503 with `Retry-After: 5`
  (`tests/test_health.py::test_an_exhausted_connection_pool_is_a_503_to_retry_not_a_500`).
- Sign-in is the slowest request under load (p50 16 s) because password hashing is deliberately expensive; the
  load stack lowers the scrypt cost so it does not dominate, and it still queues behind the other requests.

For an accounts-payable team the realistic load is tens of people, not a thousand: at the single-request service
times above, four API containers serve a team of 50 working continuously with room to spare.

## How to repeat it

```bash
sh backend/load/create_load_db.sh
docker compose -f docker-compose.yml -f docker-compose.load.yml up -d --build --scale api=4 --scale worker=1
# then, in the test image pointed at procuremind_load: python -m load.bulk, load.explain, load.match_load,
# load.intake_load --quality digital|clean --n 40 --tag <tag>; and from backend/: uv run python -m load.latency,
# uv run locust -f load/locustfile.py --host http://localhost:3300 --headless -u 1000 -r 100 -t 2m
docker compose up -d --build --scale api=1 --scale worker=1    # back to the normal stack afterwards
```
