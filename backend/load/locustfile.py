"""Read load: many signed-in people working through invoices, exceptions, approvals and orders.

    cd backend && uv run locust -f load/locustfile.py --host http://localhost:3300 --headless \
        -u 1000 -r 100 -t 2m --csv load/results/read-u1000

Each simulated person signs in once (demo accounts in the load database: 50,000 invoices per organisation), then
browses with one to three seconds of think time. Start the stack with docker-compose.load.yml so sign-in limits
do not throttle the test itself.
"""

import random

from locust import FastHttpUser, between, task

ACCOUNTS = [("clerk@kestrel.demo", "Clerk#Demo2026"), ("approver@kestrel.demo", "Approver#Demo2026"), ("viewer@kestrel.demo", "Viewer#Demo2026"),
            ("clerk@ridgeway.demo", "Clerk#Demo2026"), ("approver@ridgeway.demo", "Approver#Demo2026")]  # fmt: skip
STATUSES = ["", "", "needs_review", "exception", "pending_approval", "approved"]


class Reader(FastHttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        email, password = random.choice(ACCOUNTS)
        self.role = email.split("@")[0]
        r = self.client.post(
            "/api/auth/login", json={"email": email, "password": password}, name="POST /api/auth/login"
        )
        r.raise_for_status()
        listed = self.client.get("/api/invoices?limit=100", name="GET /api/invoices").json()["invoices"]
        self.invoices = [i["id"] for i in listed]
        self.orders = [o["id"] for o in self.client.get("/api/orders", name="GET /api/orders").json()["orders"][:100]]

    @task(5)
    def invoice_list(self) -> None:
        status = random.choice(STATUSES)
        self.client.get(f"/api/invoices?limit=100{'&status=' + status if status else ''}", name="GET /api/invoices")

    @task(4)
    def open_invoice(self) -> None:
        if self.invoices:
            i = random.choice(self.invoices)
            self.client.get(f"/api/invoices/{i}", name="GET /api/invoices/{id}")
            self.client.get(f"/api/invoices/{i}/match", name="GET /api/invoices/{id}/match")

    @task(3)
    def exceptions(self) -> None:
        self.client.get("/api/exceptions?status=open", name="GET /api/exceptions")

    @task(2)
    def approvals(self) -> None:
        if self.role != "viewer":
            self.client.get("/api/ap-approvals?mine=true", name="GET /api/ap-approvals")

    @task(2)
    def order(self) -> None:
        if self.orders:
            self.client.get(f"/api/orders/{random.choice(self.orders)}", name="GET /api/orders/{id}")

    @task(1)
    def vendors(self) -> None:
        self.client.get("/api/vendors", name="GET /api/vendors")

    @task(2)
    def me(self) -> None:
        self.client.get("/api/auth/me", name="GET /api/auth/me")
