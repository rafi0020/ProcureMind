"""Sign-up, sign-in, sessions and CSRF, exercised through the HTTP API."""

from tests.conftest import PASSWORD


async def test_signup_creates_an_org_with_its_first_admin(team):
    r = await team.admin.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
    assert r.json()["user"]["email"] == team.emails["admin"]


async def test_wrong_password_and_unknown_account_get_the_same_answer(client, team):
    wrong = await client.post("/api/auth/login", json={"email": team.emails["viewer"], "password": "nope-12345"})
    unknown = await client.post("/api/auth/login", json={"email": "nobody@example.test", "password": "nope-12345"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Incorrect e-mail or password."}


async def test_sixth_failed_login_in_a_minute_is_refused(client):
    body = {"email": "locked-out@example.test", "password": "wrong-password-1"}
    for _ in range(5):
        assert (await client.post("/api/auth/login", json=body)).status_code == 401
    r = await client.post("/api/auth/login", json=body)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


async def test_session_cookie_is_httponly_and_samesite(client, team):
    r = await client.post("/api/auth/login", json={"email": team.emails["viewer"], "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "procuremind_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


async def test_state_changing_request_without_csrf_token_is_refused(team):
    del team.clerk.headers["X-CSRF-Token"]
    r = await team.clerk.post("/api/auth/logout")
    assert r.status_code == 403
    assert (await team.clerk.get("/api/auth/me")).status_code == 200  # still signed in


async def test_logout_ends_the_session_on_the_server(team):
    cookie = team.viewer.cookies["procuremind_session"]
    assert (await team.viewer.post("/api/auth/logout")).status_code == 204
    team.viewer.cookies.set("procuremind_session", cookie)  # replay the old cookie
    assert (await team.viewer.get("/api/auth/me")).status_code == 401


async def test_no_cookie_means_not_signed_in(client):
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/users")).status_code == 401


async def test_weak_password_is_rejected_with_the_reason(client):
    base = {"email": "weak@example.test", "name": "W", "org_name": "Weak Org"}
    assert (await client.post("/api/auth/signup", json=base | {"password": "short1"})).status_code == 422
    r = await client.post("/api/auth/signup", json=base | {"password": "onlyletterslong"})
    assert r.status_code == 422 and "letter and a number" in r.json()["detail"]


async def test_duplicate_email_cannot_sign_up(client, team):
    r = await client.post(
        "/api/auth/signup",
        json={
            "email": team.emails["admin"].upper(),
            "password": PASSWORD,
            "name": "X",
            "org_name": "Dup Org",
        },
    )
    assert r.status_code == 409


async def test_changing_password_signs_out_other_sessions(team):
    second = await team.login(team.emails["clerk"])
    r = await team.clerk.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "Brand-New-Pass-7"}
    )
    assert r.status_code == 204
    assert (await team.clerk.get("/api/auth/me")).status_code == 200
    assert (await second.get("/api/auth/me")).status_code == 401
    assert (await team.login(team.emails["clerk"], "Brand-New-Pass-7")) is not None


def racing_once(monkeypatch, module, org_id, email):
    """Simulates a second request that takes the same e-mail address between this request's check and its insert:
    the first query this request makes is followed by a committed row with that address."""
    from contextlib import asynccontextmanager

    from app.db import system_session as real_session
    from app.models.identity import User

    class Racing:
        def __init__(self, db):
            self._db, self._armed = db, True

        async def execute(self, *args, **kwargs):
            result = await self._db.execute(*args, **kwargs)
            if self._armed:
                self._armed = False
                async with real_session() as other:
                    other.add(User(org_id=org_id, email=email, name="Racer", password_hash="x", role="clerk"))
            return result

        def __getattr__(self, name):
            return getattr(self._db, name)

    @asynccontextmanager
    async def wrapped(*args, **kwargs):
        async with real_session(*args, **kwargs) as db:
            yield Racing(db)

    monkeypatch.setattr(module, "system_session", wrapped)


async def test_signup_with_an_e_mail_taken_at_the_same_moment_is_409_not_500(client, team, monkeypatch):
    """Found in review: the check and the insert were separate, so a race was an unhandled IntegrityError (500)."""
    from app.api import auth

    email = "racer-signup@example.test"
    racing_once(monkeypatch, auth, team.org_id, email)
    r = await client.post(
        "/api/auth/signup", json={"email": email, "password": PASSWORD, "name": "R", "org_name": "Race Org"}
    )
    assert r.status_code == 409, r.text


async def test_creating_a_user_with_an_e_mail_taken_at_the_same_moment_is_409_not_500(team, monkeypatch):
    from app.api import users

    email = "racer-user@example.test"
    racing_once(monkeypatch, users, team.org_id, email)
    r = await team.admin.post("/api/users", json={"email": email, "name": "R", "role": "clerk", "password": PASSWORD})
    assert r.status_code == 409, r.text
