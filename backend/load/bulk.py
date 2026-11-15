"""Fills the load database with 100,000 approved invoices and their history, in SQL, so the request paths and the
matcher can be measured at that size.

    DATABASE_URL=...procuremind_load DATABASE_URL_OWNER=...procuremind_load python -m load.bulk --per-org 50000

Per organisation (Kestrel and Ridgeway, with their demo accounts): 250 vendors, N purchase orders of one line,
each fully received and invoiced once, the invoice matched and approved. One invoice in a hundred is left waiting
for approval, one in two hundred is an open exception and one in five hundred needs review, so the lists people
filter have something realistic to find. Written through the owner role under each organisation's row-level
security, the same as the application.
"""

import argparse
import asyncio
import time

import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from seed.demo_users import KESTREL, RIDGEWAY, ensure_demo_users

VENDORS = 250
STEPS = [
    ("vendors", """INSERT INTO vendors (org_id, name, tax_id, default_currency)
        SELECT %(o)s, 'Load Vendor ' || lpad(g::text, 4, '0'), 'LT' || lpad(g::text, 8, '0'), %(cur)s FROM generate_series(1, %(nv)s) g"""),
    ("index vendors", """CREATE TEMP TABLE lv ON COMMIT DROP AS
        SELECT id, row_number() OVER (ORDER BY name) AS n FROM vendors WHERE name LIKE 'Load Vendor %%'"""),
    ("purchase orders", """INSERT INTO purchase_orders (org_id, vendor_id, po_number, po_number_norm, currency, status, created_at)
        SELECT %(o)s, lv.id, 'LPO-' || g, 'lpo' || g, %(cur)s, 'open', now() - make_interval(days => g %% 365)
        FROM generate_series(1, %(n)s) g JOIN lv ON lv.n = 1 + g %% %(nv)s"""),
    ("index orders", """CREATE TEMP TABLE lp ON COMMIT DROP AS
        SELECT id, vendor_id, substring(po_number FROM 5)::int AS g FROM purchase_orders WHERE po_number LIKE 'LPO-%%'"""),
    ("order lines", """INSERT INTO po_lines (org_id, po_id, line_no, item_code, description, quantity, unit_price)
        SELECT %(o)s, id, 1, 'IT-' || (g %% 500), 'Load item ' || (g %% 500), 10, 5 + g %% 50 FROM lp"""),
    ("receipts", "INSERT INTO goods_receipts (org_id, po_id, receipt_no) SELECT %(o)s, id, 'LGRN-' || g FROM lp"),
    ("receipt lines", """INSERT INTO receipt_lines (org_id, receipt_id, po_line_id, quantity)
        SELECT %(o)s, gr.id, pl.id, 10 FROM goods_receipts gr JOIN po_lines pl ON pl.po_id = gr.po_id WHERE gr.receipt_no LIKE 'LGRN-%%'"""),
    ("invoices", """INSERT INTO invoices (org_id, vendor_id, kind, invoice_number, invoice_number_norm, invoice_date, due_date, po_number,
                                          currency, subtotal, tax, total, status, created_at, pending_since)
        SELECT %(o)s, vendor_id, 'invoice', 'LINV-' || g, 'linv' || g, (now() - make_interval(days => g %% 365))::date,
               (now() - make_interval(days => g %% 365) + interval '30 days')::date, 'LPO-' || g, %(cur)s,
               10 * (5 + g %% 50), 5 + g %% 50, 11 * (5 + g %% 50),
               CASE WHEN g %% 500 = 0 THEN 'needs_review' WHEN g %% 200 = 0 THEN 'exception' WHEN g %% 100 = 0 THEN 'pending_approval' ELSE 'approved' END,
               now() - make_interval(days => g %% 365), CASE WHEN g %% 100 = 0 THEN now() - make_interval(hours => g %% 72) END
        FROM lp"""),
    ("invoice lines", """INSERT INTO invoice_lines (org_id, invoice_id, line_no, item_code, description, quantity, unit_price, amount)
        SELECT %(o)s, i.id, 1, pl.item_code, pl.description, 10, pl.unit_price, 10 * pl.unit_price
        FROM invoices i JOIN purchase_orders p ON p.po_number = i.po_number JOIN po_lines pl ON pl.po_id = p.id WHERE i.invoice_number LIKE 'LINV-%%'"""),
    ("matches", """INSERT INTO matches (org_id, invoice_id, po_id, status)
        SELECT %(o)s, i.id, p.id, CASE WHEN i.status = 'exception' THEN 'exception' ELSE 'matched' END
        FROM invoices i JOIN purchase_orders p ON p.po_number = i.po_number WHERE i.invoice_number LIKE 'LINV-%%' AND i.status <> 'needs_review'"""),
    ("match lines", """INSERT INTO match_lines (org_id, match_id, invoice_line_id, po_line_id, quantity, unit_price)
        SELECT %(o)s, m.id, il.id, pl.id, 10, pl.unit_price FROM matches m JOIN invoice_lines il ON il.invoice_id = m.invoice_id
        JOIN po_lines pl ON pl.po_id = m.po_id"""),
    ("exceptions", """INSERT INTO exceptions (org_id, invoice_id, reasons)
        SELECT %(o)s, id, '[{"code": "price_over_tolerance", "detail": "Load data."}]'::jsonb FROM invoices WHERE status = 'exception'"""),
]  # fmt: skip


def fill(org, currency: str, n: int) -> None:
    with psycopg.connect(libpq_url(get_settings().database_url_owner)) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))
        if conn.execute("SELECT EXISTS (SELECT 1 FROM purchase_orders WHERE po_number LIKE 'LPO-%%')").fetchone()[0]:
            print(f"  {org}: already filled")
            return
        for name, sql in STEPS:
            started = time.perf_counter()
            done = conn.execute(sql, {"o": org, "cur": currency, "n": n, "nv": VENDORS})
            print(f"  {org} {name}: {done.rowcount:,} rows in {time.perf_counter() - started:.1f} s", flush=True)
            conn.execute("ANALYZE")  # fresh tables have no statistics; without them the joins below nest-loop
        conn.execute("ANALYZE")


def main(args) -> None:
    asyncio.run(ensure_demo_users())
    for org, currency in ((KESTREL, "USD"), (RIDGEWAY, "GBP")):
        fill(org, currency, args.per_org)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-org", type=int, default=50_000)
    main(parser.parse_args())
