"""The demo data: planted cases with an answer key, processed through the real intake path."""

from tests.conftest import rows


async def test_planted_cases_exist_go_through_intake_and_match_as_the_answer_key_says(demo):
    from seed.demo_users import KESTREL
    from seed.generate import PLANTED

    truth = demo
    assert set(truth["planted"]) == set(PLANTED)

    by_name = {
        r.filename: r
        for r in await rows(
            KESTREL,
            "SELECT d.filename, d.status, i.status AS invoice_status, i.id FROM documents d LEFT JOIN invoices i ON i.document_id = d.id",
        )
    }
    assert all(r.status in ("extracted", "needs_review") for r in by_name.values())
    for case, filenames in truth["planted"].items():
        for name in filenames:
            expected = truth["documents"][name]
            got = by_name[name]
            assert got.invoice_status == expected["invoice_status"], (case, name, got.invoice_status, expected)
            if expected.get("findings"):
                exc = await rows(KESTREL, "SELECT findings, reasons FROM exceptions WHERE invoice_id = :i", i=got.id)
                codes = {f["code"] for f in exc[0].findings} | {r["code"] for r in exc[0].reasons}
                assert set(expected["findings"]) <= codes, (case, codes)

    # Ordinary documents extract their number and total as the key says.
    ordinary = [n for n, t in truth["documents"].items() if not t.get("planted")]
    assert ordinary
    for name in ordinary:
        found = await rows(
            KESTREL,
            "SELECT i.invoice_number, i.total FROM invoices i JOIN documents d ON d.id = i.document_id WHERE d.filename = :n",
            n=name,
        )
        if found:  # Ridgeway's documents are in the other organisation
            assert (found[0].invoice_number, f"{found[0].total:.2f}") == (
                truth["documents"][name]["invoice_number"],
                truth["documents"][name]["total"],
            )


async def test_the_generator_is_deterministic():
    from seed.generate import plan

    assert plan(seed=7, scale=0.05) == plan(seed=7, scale=0.05)
    assert plan(seed=7, scale=0.05) != plan(seed=8, scale=0.05)
