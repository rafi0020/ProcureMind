"""Service time per request with no concurrency: one signed-in person, each endpoint 40 times in a row.

    uv run python -m load.latency --host http://localhost:3300

Load tests show how latency grows under contention; this measures what one request costs at the load size
(50,000 invoices in the organisation), which depends much less on what else the machine is doing.
"""

import argparse
import random
import statistics
import time

import httpx


def main(args) -> None:
    with httpx.Client(base_url=args.host, timeout=60) as c:
        c.post(
            "/api/auth/login", json={"email": "approver@kestrel.demo", "password": "Approver#Demo2026"}
        ).raise_for_status()
        invoices = [i["id"] for i in c.get("/api/invoices?limit=100").json()["invoices"]]
        orders = [o["id"] for o in c.get("/api/orders").json()["orders"][:100]]
        cases = {
            "invoice list, newest 100": lambda: c.get("/api/invoices?limit=100"),
            "invoice list, needs review": lambda: c.get("/api/invoices?limit=100&status=needs_review"),
            "invoice with lines and provenance": lambda: c.get(f"/api/invoices/{random.choice(invoices)}"),
            "stored match of an invoice": lambda: c.get(f"/api/invoices/{random.choice(invoices)}/match"),
            "open exceptions (200)": lambda: c.get("/api/exceptions?status=open"),
            "my approvals (about 500 waiting)": lambda: c.get("/api/ap-approvals?mine=true"),
            "purchase order list (500)": lambda: c.get("/api/orders"),
            "purchase order with receipts": lambda: c.get(f"/api/orders/{random.choice(orders)}"),
            "who am I": lambda: c.get("/api/auth/me"),
        }
        for name, call in cases.items():
            times = []
            for _ in range(args.n):
                t = time.perf_counter()
                call().raise_for_status()
                times.append((time.perf_counter() - t) * 1000)
            times.sort()
            print(f"  {name:<36} p50 {statistics.median(times):7.1f} ms   p95 {times[int(len(times) * 0.95)]:7.1f} ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="http://localhost:3300")
    parser.add_argument("--n", type=int, default=40)
    main(parser.parse_args())
