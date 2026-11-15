"""Documents in: by upload or by the inbox endpoint, checked, stored once, and queued for a worker."""

import base64

import pytest
from sqlalchemy import text

from app.db import system_session
from tests.conftest import rows
from tests.files import pdf, png, xlsx


async def upload(client, *files):
    return await client.post("/api/documents", files=[("files", f) for f in files])


@pytest.mark.parametrize(
    ("name", "make", "kind"),
    [("a.pdf", pdf, "application/pdf"), ("b.png", png, "image/png"), ("c.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
     ("d.csv", lambda: b"po,item,qty\nPO-1,Widget,3\n", "text/csv")],
)  # fmt: skip
async def test_each_accepted_type_is_stored_once_and_queued(team, name, make, kind):
    data = make()
    r = await upload(team.clerk, (name, data, "application/octet-stream"))
    assert r.status_code == 201, r.text
    doc = r.json()["documents"][0]
    assert doc["status"] == "queued" and doc["content_type"] == kind and doc["duplicate_of"] is None
    got = await team.viewer.get(f"/api/documents/{doc['id']}/file")
    assert got.status_code == 200 and got.content == data


async def test_wrong_type_too_big_and_too_many_pages_are_refused(team):
    assert (await upload(team.clerk, ("x.pdf", b"MZ\x90\x00 not a pdf", "application/pdf"))).status_code == 415
    assert (
        await upload(team.clerk, ("big.pdf", b"%PDF-1.7\n" + b"0" * (20 * 1024 * 1024 + 1), "application/pdf"))
    ).status_code == 413
    assert (await upload(team.clerk, ("long.pdf", pdf(pages=201), "application/pdf"))).status_code == 422
    assert (await upload(team.clerk, *[(f"{i}.png", png(f"n{i}"), "image/png") for i in range(11)])).status_code == 422
    assert (await rows(team.org_id, "SELECT count(*) FROM documents"))[0][0] == 0


async def test_the_same_file_twice_is_one_document(team):
    data = pdf("INVOICE INV-7")
    first = (await upload(team.clerk, ("a.pdf", data, "application/pdf"))).json()["documents"][0]
    second = (await upload(team.clerk, ("renamed.pdf", data, "application/pdf"))).json()["documents"][0]
    assert second["id"] == first["id"] and second["duplicate_of"] == first["id"]
    assert (await rows(team.org_id, "SELECT count(*) FROM documents"))[0][0] == 1


async def test_viewers_cannot_upload_and_other_organisations_cannot_read(team, other_team):
    assert (await upload(team.viewer, ("a.pdf", pdf(), "application/pdf"))).status_code == 403
    doc = (await upload(team.clerk, ("a.pdf", pdf(), "application/pdf"))).json()["documents"][0]
    assert (await other_team.admin.get(f"/api/documents/{doc['id']}")).status_code == 404
    assert (await other_team.admin.get(f"/api/documents/{doc['id']}/file")).status_code == 404


async def test_the_inbox_takes_attachments_with_a_key_and_refuses_revoked_keys(client, team):
    key = (await team.admin.post("/api/inbox-keys", json={"name": "ap mailbox"})).json()
    assert key["key"].startswith("ik_")
    body = {"from": "billing@acme.example", "subject": "Invoice 1001",
            "attachments": [{"filename": "inv.pdf", "content_base64": base64.b64encode(pdf()).decode()}]}  # fmt: skip
    r = await client.post("/v1/inbox", json=body, headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 200, r.text
    doc = r.json()["documents"][0]
    assert (await team.viewer.get(f"/api/documents/{doc['id']}")).json()["source"] == "inbox"
    assert (await team.clerk.post("/api/inbox-keys", json={"name": "x"})).status_code == 403
    await team.admin.delete(f"/api/inbox-keys/{key['id']}")
    assert (
        await client.post("/v1/inbox", json=body, headers={"Authorization": f"Bearer {key['key']}"})
    ).status_code == 401
    assert (await client.post("/v1/inbox", json=body)).status_code == 401


async def test_each_document_is_claimed_by_one_worker_and_reclaimed_after_its_lease(team):
    from app.intake.queue import claim_document

    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status = 'queued'"))
    doc = (await upload(team.clerk, ("a.pdf", pdf("lease"), "application/pdf"))).json()["documents"][0]
    first = await claim_document("worker-a")
    assert str(first.id) == doc["id"] and await claim_document("worker-b") is None
    async with system_session() as db:  # worker-a dies: its lease runs out
        await db.execute(
            text("UPDATE documents SET lease_expires_at = now() - interval '1 second' WHERE id = :d"), {"d": doc["id"]}
        )
    again = await claim_document("worker-b")
    assert str(again.id) == doc["id"] and again.attempts == 2


async def test_a_document_that_keeps_failing_ends_dead_with_the_reason(team, monkeypatch):
    from app.intake import queue

    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    doc = (await upload(team.clerk, ("a.pdf", pdf("broken"), "application/pdf"))).json()["documents"][0]

    async def broken(document_id):
        raise ValueError("cannot read this file")

    monkeypatch.setattr(queue, "PIPELINE", [broken])
    for _ in range(queue.MAX_ATTEMPTS):
        await queue.process_pending("test", backoff_s=0)
    status, error, attempts = (
        await rows(team.org_id, "SELECT status, error, attempts FROM documents WHERE id = :d", d=doc["id"])
    )[0]
    assert status == "dead" and "cannot read this file" in error and attempts == queue.MAX_ATTEMPTS


async def test_a_process_that_never_imported_the_pipeline_still_runs_every_step(team, monkeypatch):
    """Found by the matching evaluation: a script draining the queue without importing app.pipeline ran no steps and
    sent every document to review with no reason given."""
    from app.intake import queue
    from seed.documents import render_pdf
    from tests.samples import invoice

    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    monkeypatch.setattr(queue, "PIPELINE", [])  # as in a fresh process: nothing registered
    doc = (await upload(team.clerk, ("fresh.pdf", render_pdf(invoice(number="INV-FRESH")), "application/pdf"))).json()[
        "documents"
    ][0]
    await queue.process_pending("test")
    pages = await rows(team.org_id, "SELECT count(*) FROM document_pages WHERE document_id = :d", d=doc["id"])
    assert pages[0][0] == 1


async def test_the_inbox_keeps_the_senders_address_and_subject(client, team):
    """Found in review: the sender and subject of an e-mail were dropped, so nobody could see where a document came from."""
    key = (await team.admin.post("/api/inbox-keys", json={"name": "ap mailbox"})).json()
    body = {"from": "billing@acme.example", "subject": "Invoice 1001", "attachments": [
        {"filename": "inv.pdf", "content_base64": base64.b64encode(pdf("INVOICE INV-1001")).decode()}]}  # fmt: skip
    doc = (await client.post("/v1/inbox", json=body, headers={"Authorization": f"Bearer {key['key']}"})).json()[
        "documents"
    ][0]
    shown = (await team.viewer.get(f"/api/documents/{doc['id']}")).json()
    assert shown["inbox_from"] == "billing@acme.example" and shown["inbox_subject"] == "Invoice 1001"
    assert (await team.viewer.get("/api/documents")).json()["documents"][0]["inbox_from"] == "billing@acme.example"
