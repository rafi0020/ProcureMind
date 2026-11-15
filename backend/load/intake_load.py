"""Documents processed per minute: uploads N invoices at once through the API, then watches the queue drain.

    python -m load.intake_load --quality digital --n 40 --tag d1      # with the load stack up, N workers running

Each invoice has its vendor and a received purchase order set up first through the API, so every document goes
the whole way: text (or OCR for scans), classification, extraction, the invoice record, matching and checks.
Extraction runs the rules only (the load stack's model is the built-in script). The queue is read from the
database every second until no document of this run is queued or processing.
"""

import argparse
import json
import time
from datetime import date, timedelta
from decimal import Decimal as D

import httpx
import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from seed.demo_users import KESTREL
from seed.documents import DocSpec, Line
from seed.generate import _render

BASE = "http://web"
ITEMS = [
    ("LD-100", "Copier paper A4", "4.20"),
    ("LD-200", "Nitrile gloves", "9.10"),
    ("LD-300", "Toner cartridge", "61.00"),
    ("LD-400", "Pallet wrap", "15.30"),
]


def session(email: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=120)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def prepare(args) -> list[tuple[str, bytes, str]]:
    admin, clerk = session("admin@kestrel.demo", "Admin#Demo2026"), session("clerk@kestrel.demo", "Clerk#Demo2026")
    vendors = {v["name"]: v["id"] for v in admin.get("/api/vendors").json()["vendors"]}
    files = []
    for n in range(args.n):
        name = f"Intake Vendor {n % 10:02d}"
        if name not in vendors:
            vendors[name] = (
                admin.post("/api/vendors", json={"name": name, "default_currency": "USD"})
                .raise_for_status()
                .json()["id"]
            )
        code, desc, price = ITEMS[n % len(ITEMS)]
        po_number, qty = f"IPO-{args.tag}-{n:04d}", 5 + n % 20
        po = clerk.post("/api/orders", json={"vendor_id": vendors[name], "po_number": po_number, "currency": "USD",
                                              "lines": [{"item_code": code, "description": desc, "quantity": str(qty), "unit_price": price}]}).raise_for_status().json()  # fmt: skip
        clerk.post(
            f"/api/orders/{po['id']}/receipts",
            json={
                "receipt_no": f"GRN-{po_number}",
                "lines": [{"po_line_id": po["lines"][0]["id"], "quantity": str(qty)}],
            },
        ).raise_for_status()
        day = date(2026, 11, 10)
        spec = DocSpec("invoice", f"IINV-{args.tag}-{n:04d}", day, name, "Kestrel Foods", "USD", [Line(code, desc, D(qty), D(price))], po_number=po_number,
                       due_date=day + timedelta(days=30), bank_account=f"GB00INTK{n:014d}", layout=n % 4)  # fmt: skip
        ext, body = _render(spec, args.quality, seed=n)
        files.append((f"{args.tag}-{n:04d}.{ext}", body, "application/pdf" if ext == "pdf" else "image/jpeg"))
    return files


def counts(tag: str) -> dict[str, int]:
    with psycopg.connect(libpq_url(get_settings().database_url_owner)) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.org_id', %s, true)", (str(KESTREL),))
        return dict(
            conn.execute(
                "SELECT status, count(*) FROM documents WHERE filename LIKE %s GROUP BY 1", (f"{tag}-%",)
            ).fetchall()
        )


def main(args) -> None:
    files = prepare(args)
    clerk = session("clerk@kestrel.demo", "Clerk#Demo2026")
    started = time.perf_counter()
    for i in range(0, len(files), 10):
        clerk.post("/api/documents", files=[("files", f) for f in files[i : i + 10]]).raise_for_status()
    uploaded = time.perf_counter() - started
    timeline, deepest = [], 0
    while True:
        c = counts(args.tag)
        waiting = c.get("queued", 0) + c.get("processing", 0)
        deepest = max(deepest, c.get("queued", 0))
        timeline.append((round(time.perf_counter() - started, 1), c.get("queued", 0), c.get("processing", 0)))
        if waiting == 0:
            break
        time.sleep(1)
    total = time.perf_counter() - started
    c = counts(args.tag)
    result = {"tag": args.tag, "quality": args.quality, "documents": args.n, "upload_s": round(uploaded, 1), "drained_s": round(total, 1),
              "per_minute": round(args.n / total * 60, 1), "deepest_queue": deepest, "final": c, "timeline": timeline[:: max(1, len(timeline) // 12)]}  # fmt: skip
    print(
        json.dumps({k: v for k, v in result.items() if k != "timeline"}),
        "\n timeline (s, queued, processing):",
        result["timeline"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quality", choices=["digital", "clean", "medium", "photo"], default="digital")
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--tag", required=True)
    main(parser.parse_args())
