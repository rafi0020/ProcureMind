"""Vendors, their aliases and bank accounts. A bank change is the classic payment-fraud move, so it needs two
administrators."""

from app.db import tenant_session
from tests.conftest import PASSWORD


async def new_vendor(team, name="Acme Supplies", **extra) -> dict:
    body = {"name": name, "default_currency": "USD", "aliases": ["Acme"]} | extra
    r = await team.admin.post("/api/vendors", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def second_admin(team):
    email = f"second-{team.org_id[:8]}@example.test"
    r = await team.admin.post(
        "/api/users", json={"email": email, "name": "Second", "role": "admin", "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return await team.login(email)


async def test_vendors_are_listed_for_everyone_and_created_only_by_admins(team):
    v = await new_vendor(team)
    assert v["name"] == "Acme Supplies" and v["default_currency"] == "USD"
    for client in (team.viewer, team.clerk, team.approver):
        assert [x["name"] for x in (await client.get("/api/vendors")).json()["vendors"]] == ["Acme Supplies"]
    assert (await team.clerk.post("/api/vendors", json={"name": "Other", "default_currency": "USD"})).status_code == 403
    assert (
        await team.admin.post("/api/vendors", json={"name": "acme supplies", "default_currency": "USD"})
    ).status_code == 409
    assert (
        await team.admin.post("/api/vendors", json={"name": "Bad", "default_currency": "dollars"})
    ).status_code == 422


async def test_a_bank_change_waits_for_a_second_administrator(team):
    v = await new_vendor(team)
    other = await second_admin(team)
    first = (
        await team.admin.post(
            f"/api/vendors/{v['id']}/bank-accounts",
            json={"iban_or_account": "GB33BUKB20201555555555", "bank_name": "Bank A"},
        )
    ).json()
    assert first["status"] == "pending"
    assert (
        await team.admin.post(
            f"/api/vendors/{v['id']}/bank-accounts/{first['id']}/decide", json={"decision": "approved"}
        )
    ).status_code == 403
    assert (
        await other.post(f"/api/vendors/{v['id']}/bank-accounts/{first['id']}/decide", json={"decision": "approved"})
    ).json()["status"] == "active"

    change = (
        await other.post(
            f"/api/vendors/{v['id']}/bank-accounts",
            json={"iban_or_account": "DE89370400440532013000", "bank_name": "Bank B"},
        )
    ).json()
    assert (
        await team.admin.post(
            f"/api/vendors/{v['id']}/bank-accounts/{change['id']}/decide", json={"decision": "approved"}
        )
    ).status_code == 200
    accounts = {
        a["iban_or_account"]: a["status"]
        for a in (await team.admin.get(f"/api/vendors/{v['id']}")).json()[
            "bank_accounts"
        ]  # full numbers: administrators
    }
    assert accounts == {"GB33BUKB20201555555555": "retired", "DE89370400440532013000": "active"}
    again = await other.post(
        f"/api/vendors/{v['id']}/bank-accounts/{change['id']}/decide", json={"decision": "approved"}
    )
    assert again.status_code == 409


async def test_a_sole_administrator_may_decide_and_it_is_recorded(team):
    v = await new_vendor(team)
    acc = (
        await team.admin.post(
            f"/api/vendors/{v['id']}/bank-accounts",
            json={"iban_or_account": "NL91ABNA0417164300", "bank_name": "Bank C"},
        )
    ).json()
    assert (
        await team.admin.post(f"/api/vendors/{v['id']}/bank-accounts/{acc['id']}/decide", json={"decision": "approved"})
    ).status_code == 200
    entry = (await team.admin.get("/api/audit", params={"action": "vendor.bank_account.approved"})).json()["entries"][0]
    assert entry["detail"]["sole_admin"] is True


async def test_only_admins_touch_bank_details_and_other_organisations_see_nothing(team, other_team):
    v = await new_vendor(team)
    assert (
        await team.clerk.post(f"/api/vendors/{v['id']}/bank-accounts", json={"iban_or_account": "X1", "bank_name": "B"})
    ).status_code == 403
    assert (
        await team.approver.post(
            f"/api/vendors/{v['id']}/bank-accounts", json={"iban_or_account": "X1", "bank_name": "B"}
        )
    ).status_code == 403
    assert (await other_team.admin.get(f"/api/vendors/{v['id']}")).status_code == 404
    assert (await other_team.admin.get("/api/vendors")).json()["vendors"] == []


async def test_find_vendor_by_name_alias_and_close_spelling(team):
    from app.vendors import find_vendor

    v = await new_vendor(team, aliases=["ACME"])
    await new_vendor(team, name="Northwind Traders", aliases=[])
    async with tenant_session(team.org_id) as db:
        assert str((await find_vendor(db, "ACME Supplies Ltd.")).id) == v["id"]
        assert str((await find_vendor(db, "acme")).id) == v["id"]
        assert (await find_vendor(db, "Northwnd Traders")).name == "Northwind Traders"  # one letter missing
        assert await find_vendor(db, "Completely Different Co") is None


async def test_a_viewer_sees_only_the_last_four_digits_of_an_account(team):
    """I10: the full account number is for administrators; everyone else sees its last four characters."""
    v = await new_vendor(team)
    await team.admin.post(
        f"/api/vendors/{v['id']}/bank-accounts",
        json={"iban_or_account": "GB33BUKB20201555555555", "bank_name": "Bank A"},
    )
    seen = (await team.viewer.get(f"/api/vendors/{v['id']}")).json()["bank_accounts"]
    assert [a["iban_or_account"] for a in seen] == ["…5555"]
    assert (await team.clerk.get(f"/api/vendors/{v['id']}")).json()["bank_accounts"][0]["iban_or_account"] == "…5555"
    full = (await team.admin.get(f"/api/vendors/{v['id']}")).json()["bank_accounts"]
    assert [a["iban_or_account"] for a in full] == ["GB33BUKB20201555555555"]
