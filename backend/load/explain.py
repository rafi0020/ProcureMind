"""Checks the query plans of the request-path and pipeline queries at the load size (100,000 invoices), and times them.

    python -m load.explain            # with DATABASE_URL / DATABASE_URL_OWNER pointing at procuremind_load

No such query may read a large table end to end: a sequential scan over more than 10,000 rows fails the check
and is named. Each query runs three times as the application role (row-level security applies); the first and the
last (warm) times are reported.
"""

import json
import sys

import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from app.intake.queue import CLAIM
from seed.demo_users import KESTREL

BIG = 10_000
ORG = str(KESTREL)
OPEN = ["open", "investigating", "recommended", "escalated"]
ACCEPTED = ["matched", "pending_approval", "approved"]
LIST = "SELECT * FROM invoices"


def queries(s: dict) -> dict:
    return {
        "invoice list, newest 100": (LIST + " ORDER BY created_at DESC LIMIT 100", {}),
        "invoice list, needs review": (LIST + " WHERE status = 'needs_review' ORDER BY created_at DESC LIMIT 100", {}),
        "invoice list, exceptions": (LIST + " WHERE status = 'exception' ORDER BY created_at DESC LIMIT 100", {}),
        "invoice with its lines": ("SELECT * FROM invoice_lines WHERE invoice_id = %(i)s ORDER BY line_no", s),
        "invoice from a document (pipeline)": ("SELECT id FROM invoices WHERE document_id = %(d)s", s),
        "open exceptions list": ("SELECT e.*, i.invoice_number, v.name FROM exceptions e JOIN invoices i ON i.id = e.invoice_id "
                                 "LEFT JOIN vendors v ON v.id = i.vendor_id WHERE e.status = ANY(%(open)s) ORDER BY e.created_at DESC LIMIT 500", {"open": OPEN}),
        "approval queue": ("SELECT * FROM invoices WHERE status = 'pending_approval' ORDER BY escalated_at IS NULL, pending_since", {}),
        "purchase order list": ("SELECT * FROM purchase_orders ORDER BY created_at DESC LIMIT 500", {}),
        "purchase order by number": ("SELECT * FROM purchase_orders WHERE po_number_norm = %(n)s", s),
        "received per order line": ("SELECT po_line_id, sum(quantity) FROM receipt_lines WHERE po_line_id = ANY(%(lines)s) GROUP BY 1", s),
        "already billed on an order line": (
            "SELECT coalesce(sum(CASE WHEN i.kind = 'credit_note' THEN -ml.quantity ELSE ml.quantity END), 0) FROM match_lines ml "
            "JOIN matches m ON m.id = ml.match_id JOIN invoices i ON i.id = m.invoice_id WHERE ml.po_line_id = %(line)s AND i.id <> %(i)s "
            "AND i.status = ANY(%(acc)s)", s | {"acc": ACCEPTED}),
        "duplicate by number": ("SELECT * FROM invoices WHERE vendor_id = %(v)s AND kind = 'invoice' AND id <> %(i)s AND status <> 'rejected' "
                                "AND invoice_number_norm = %(n_inv)s", s),
        "duplicate by amount and date": (
            "SELECT * FROM invoices WHERE vendor_id = %(v)s AND kind = 'invoice' AND id <> %(i)s AND status <> 'rejected' AND total = %(t)s "
            "AND invoice_date BETWEEN %(day)s::date - 30 AND %(day)s::date + 30 AND invoice_number_norm IS DISTINCT FROM %(n_inv)s", s),
        "split under a limit": ("SELECT invoice_number, total FROM invoices WHERE vendor_id = %(v)s AND kind = 'invoice' AND status <> 'rejected' "
                                "AND total >= 900 AND total < 1000 AND invoice_date BETWEEN %(day)s::date - 7 AND %(day)s::date", s),
        "price drift": ("SELECT max(il.unit_price) FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id WHERE i.vendor_id = %(v)s "
                        "AND i.id <> %(i)s AND i.status = ANY(%(acc)s) AND il.item_code = %(item)s AND i.invoice_date BETWEEN %(day)s::date - 180 AND %(day)s::date",
                        s | {"acc": ACCEPTED}),
        "agent: vendor history, invoices": ("SELECT invoice_number, kind, invoice_date, total, status FROM invoices WHERE vendor_id = %(v)s "
                                            "AND invoice_date >= current_date - interval '365 days' ORDER BY invoice_date DESC LIMIT 50", s),
        "agent: vendor history, prices": (
            "SELECT il.item_code, min(il.unit_price), max(il.unit_price), count(*) FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id "
            "WHERE i.vendor_id = %(v)s AND i.status IN ('approved', 'pending_approval', 'matched') AND il.item_code IS NOT NULL "
            "AND i.invoice_date >= current_date - interval '365 days' GROUP BY il.item_code", s),
        "agent: accounts printed on invoices": ("SELECT bank_account, count(*) FROM invoices WHERE vendor_id = %(v)s AND bank_account IS NOT NULL GROUP BY 1", s),
        "agent: similar exceptions": ("SELECT e.resolution, e.note FROM exceptions e JOIN invoices i ON i.id = e.invoice_id WHERE e.status = 'resolved' "
                                      "AND (e.reasons @> %(c)s::jsonb OR e.findings @> %(c)s::jsonb) ORDER BY e.resolved_at DESC LIMIT 5",
                                      {"c": json.dumps([{"code": "price_over_tolerance"}])}),
        "claim a document": (str(CLAIM).replace(":worker", "%(worker)s").replace(":lease", "%(lease)s"), {"worker": "explain", "lease": 30}),
    }  # fmt: skip


def seq_scans(node: dict, found: list) -> None:
    rows = node.get("Actual Rows", 0) * node.get("Actual Loops", 1) + node.get("Rows Removed by Filter", 0)
    if node.get("Node Type") == "Seq Scan" and rows > BIG:
        found.append(f"{node['Relation Name']} ({rows:,} rows read)")
    for child in node.get("Plans", []):
        seq_scans(child, found)


def sample() -> dict:
    with psycopg.connect(libpq_url(get_settings().database_url_owner)) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("invoices", "invoice_lines", "purchase_orders", "match_lines")
        }  # noqa: S608 - fixed names
        i, v, n_inv, t, day, po = conn.execute(
            "SELECT i.id, i.vendor_id, i.invoice_number_norm, i.total, i.invoice_date, p.id FROM invoices i JOIN purchase_orders p ON p.po_number = i.po_number "
            "WHERE i.invoice_number = 'LINV-20000'").fetchone()  # fmt: skip
        line, item = conn.execute("SELECT id, item_code FROM po_lines WHERE po_id = %s", (po,)).fetchone()
        n = conn.execute("SELECT po_number_norm FROM purchase_orders WHERE id = %s", (po,)).fetchone()[0]
        d = conn.execute("SELECT gen_random_uuid()").fetchone()[0]
    return {
        "counts": counts,
        "i": i,
        "v": v,
        "n_inv": n_inv,
        "t": t,
        "day": day,
        "line": line,
        "lines": [line],
        "item": item,
        "n": n,
        "d": d,
    }


def main() -> int:
    s = sample()
    print("Kestrel's rows: " + ", ".join(f"{k} {v:,}" for k, v in s.pop("counts").items()))
    failures, report = 0, []
    with psycopg.connect(libpq_url(get_settings().database_url)) as conn:  # the application role: RLS applies
        for name, (sql, params) in queries(s).items():
            times = []
            for _ in range(3):
                with conn.transaction(force_rollback=True):  # the claim is an UPDATE; never keep its effect
                    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
                    if name.startswith("claim"):
                        conn.execute("SELECT set_config('app.system', 'on', true)")
                    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0][0]
                times.append(plan["Execution Time"])
            found: list[str] = []
            seq_scans(plan["Plan"], found)
            verdict = "SEQUENTIAL SCAN on " + ", ".join(found) if found else "ok"
            failures += bool(found)
            print(f"  {name:<40}{times[0]:>9.2f} ms first{times[-1]:>9.2f} ms warm  {verdict}")
            report.append(
                {"query": name, "first_ms": round(times[0], 2), "warm_ms": round(times[-1], 2), "verdict": verdict}
            )
    if "--json" in sys.argv:
        print(json.dumps({"failures": failures, "queries": report}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
