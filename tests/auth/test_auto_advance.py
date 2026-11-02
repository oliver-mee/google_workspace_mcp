"""Native routes with provider calls mocked; no prior cookies or pinned client."""

from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import AnyUrl

from auth.auto_advance import CONFIG_ENV, auto_advance_enabled
from tests.auth.test_oauth_consent import (
    REDIRECT,
    VERIFIER,
    Fields,
    approve,
    callback,
    assertion,
    install_cimd,
    native as native_fixture,
    page,
    start,
)


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setenv(CONFIG_ENV, "true")
    yield from native_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("false", False), ("true", True), (" TRUE ", True)],
)
def test_explicit_deployment_configuration(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(CONFIG_ENV, raising=False)
    else:
        monkeypatch.setenv(CONFIG_ENV, value)
    assert auto_advance_enabled() is expected


@pytest.mark.parametrize("value", ["", "1", "yes", "remember", "{}"])
def test_invalid_config_fails_startup(monkeypatch, value):
    monkeypatch.setenv(CONFIG_ENV, value)
    with pytest.raises(ValueError, match=CONFIG_ENV):
        auto_advance_enabled()


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", None])
@pytest.mark.parametrize("mode", [True, "remember"])
def test_new_client_first_visit_callback_pkce_replay_and_refresh(native, site, mode):
    c, p, google, cid = native
    assert p._auto_advance_enabled
    p._require_authorization_consent = mode
    c.cookies.clear()
    automatic = page(c, start(c, cid), site)
    assert automatic.status_code == 302
    assert urlsplit(automatic.headers["location"]).hostname == "accounts.google.com"
    assert not any(x.name.endswith("MCP_APPROVED_CLIENTS") for x in c.cookies.jar)
    saved = list(c.cookies.jar)
    c.cookies.clear()
    assert callback(c, automatic).status_code == 403
    google.fetch_token.assert_not_awaited()
    for x in saved:
        c.cookies.set(x.name, x.value + "bad", domain=x.domain, path=x.path)
    assert callback(c, automatic).status_code == 403
    c.cookies.clear()
    for x in saved:
        c.cookies.set(x.name, x.value, domain=x.domain, path=x.path)
    returned = callback(c, automatic)
    assert returned.status_code == 302
    code = parse_qs(urlsplit(returned.headers["location"]).query)["code"][0]
    data = {
        "grant_type": "authorization_code",
        "client_id": cid,
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": "wrong",
    }
    assert c.post("/token", data=data).status_code in (400, 401)
    data["code_verifier"] = VERIFIER
    token = c.post("/token", data=data)
    assert token.status_code == 200
    assert c.post("/token", data=data).status_code in (400, 401)
    assert callback(c, automatic).status_code == 400
    google.refresh_token.return_value = dict(google.fetch_token.return_value)
    refresh = c.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": cid,
            "refresh_token": token.json()["refresh_token"],
        },
    )
    assert refresh.status_code == 200


def test_second_unpinned_client_and_changed_valid_request_also_advance(native):
    c, p, google, cid = native
    first = page(c, start(c, cid))
    assert first.status_code == 302
    other = c.post(
        "/register",
        json={
            "client_name": "Other application",
            "redirect_uris": [REDIRECT],
            "scope": "openid extra",
        },
    )
    assert other.status_code == 201
    c.cookies.clear()
    assert (
        page(c, start(c, other.json()["client_id"], scope="openid extra")).status_code
        == 302
    )
    assert (
        page(
            c,
            start(
                c, cid, redirect=REDIRECT.replace("one", "two"), scope="openid extra"
            ),
        ).status_code
        == 302
    )
    google.fetch_token.assert_not_awaited()


def test_invalid_requests_and_changed_redirect_metadata_do_not_advance(native):
    c, p, google, cid = native
    assert (
        c.get(
            "/authorize",
            params={
                "client_id": cid,
                "redirect_uri": "https://evil.example/callback",
                "response_type": "code",
            },
        ).status_code
        == 400
    )
    url = start(c, cid)

    async def revoke_redirect():
        client = await p.get_client(cid)
        await p._client_store.put(
            key=cid,
            value=client.model_copy(
                update={"redirect_uris": [AnyUrl("https://different.example/callback")]}
            ),
        )

    c.portal.call(revoke_redirect)
    assert page(c, url).status_code == 400
    assert c.get("/consent", params={"txn_id": "missing"}).status_code == 400
    google.fetch_token.assert_not_awaited()


def test_disabled_service_keeps_approval_post_csrf_and_denial(native):
    c, p, google, cid = native
    p._auto_advance_enabled = False
    r = page(c, start(c, cid))
    assert r.status_code == 200
    fields = Fields(r.text).fields
    assert (
        c.post(
            str(r.url), data={**fields, "csrf_token": "forged", "action": "approve"}
        ).status_code
        == 400
    )
    denied = approve(c, r, action="deny")
    assert denied.status_code == 302
    assert "access_denied" in denied.headers["location"]
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize(
    "bad", [None, "missing", "wrong-key", "audience", "subject", "expired"]
)
def test_auto_advance_still_requires_cimd_client_assertion(native, monkeypatch, bad):
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa

    c, p, google, _ = native
    cid, key, _ = install_cimd(p, monkeypatch, name="Arbitrary app")
    automatic = page(c, start(c, cid))
    assert automatic.status_code == 302
    returned = callback(c, automatic)
    assert returned.status_code == 302
    code = parse_qs(urlsplit(returned.headers["location"]).query)["code"][0]
    updates = {}
    if bad == "wrong-key":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif bad == "audience":
        updates["aud"] = "https://other.example/token"
    elif bad == "subject":
        updates["sub"] = "other-client"
    elif bad == "expired":
        updates.update(exp=int(time.time()) - 300, iat=int(time.time()) - 360)
    data = {
        "grant_type": "authorization_code",
        "client_id": cid,
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
    }
    if bad != "missing":
        data["client_assertion"] = assertion(key, cid, **updates)
    response = c.post("/token", data=data)
    assert response.status_code == (200 if bad is None else 401)
    if bad is None:
        assert c.post("/token", data=data).status_code == 401
    google.fetch_token.assert_awaited_once()


@pytest.mark.parametrize("credential", ["correct", "wrong", "missing"])
def test_auto_advance_retains_confidential_basic_secret_validation(native, credential):
    c, p, google, cid = native
    secret = "synthetic-confidential-secret"

    async def seed_confidential():
        client = await p.get_client(cid)
        await p._client_store.put(
            key=cid,
            value=client.model_copy(
                update={
                    "client_secret": secret,
                    "token_endpoint_auth_method": "client_secret_basic",
                }
            ),
        )

    c.portal.call(seed_confidential)
    automatic = page(c, start(c, cid))
    assert automatic.status_code == 302
    returned = callback(c, automatic)
    code = parse_qs(urlsplit(returned.headers["location"]).query)["code"][0]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    if credential == "missing":
        data["client_id"] = cid
        response = c.post("/token", data=data)
    else:
        response = c.post(
            "/token",
            data=data,
            auth=(cid, secret if credential == "correct" else "wrong"),
        )
    assert response.status_code == (200 if credential == "correct" else 401)
