"""Real native OAuth routes; only Google's token HTTP boundary is mocked."""

import base64
import hashlib
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.testclient import TestClient

from auth.google_oauth_provider import GoogleProvider


BASE = "https://mcp.example.test"
REDIRECT = "http://localhost:9876/callback?destination=one"
VERIFIER = "a" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


class Fields(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.fields = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.fields[attrs["name"]] = attrs.get("value", "")


@pytest.fixture
def native(monkeypatch):
    p = GoogleProvider(
        client_id="synthetic-google",
        client_secret="synthetic-secret",
        jwt_signing_key="synthetic-signing-key",
        base_url=BASE,
        redirect_path="/oauth2callback",
        client_storage=MemoryStore(),
        required_scopes=["openid"],
        valid_scopes=["openid", "extra"],
        require_authorization_consent="remember",
    )
    google = AsyncMock()
    google.fetch_token.return_value = {
        "access_token": "synthetic-google-token",
        "refresh_token": "synthetic-google-refresh",
        "expires_in": 3600,
        "scope": "openid extra",
        "token_type": "Bearer",
    }

    @asynccontextmanager
    async def boundary():
        yield google

    monkeypatch.setattr(p, "_upstream_oauth_client", boundary)
    with TestClient(
        FastMCP("Preface Workspace", auth=p).http_app(),
        base_url=BASE,
        follow_redirects=False,
    ) as c:
        r = c.post(
            "/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [REDIRECT, REDIRECT.replace("one", "two")],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "openid extra",
            },
        )
        assert r.status_code == 201, r.text
        yield c, p, google, r.json()["client_id"]


def start(c, client_id, *, scope="openid", redirect=REDIRECT):
    r = c.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": scope,
            "state": "client-owned-state",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": BASE + "/mcp",
        },
    )
    assert r.status_code == 302, r.text
    return r.headers["location"]


def page(c, url, site="cross-site"):
    return c.get(url, headers={"Sec-Fetch-Site": site} if site else {})


def approve(c, r, **changes):
    return c.post(
        str(r.url), data={**Fields(r.text).fields, "action": "approve", **changes}
    )


def callback(c, approval):
    txn = parse_qs(urlsplit(approval.headers["location"]).query)["state"][0]
    return c.get(
        "/oauth2callback", params={"code": "synthetic-google-code", "state": txn}
    )


def test_compact_real_page_and_full_pkce_exchange(native):
    c, p, google, client_id = native
    r = page(c, start(c, client_id))
    assert r.status_code == 200
    assert "Connect ChatGPT to Preface Workspace" in r.text
    assert "<details><summary>" in r.text
    assert "<script" not in r.text
    assert "frame-ancestors" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "__Host-MCP_CONSENT_STATE_" in str(r.headers)
    approval = approve(c, r)
    assert approval.status_code == 302, approval.text
    result = callback(c, approval)
    assert result.status_code == 302, result.text
    params = parse_qs(urlsplit(result.headers["location"]).query)
    assert params["state"] == ["client-owned-state"]
    code = params["code"][0]
    token = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert token.status_code == 200, token.text
    google.fetch_token.assert_awaited_once()
    replay = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert replay.status_code in (400, 401)
    assert callback(c, approval).status_code == 400


@pytest.mark.parametrize(
    "attack", ["absent", "tampered", "wrong-token", "wrong-txn", "wrong-context"]
)
def test_csrf_rejection_before_google(native, attack):
    c, _, google, client_id = native
    r = page(c, start(c, client_id))
    fields = Fields(r.text).fields
    if attack in ("absent", "tampered"):
        cookies = list(c.cookies.jar)
        c.cookies.clear()
        if attack == "tampered":
            for cookie in cookies:
                c.cookies.set(
                    cookie.name,
                    cookie.value + "tampered",
                    domain=cookie.domain,
                    path=cookie.path,
                )
    else:
        fields[
            {
                "wrong-token": "csrf_token",
                "wrong-txn": "txn_id",
                "wrong-context": "approval_context",
            }[attack]
        ] = "forged"
    result = c.post(str(r.url), data={**fields, "action": "approve"})
    assert result.status_code in (400, 403), result.text
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("attack", ["absent", "tampered", "unapproved"])
def test_callback_requires_approving_browser(native, attack):
    c, _, google, client_id = native
    r = page(c, start(c, client_id))
    approval = approve(c, r)
    if attack == "unapproved":
        unapproved = start(c, client_id)
        txn = parse_qs(urlsplit(unapproved).query)["txn_id"][0]
        response = c.get("/oauth2callback", params={"state": txn, "code": "fake"})
    else:
        cookies = list(c.cookies.jar)
        c.cookies.clear()
        if attack == "tampered":
            for cookie in cookies:
                if "BINDING" in cookie.name:
                    c.cookies.set(
                        cookie.name,
                        cookie.value + "tampered",
                        domain=cookie.domain,
                        path=cookie.path,
                    )
        response = callback(c, approval)
    assert response.status_code == 403, response.text
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", None])
def test_remembered_real_client_still_prompts_for_external_navigation(native, site):
    c, _, google, client_id = native
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    r = page(c, start(c, client_id), site)
    assert r.status_code == 200
    assert "Approve connection" in r.text
    google.fetch_token.assert_not_awaited()


def test_exact_remembered_same_origin_path_retains_callback_binding(native):
    c, _, google, client_id = native
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    r = page(c, start(c, client_id), "same-origin")
    assert r.status_code == 302, r.text
    assert "accounts.google.com" in r.headers["location"]
    assert callback(c, r).status_code == 302
    google.fetch_token.assert_awaited_once()


@pytest.mark.parametrize(
    "change", ["scope", "redirect", "new-client", "branding", "auth-method"]
)
def test_changed_context_cannot_reuse_approval(native, change):
    c, p, google, client_id = native
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    kwargs = {}
    if change == "scope":
        kwargs["scope"] = "openid extra"
    elif change == "redirect":
        kwargs["redirect"] = REDIRECT.replace("one", "two")
    elif change == "new-client":
        client_id = c.post(
            "/register", json={"client_name": "ChatGPT", "redirect_uris": [REDIRECT]}
        ).json()["client_id"]
    else:

        async def modify():
            record = await p.get_client(client_id)
            if change == "branding":
                record.client_name = "<img src=x onerror=alert(1)>ChatGPT"
            else:
                record.token_endpoint_auth_method = "client_secret_basic"
                record.client_secret = "synthetic-confidential"
            await p._client_store.put(key=client_id, value=record)

        c.portal.call(modify)
    r = page(c, start(c, client_id, **kwargs), "same-origin")
    assert r.status_code == 200, r.text
    assert "Approve connection" in r.text
    if change == "branding":
        assert "&lt;img" in r.text
        assert "<img" not in r.text
    google.fetch_token.assert_not_awaited()


def test_deny_and_csrf_replay(native):
    c, _, google, client_id = native
    r = page(c, start(c, client_id))
    response = approve(c, r, action="deny")
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == [
        "access_denied"
    ]
    assert approve(c, r).status_code in (400, 403)
    google.fetch_token.assert_not_awaited()


def test_metadata_change_after_render_rejected(native):
    c, p, google, client_id = native
    r = page(c, start(c, client_id))

    async def modify():
        record = await p.get_client(client_id)
        record.client_name = "Changed after render"
        await p._client_store.put(key=client_id, value=record)

    c.portal.call(modify)
    assert approve(c, r).status_code == 400
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["no-cookie", "tampered-cookie", "old-cookie"])
def test_remember_cookie_is_required_and_legacy_keys_are_not_accepted(native, mutation):
    c, p, google, client_id = native
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    cookies = list(c.cookies.jar)
    c.cookies.clear()
    if mutation == "tampered-cookie":
        for cookie in cookies:
            c.cookies.set(
                cookie.name,
                cookie.value + "forged",
                domain=cookie.domain,
                path=cookie.path,
            )
    if mutation == "old-cookie":
        old = p._encode_list_cookie([f"{client_id}:http://localhost:9876/callback"])
        c.cookies.set(
            "__Host-MCP_APPROVED_CLIENTS", old, domain="mcp.example.test", path="/"
        )
    assert page(c, start(c, client_id), "same-origin").status_code == 200
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("bad", ["scope", "redirect", "client", "resource", "pkce"])
def test_invalid_authorization_rejected(native, bad):
    c, _, google, client_id = native
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "openid",
        "state": "state",
        "resource": BASE + "/mcp",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    key, value = {
        "scope": ("scope", "not-allowed"),
        "redirect": ("redirect_uri", "https://attacker.test/callback"),
        "client": ("client_id", "unknown"),
        "resource": ("resource", "https://attacker.test/mcp"),
        "pkce": ("code_challenge_method", "invalid"),
    }[bad]
    params[key] = value
    response = c.get("/authorize", params=params)
    if response.status_code == 302:
        assert "error=" in response.headers["location"]
        assert "/consent" not in response.headers["location"]
    else:
        assert response.status_code in (400, 401)
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("bad", ["pkce", "redirect", "client"])
def test_downstream_code_still_bound_after_consent(native, bad):
    c, _, google, client_id = native
    approval = approve(c, page(c, start(c, client_id)))
    result = callback(c, approval)
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": REDIRECT,
    }
    data[
        {"pkce": "code_verifier", "redirect": "redirect_uri", "client": "client_id"}[
            bad
        ]
    ] = {
        "pkce": "wrong",
        "redirect": "https://attacker.test/cb",
        "client": "unknown",
    }[bad]
    assert c.post("/token", data=data).status_code in (400, 401)
    google.fetch_token.assert_awaited_once()


def test_parallel_forms_keep_separate_csrf_and_transaction_bindings(native):
    c, _, google, client_id = native
    first = page(c, start(c, client_id))
    second = page(c, start(c, client_id, scope="openid extra"))
    approved_first = approve(c, first)
    approved_second = approve(c, second)
    assert approved_first.status_code == approved_second.status_code == 302
    assert callback(c, approved_first).status_code == 302
    assert callback(c, approved_second).status_code == 302
    assert google.fetch_token.await_count == 2


def install_cimd(
    p,
    monkeypatch,
    *,
    client_id="https://client.example.test/oauth.json",
    name="ChatGPT",
):
    """Mock only metadata retrieval, not CIMD resolution or JWT verification."""
    import json
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastmcp.server.auth.cimd import CIMDDocument
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"
    doc = CIMDDocument(
        client_id=client_id,
        client_name=name,
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="private_key_jwt",
        jwks={"keys": [jwk]},
        scope="openid extra",
        grant_types=["authorization_code", "refresh_token"],
    )
    monkeypatch.setattr(p._cimd_manager._fetcher, "fetch", AsyncMock(return_value=doc))
    return client_id, key, doc


def assertion(key, client_id, **updates):
    import time
    import uuid
    import jwt

    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": BASE + "/token",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "jti": str(uuid.uuid4()),
        **updates,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize(
    "bad", [None, "missing", "wrong-key", "audience", "subject", "expired"]
)
def test_cimd_private_key_jwt_native_exchange(native, monkeypatch, bad):
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa

    c, p, google, _ = native
    client_id, key, _ = install_cimd(p, monkeypatch)
    r = page(c, start(c, client_id))
    # Metadata/domain authenticity is not a browser-intent claim.
    assert "Verified domain" not in r.text
    approved = approve(c, r)
    result = callback(c, approved)
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    updates = {}
    if bad == "audience":
        updates["aud"] = "https://attacker.test/token"
    elif bad == "subject":
        updates["sub"] = "someone-else"
    elif bad == "expired":
        updates.update(exp=int(time.time()) - 300, iat=int(time.time()) - 360)
    if bad == "wrong-key":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
    }
    if bad != "missing":
        data["client_assertion"] = assertion(key, client_id, **updates)
    response = c.post("/token", data=data)
    assert response.status_code == (200 if bad is None else 401), response.text
    google.fetch_token.assert_awaited_once()
    # Reusing a signed assertion is rejected even on a different grant request.
    if bad is None:
        assert c.post("/token", data=data).status_code == 401


@pytest.mark.parametrize("site", ["cross-site", "none", "same-site", None])
def test_real_allowed_cimd_identity_does_not_establish_browser_intent(
    native, monkeypatch, site
):
    c, p, google, _ = native
    client_id, _, _ = install_cimd(p, monkeypatch)
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    assert page(c, start(c, client_id), site).status_code == 200
    google.fetch_token.assert_not_awaited()


def test_cimd_same_origin_remember_and_key_rotation(native, monkeypatch):
    c, p, _, _ = native
    client_id, _, doc = install_cimd(p, monkeypatch)
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    assert page(c, start(c, client_id), "same-origin").status_code == 302
    doc.jwks["keys"][0]["kid"] = "rotated-key"
    assert page(c, start(c, client_id), "same-origin").status_code == 200


def test_forged_chatgpt_domain_and_name_do_not_inherit_trust(native, monkeypatch):
    c, p, google, _ = native
    client_id, _, _ = install_cimd(
        p, monkeypatch, client_id="https://chatgpt.com.attacker.test/client.json"
    )
    r = page(c, start(c, client_id), "same-origin")
    assert r.status_code == 200
    assert "chatgpt.com.attacker.test" in r.text
    assert "provided by client" in r.text
    google.fetch_token.assert_not_awaited()


def test_always_mode_never_silently_continues(native):
    c, p, google, client_id = native
    p._require_authorization_consent = True
    assert approve(c, page(c, start(c, client_id))).status_code == 302
    assert page(c, start(c, client_id), "same-origin").status_code == 200
    google.fetch_token.assert_not_awaited()


def test_unknown_upstream_form_contract_fails_closed(native, monkeypatch):
    from fastmcp.server.auth.oauth_proxy import consent

    c, _, google, client_id = native
    original = consent.create_consent_html
    monkeypatch.setattr(
        consent,
        "create_consent_html",
        lambda **kwargs: original(**kwargs).replace(
            'id="consentForm"', 'id="new-contract"'
        ),
    )
    result = page(c, start(c, client_id))
    assert result.status_code == 503
    assert "unavailable" in result.text
    google.fetch_token.assert_not_awaited()


def test_approval_csrf_token_cannot_be_replayed(native):
    c, _, google, client_id = native
    r = page(c, start(c, client_id))
    assert approve(c, r).status_code == 302
    assert approve(c, r).status_code in (400, 403)
    google.fetch_token.assert_not_awaited()


def test_consent_form_contract_rejects_added_visible_or_duplicate_fields():
    from auth.oauth_consent import ConsentForm
    from fastmcp.server.auth.oauth_proxy.ui import create_consent_html

    original = create_consent_html(
        client_id="client",
        redirect_uri=REDIRECT,
        scopes=["openid"],
        txn_id="txn",
        csrf_token="csrf",
    )
    for insertion in [
        '<input name="surprise">',
        '<input type="hidden" name="txn_id" value="other">',
    ]:
        parser = ConsentForm()
        parser.feed(original.replace("</form>", insertion + "</form>"))
        assert not parser.recognized
