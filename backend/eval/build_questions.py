"""Builds the agent's exception cases in their own organisation, Osprey Evaluation Ltd, and writes the question sets.

    python -m eval.build_questions

Thirty cases over the eight kinds of exception the matcher and the checks raise, each built through the real
matcher and checks (so the exception the agent reads is the one production would open), with variations in amount,
quantity and percentage. Every third case of a kind is held out (questions_test.json); the rest are for development.
Each case records the outcomes an AP lead would accept and, where one exists, the figure a good answer cites.
Running it again deletes the evaluation organisation and builds it afresh; the demo organisations are untouched.
"""

import asyncio
import json
import secrets
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from app.ap.numbers import normalise_number
from app.checks.run import open_exception
from app.db import dispose_engines, system_session, tenant_session
from app.matching.match import match_invoice, store_match
from app.models.ap import GoodsReceipt, Invoice, InvoiceLine, POLine, PurchaseOrder, ReceiptLine
from app.models.identity import Org, User
from app.models.vendors import Vendor, VendorBankAccount
from app.redis import close_redis
from app.security.passwords import hash_password

HERE = Path(__file__).parent
OSPREY = UUID("33333333-3333-4333-8333-333333333333")
DAY = date(2026, 11, 10)
CENT = D("0.01")
ITEMS = [("ST-210", "Stainless bolts M8"), ("CB-330", "Cable tray 3m"), ("GL-120", "Safety gloves, nitrile"), ("PA-450", "Pallet wrap"),
         ("CO-870", "Coffee beans 1kg"), ("TN-505", "Toner cartridge"), ("LB-610", "Lab beakers 250ml"), ("FL-720", "Floor cleaner 5L")]  # fmt: skip
VENDORS = ["Albany Fasteners", "Brook Electrical", "Cedar Safety", "Dunmore Packaging", "Elm Coffee Roasters", "Fairview Office", "Glen Scientific",
           "Hartley Hygiene", "Inver Metals", "Juno Logistics"]  # fmt: skip

# kind, variation parameters, acceptable outcomes, what a good answer cites
CASES = (
    [("price_over_tolerance", {"pct": p}, ["request_credit_note", "hold"]) for p in ("3", "4.5", "5", "7", "9", "12")]
    + [
        ("quantity_over_received", {"ordered": o, "received": r}, ["hold"])
        for o, r in ((10, 6), (40, 25), (12, 9), (100, 60))
    ]
    + [
        ("duplicate_number", {"printed": p}, ["reject_duplicate"])
        for p in ("INV-0042", "inv 42 ", "No. 42", "INV00042")
    ]
    + [("bank_changed", {"n": n}, ["hold", "escalate"]) for n in range(4)]
    + [("no_po", {"n": n}, ["hold", "request_corrected_invoice"]) for n in range(3)]
    + [("line_unmatched", {"n": n}, ["request_corrected_invoice", "hold"]) for n in range(3)]
    + [("currency_mismatch", {"currency": c}, ["request_corrected_invoice", "hold"]) for c in ("EUR", "GBP", "CAD")]
    + [("split_under_threshold", {"each": e}, ["escalate", "hold"]) for e in ("905.00", "930.00", "960.00")]
)


async def reset() -> tuple[UUID, UUID]:
    async with system_session() as db:
        await db.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": OSPREY})
        db.add(Org(id=OSPREY, name="Osprey Evaluation Ltd"))
        await db.flush()
        password = await asyncio.to_thread(hash_password, secrets.token_urlsafe(18))  # nobody signs in to it
        ids = []
        for role in ("clerk", "approver"):
            user = User(
                org_id=OSPREY, email=f"{role}@osprey.eval", name=f"Osprey {role}", role=role, password_hash=password
            )
            db.add(user)
            await db.flush()
            ids.append(user.id)
    return ids[0], ids[1]


class Builder:
    def __init__(self, db, clerk: UUID):
        self.db, self.clerk, self.n = db, clerk, 0

    async def vendor(self, name: str, account: str) -> Vendor:
        v = Vendor(org_id=OSPREY, name=name, tax_id=f"OS{abs(hash(name)) % 10**8:08d}", default_currency="USD")
        self.db.add(v)
        await self.db.flush()
        self.db.add(
            VendorBankAccount(
                org_id=OSPREY, vendor_id=v.id, iban_or_account=account, bank_name="Eval Bank", status="active"
            )
        )
        return v

    async def order(self, v: Vendor, number: str, item, qty, price, received) -> PurchaseOrder:
        po = PurchaseOrder(
            org_id=OSPREY,
            vendor_id=v.id,
            po_number=number,
            po_number_norm=normalise_number(number),
            currency="USD",
            created_by=self.clerk,
        )
        self.db.add(po)
        await self.db.flush()
        line = POLine(
            org_id=OSPREY,
            po_id=po.id,
            line_no=1,
            item_code=item[0],
            description=item[1],
            quantity=D(qty),
            unit_price=D(price),
        )
        self.db.add(line)
        await self.db.flush()
        if received:
            grn = GoodsReceipt(org_id=OSPREY, po_id=po.id, receipt_no=f"GRN-{number[3:]}", created_by=self.clerk)
            self.db.add(grn)
            await self.db.flush()
            self.db.add(ReceiptLine(org_id=OSPREY, receipt_id=grn.id, po_line_id=line.id, quantity=D(received)))
        return po

    async def invoice(
        self,
        v: Vendor,
        number: str,
        po: str | None,
        item,
        qty,
        price,
        *,
        account: str,
        currency="USD",
        when=DAY,
        status="ready",
    ) -> Invoice:
        subtotal = (D(qty) * D(price)).quantize(CENT)
        tax = (subtotal * D("0.10")).quantize(CENT)
        inv = Invoice(org_id=OSPREY, vendor_id=v.id, kind="invoice", invoice_number=number, invoice_number_norm=normalise_number(number), invoice_date=when,
                      due_date=when + timedelta(days=30), po_number=po, currency=currency, subtotal=subtotal, tax=tax, total=subtotal + tax,
                      bank_account=account, vendor_name_printed=v.name, status=status, entered_by=self.clerk)  # fmt: skip
        self.db.add(inv)
        await self.db.flush()
        self.db.add(
            InvoiceLine(
                org_id=OSPREY,
                invoice_id=inv.id,
                line_no=1,
                item_code=item[0],
                description=item[1],
                quantity=D(qty),
                unit_price=D(price),
                amount=subtotal,
            )
        )
        await self.db.flush()
        return inv

    async def settle(self, inv: Invoice, status: str | None = None) -> UUID | None:
        """Match and check, as the pipeline does; optionally leave an earlier invoice approved or waiting."""
        await store_match(self.db, inv.id, await match_invoice(self.db, inv.id))
        if status:
            inv.status = status
            await self.db.flush()
            return None
        return await open_exception(self.db, inv.id)

    async def case(self, kind: str, p: dict, index: int) -> tuple[UUID, dict]:
        """One exception of this kind; returns its id and what a good answer cites."""
        account = f"US{index:02d}EVAL{index:010d}"
        v = await self.vendor(f"{VENDORS[index % len(VENDORS)]} {kind.split('_')[0].title()} {index}", account)
        item = ITEMS[index % len(ITEMS)]
        po, price = f"PO-E{index:04d}", D("20.00") + index
        if kind == "price_over_tolerance":
            await self.order(v, po, item, 10, price, 10)
            charged = (price * (1 + D(p["pct"]) / 100)).quantize(CENT)
            inv = await self.invoice(v, f"INV-E{index:04d}", po, item, 10, charged, account=account)
            pct = ((charged - price) / price * 100).quantize(CENT)
            return await self.settle(inv), {"value": float(pct), "what": f"the {pct}% price variance"}
        if kind == "quantity_over_received":
            await self.order(v, po, item, p["ordered"], price, p["received"])
            inv = await self.invoice(v, f"INV-E{index:04d}", po, item, p["ordered"], price, account=account)
            return await self.settle(inv), {"value": float(p["received"]), "what": f"the {p['received']} received"}
        if kind == "duplicate_number":
            await self.order(v, po, item, 100, price, 100)
            first = await self.invoice(
                v, "INV-0042", po, item, 10, price, account=account, when=DAY - timedelta(days=12)
            )
            await self.settle(first, "approved")
            inv = await self.invoice(v, p["printed"], po, item, 10, price, account=account)
            return await self.settle(inv), {
                "value": float(inv.total),
                "what": f"the total {inv.total} already invoiced",
            }
        if kind == "bank_changed":
            await self.order(v, po, item, 8, price, 8)
            inv = await self.invoice(
                v, f"INV-E{index:04d}", po, item, 8, price, account=f"GB{index:02d}NEWB{index:014d}"
            )
            return await self.settle(inv), None
        if kind == "no_po":
            inv = await self.invoice(v, f"INV-E{index:04d}", f"PO-X{index:04d}", item, 3, price, account=account)
            return await self.settle(inv), None
        if kind == "line_unmatched":
            await self.order(v, po, item, 10, price, 10)
            other = ITEMS[(index + 3) % len(ITEMS)]
            inv = await self.invoice(v, f"INV-E{index:04d}", po, other, 10, price, account=account)
            return await self.settle(inv), None
        if kind == "currency_mismatch":
            await self.order(v, po, item, 10, price, 10)
            inv = await self.invoice(
                v, f"INV-E{index:04d}", po, item, 10, price, account=account, currency=p["currency"]
            )
            return await self.settle(inv), None
        if kind == "split_under_threshold":
            each = D(p["each"])
            unit = (each / D("1.10") / 10).quantize(CENT)  # ten units, plus 10% tax, just under the 1,000 limit
            await self.order(v, po, item, 30, unit, 30)
            for k in range(2):
                earlier = await self.invoice(
                    v,
                    f"INV-E{index:04d}-{k + 1}",
                    po,
                    item,
                    10,
                    unit,
                    account=account,
                    when=DAY - timedelta(days=4 - 2 * k),
                )
                await self.settle(earlier, "pending_approval")
            inv = await self.invoice(v, f"INV-E{index:04d}-3", po, item, 10, unit, account=account)
            return await self.settle(inv), {"value": 1000.0, "what": "the 1,000 approval limit"}
        raise ValueError(kind)


async def build() -> tuple[list[dict], list[dict]]:
    clerk, _ = await reset()
    dev, test = [], []
    seen: dict[str, int] = {}
    async with tenant_session(OSPREY) as db:
        b = Builder(db, clerk)
        for index, (kind, params, acceptable) in enumerate(CASES):
            exception_id, cite = await b.case(kind, params, index)
            assert exception_id is not None, (kind, params)
            reasons = (
                await db.execute(text("SELECT reasons, findings FROM exceptions WHERE id = :e"), {"e": exception_id})
            ).one()
            codes = {r["code"] for r in reasons.reasons} | {f["code"] for f in reasons.findings}
            assert kind in codes, (kind, codes)  # the case raised the exception it was built for
            seen[kind] = seen.get(kind, 0) + 1
            question = {"id": f"{kind}-{seen[kind]}", "kind": kind, "exception_id": str(exception_id), "acceptable": acceptable, "cite": cite,
                        "codes": sorted(codes)}  # fmt: skip
            (test if seen[kind] % 3 == 0 else dev).append(question)
    return dev, test


async def main() -> None:
    dev, test = await build()
    (HERE / "questions_dev.json").write_text(json.dumps(dev, indent=1))
    (HERE / "questions_test.json").write_text(json.dumps(test, indent=1))
    print(f"{len(dev)} development cases, {len(test)} held out, in Osprey Evaluation Ltd")
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
