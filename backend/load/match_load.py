"""Time to match and check one invoice when the organisation already has 50,000 (100,000 in the database).

    python -m load.match_load --n 300      # against procuremind_load, after load.bulk

Each round creates a fresh purchase order (received in full) and its invoice for one of the load vendors, then
times exactly what the pipeline does after extraction: the three-way match (find the order by number, received
and already-billed quantities, tolerances), storing it, and the checks (duplicates by number and by amount,
bank account, price drift against 180 days of that vendor's lines, split under a limit), opening an exception
when one is due. One invoice in ten is priced 6 % over the order, so some rounds open an exception.
"""

import argparse
import asyncio
import random
import statistics
import time
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.ap.numbers import normalise_number
from app.checks.run import open_exception
from app.db import dispose_engines, tenant_session
from app.matching.match import match_invoice, store_match
from app.models.ap import GoodsReceipt, Invoice, InvoiceLine, POLine, PurchaseOrder, ReceiptLine
from app.models.vendors import Vendor
from app.redis import close_redis
from seed.demo_users import KESTREL


async def one(k: int, vendors: list) -> tuple[float, bool]:
    async with tenant_session(KESTREL) as db:
        vendor = random.choice(vendors)
        number = f"MPO-{k}-{random.randrange(10**9)}"
        po = PurchaseOrder(
            org_id=KESTREL, vendor_id=vendor, po_number=number, po_number_norm=normalise_number(number), currency="USD"
        )
        db.add(po)
        await db.flush()
        item, price = f"IT-{random.randrange(500)}", D(5 + random.randrange(50))
        line = POLine(
            org_id=KESTREL,
            po_id=po.id,
            line_no=1,
            item_code=item,
            description="Load item",
            quantity=D(10),
            unit_price=price,
        )
        db.add(line)
        grn = GoodsReceipt(org_id=KESTREL, po_id=po.id, receipt_no=f"M-{number}")
        db.add(grn)
        await db.flush()
        db.add(ReceiptLine(org_id=KESTREL, receipt_id=grn.id, po_line_id=line.id, quantity=D(10)))
        charged = price * D("1.06") if k % 10 == 0 else price
        sub = (charged * 10).quantize(D("0.01"))
        inv = Invoice(org_id=KESTREL, vendor_id=vendor, kind="invoice", invoice_number=f"MINV-{number}", invoice_number_norm=normalise_number(f"MINV-{number}"),
                      invoice_date=date(2026, 11, 10), po_number=number, currency="USD", subtotal=sub, tax=D(0), total=sub, status="ready")  # fmt: skip
        db.add(inv)
        await db.flush()
        db.add(
            InvoiceLine(
                org_id=KESTREL,
                invoice_id=inv.id,
                line_no=1,
                item_code=item,
                description="Load item",
                quantity=D(10),
                unit_price=charged,
                amount=sub,
            )
        )
        await db.flush()
        started = time.perf_counter()
        await store_match(db, inv.id, await match_invoice(db, inv.id))
        opened = await open_exception(db, inv.id)
        return (time.perf_counter() - started) * 1000, opened is not None


async def main(args) -> None:
    async with tenant_session(KESTREL) as db:
        vendors = list((await db.execute(select(Vendor.id).where(Vendor.name.like("Load Vendor %")))).scalars())
    for k in range(20):  # warm the connection pool and caches; not counted
        await one(-k - 1, vendors)
    times, exceptions = [], 0
    started = time.perf_counter()
    for k in range(args.n):
        ms, opened = await one(k, vendors)
        times.append(ms)
        exceptions += opened
    wall = time.perf_counter() - started
    times.sort()
    print(f"{args.n} invoices, {exceptions} exceptions opened; match + checks per invoice: p50 {statistics.median(times):.1f} ms, "
          f"p95 {times[int(len(times) * 0.95)]:.1f} ms, max {times[-1]:.1f} ms; whole round incl. creating the records {wall / args.n * 1000:.1f} ms")  # fmt: skip
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=300)
    asyncio.run(main(parser.parse_args()))
