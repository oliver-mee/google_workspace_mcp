"""Native HTTP experiment, synthetic identities/clients and mocked Google only."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.requests import Request
from starlette.testclient import TestClient

from tests.auth.personal_auto_advance import PersonalAutoAdvanceExperiment
from tests.auth.test_oauth_consent import (
    BASE,
    CHALLENGE,
    REDIRECT,
    VERIFIER,
    Fields,
    approve,
    assertion,
    callback,
    install_cimd,
    page,
    start,
)


@pytest.fixture
def experiment(monkeypatch):
    p = PersonalAutoAdvanceExperiment(
        client_id="synthetic-google",
        client_secret="synthetic-secret",
        jwt_signing_key="synthetic-key",
        base_url=BASE,
        redirect_path="/oauth2callback",
        client_storage=MemoryStore(),
        required_scopes=["openid"],
        valid_scopes=["openid", "extra"],
        require_authorization_consent="remember",
    )
    google = AsyncMock()
    google.fetch_token.return_value = {
        "access_token": "synthetic-browser-B-google-grant",
        "refresh_token": "synthetic-refresh",
        "expires_in": 3600,
        "scope": "openid extra",
        "token_type": "Bearer",
    }

    @asynccontextmanager
    async def boundary():
        yield google

    monkeypatch.setattr(p, "_upstream_oauth_client", boundary)
    app = FastMCP("Personal isolated experiment", auth=p).http_app()
    with TestClient(app, base_url=BASE, follow_redirects=False) as a:
        registration = a.post(
            "/register",
            json={
                "client_name": "Synthetic trusted app",
                "redirect_uris": [REDIRECT, REDIRECT.replace("one", "two")],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
                "scope": "openid extra",
            },
        )
        assert registration.status_code == 201
        # B has a separate cookie jar, even though the same AS app handles both.
        b = TestClient(app, base_url=BASE, follow_redirects=False)
        b.portal = a.portal
        yield a, b, p, google, registration.json()["client_id"]
        b.close()


def pin_context(c, p, consent_url):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "query_string": urlsplit(consent_url).query.encode(),
        }
    )
    fingerprint = c.portal.call(p._consent_context, request)[2]
    p.experiment_contexts = frozenset({fingerprint})
    return fingerprint


def remember(c, p, client_id):
    url = start(c, client_id)
    pin_context(c, p, url)
    result = approve(c, page(c, url))
    assert result.status_code == 302
    return result


def code_from(result):
    return parse_qs(urlsplit(result.headers["location"]).query)["code"][0]


def exchange(c, cid, code, **changes):
    return c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
            **changes,
        },
    )


def test_disabled_and_first_visit_never_auto_advance(experiment):
    a, b, p, google, cid = experiment
    remember(a, p, cid)
    assert page(a, start(a, cid)).status_code == 200
    p.experiment_enabled = True
    assert page(b, start(b, cid)).status_code == 200
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", None])
def test_explicit_optin_pinned_previously_approved_context_advances(experiment, site):
    a, _, p, _, cid = experiment
    remember(a, p, cid)
    p.experiment_enabled = True
    automatic = page(a, start(a, cid), site)
    assert automatic.status_code == 302
    assert "accounts.google.com" in automatic.headers["location"]
    assert "MCP_CONSENT_BINDING" in str(automatic.headers)
    assert exchange(a, cid, code_from(callback(a, automatic))).status_code == 200


@pytest.mark.parametrize("bad", ["missing", "tampered", "other-browser"])
def test_remembered_browser_cookie_required(experiment, bad):
    a, b, p, google, cid = experiment
    remember(a, p, cid)
    p.experiment_enabled = True
    if bad == "other-browser":
        c = b
    else:
        c = a
        cookies = list(c.cookies.jar)
        c.cookies.clear()
        if bad == "tampered":
            for x in cookies:
                c.cookies.set(x.name, x.value + "bad", domain=x.domain, path=x.path)
    assert page(c, start(c, cid)).status_code == 200
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["scope", "redirect", "resource", "name", "client", "disabled", "always"]
)
def test_changed_or_untrusted_context_retains_prompt(experiment, change):
    a, _, p, google, cid = experiment
    remember(a, p, cid)
    p.experiment_enabled = True
    kwargs = {}
    if change == "scope":
        kwargs["scope"] = "openid extra"
    elif change == "redirect":
        kwargs["redirect"] = REDIRECT.replace("one", "two")
    elif change in {"name", "client"}:
        if change == "name":

            async def rename():
                client = await p.get_client(cid)
                await p._client_store.put(
                    key=cid, value=client.model_copy(update={"client_name": "ChatGPT"})
                )

            a.portal.call(rename)
        else:
            cid = a.post(
                "/register",
                json={
                    "client_name": "Synthetic trusted app",
                    "redirect_uris": [REDIRECT],
                    "scope": "openid extra",
                },
            ).json()["client_id"]
    elif change == "disabled":
        p.experiment_enabled = False
    elif change == "always":
        p._require_authorization_consent = True
    url = start(a, cid, **kwargs)
    if change == "resource":

        async def resource():
            txn_id = parse_qs(urlsplit(url).query)["txn_id"][0]
            txn = await p._transaction_store.get(key=txn_id)
            await p._transaction_store.put(
                key=txn_id, value=txn.model_copy(update={"resource": None})
            )

        a.portal.call(resource)
    assert page(a, url).status_code == 200
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize(
    "bad", ["absent", "tampered", "other-browser", "wrong-transaction"]
)
def test_auto_callback_keeps_per_transaction_browser_binding(experiment, bad):
    a, b, p, google, cid = experiment
    remember(a, p, cid)
    p.experiment_enabled = True
    automatic = page(a, start(a, cid))
    if bad == "other-browser":
        c = b
    else:
        c = a
        saved = list(a.cookies.jar)
        a.cookies.clear()
        if bad == "tampered":
            for x in saved:
                a.cookies.set(x.name, x.value + "bad", domain=x.domain, path=x.path)
        elif bad == "wrong-transaction":
            # An older valid binding cannot approve this new transaction.
            for x in saved:
                if "APPROVED_CLIENTS" in x.name:
                    a.cookies.set(x.name, x.value, domain=x.domain, path=x.path)
            older = page(a, start(a, cid))
            assert older.status_code == 302
    assert callback(c, automatic).status_code == 403
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("bad", ["csrf", "cookie", "context"])
def test_prompt_csrf_not_weakened_by_experiment(experiment, bad):
    a, _, p, google, cid = experiment
    p.experiment_enabled = True
    response = page(a, start(a, cid))
    fields = Fields(response.text).fields
    if bad == "cookie":
        a.cookies.clear()
    else:
        fields["csrf_token" if bad == "csrf" else "approval_context"] = "bad"
    assert a.post(
        str(response.url), data={**fields, "action": "approve"}
    ).status_code in (400, 403)
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("bad", ["pkce", "redirect", "client", "replay"])
def test_native_code_binding_and_replay_after_auto(experiment, bad):
    a, _, p, _, cid = experiment
    remember(a, p, cid)
    p.experiment_enabled = True
    automatic = page(a, start(a, cid))
    result = callback(a, automatic)
    code = code_from(result)
    changes = {
        "pkce": {"code_verifier": "bad"},
        "redirect": {"redirect_uri": "https://attacker.test/cb"},
        "client": {"client_id": "unknown"},
        "replay": {},
    }[bad]
    if bad == "replay":
        assert exchange(a, cid, code).status_code == 200
    assert exchange(a, cid, code, **changes).status_code in (400, 401)
    assert callback(a, automatic).status_code == 400


@pytest.mark.parametrize("bound_downstream", [True, False])
@pytest.mark.parametrize("forwarded", ["initial-authorize", "preapproval-consent"])
def test_forwarded_link_outcome_depends_on_downstream_browser_session(
    experiment, bound_downstream, forwarded
):
    a, b, p, google, cid = experiment
    # Victim/browser B previously approved exactly this client context.
    remember(b, p, cid)
    p.experiment_enabled = True
    params = {
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "openid",
        "state": "A-private-nonce",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": BASE + "/mcp",
    }
    initial = BASE + "/authorize?" + urlencode(params)
    # A can save/copy the entire initial URL BEFORE following any redirect.
    assert parse_qs(urlsplit(initial).query)["state"] == ["A-private-nonce"]
    a.cookies.set("downstream_session", "A", domain="mcp.example.test")
    link = (
        initial
        if forwarded == "initial-authorize"
        else a.get(initial).headers["location"]
    )
    response = b.get(link)
    if "/consent" in response.headers.get("location", ""):
        response = page(b, response.headers["location"])
    assert (
        response.status_code == 302
        and "accounts.google.com" in response.headers["location"]
    )
    # B legitimately gets its own new binding on auto-advance. A does not.
    assert callback(a, response).status_code == 403
    returned = callback(b, response)
    assert returned.status_code == 302
    assert parse_qs(urlsplit(returned.headers["location"]).query)["state"] == [
        "A-private-nonce"
    ]
    assert b.cookies.get("downstream_session") is None
    if bound_downstream:
        # Model a downstream that requires matching initiating browser session.
        # It rejects BEFORE code exchange/account attachment.
        attached = False
        assert b.cookies.get("downstream_session") != "A"
    else:
        # Deliberately permissive downstream: server-held state+PKCE alone.
        token = exchange(a, cid, code_from(returned))
        attached = token.status_code == 200
        assert (
            google.fetch_token.return_value["access_token"]
            == "synthetic-browser-B-google-grant"
        )
    assert attached is (not bound_downstream)
    google.fetch_token.assert_awaited_once()


@pytest.mark.parametrize("bad", [False, True])
def test_verified_cimd_private_key_jwt_still_required_after_auto(
    experiment, monkeypatch, bad
):
    a, _, p, _, _ = experiment
    cid, key, _ = install_cimd(p, monkeypatch)
    remember(a, p, cid)
    p.experiment_enabled = True
    result = callback(a, page(a, start(a, cid)))
    data = {
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    }
    if not bad:
        data["client_assertion"] = assertion(key, cid)
    assert exchange(a, cid, code_from(result), **data).status_code == (
        401 if bad else 200
    )


@pytest.mark.parametrize("credential", ["correct", "missing", "wrong"])
def test_confidential_secret_authentication_retained(experiment, credential):
    a, _, p, _, _ = experiment
    reg = a.post(
        "/register",
        json={
            "client_name": "Synthetic confidential client",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "client_secret_basic",
            "scope": "openid extra",
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert reg.status_code == 201
    # Pinned GoogleProvider intentionally registers public DCR clients even
    # when Basic is requested. Seed a confidential fixture record separately;
    # do not claim confidential DCR is its production policy.
    assert reg.json()["token_endpoint_auth_method"] == "none"
    assert not reg.json().get("client_secret")
    cid, secret = reg.json()["client_id"], "synthetic-confidential-secret"

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

    a.portal.call(seed_confidential)
    remember(a, p, cid)
    p.experiment_enabled = True
    result = callback(a, page(a, start(a, cid)))
    data = {
        "grant_type": "authorization_code",
        "code": code_from(result),
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    if credential == "missing":
        data["client_id"] = cid
        token = a.post("/token", data=data)
    else:
        token = a.post(
            "/token",
            data=data,
            auth=(cid, secret if credential == "correct" else "wrong"),
        )
    assert token.status_code == (200 if credential == "correct" else 401)


def test_changed_cimd_inline_key_loses_pinned_trust(experiment, monkeypatch):
    a, _, p, google, _ = experiment
    cid, _, doc = install_cimd(p, monkeypatch)
    remember(a, p, cid)
    p.experiment_enabled = True
    doc.jwks["keys"][0]["kid"] = "new-inline-key"
    assert page(a, start(a, cid)).status_code == 200
    google.fetch_token.assert_not_awaited()


def test_forged_cimd_domain_name_cannot_inherit_pinned_context(experiment, monkeypatch):
    a, _, p, google, _ = experiment
    cid, _, _ = install_cimd(p, monkeypatch)
    remember(a, p, cid)
    p.experiment_enabled = True
    forged, _, _ = install_cimd(
        p,
        monkeypatch,
        client_id="https://client.example.test.attacker.test/oauth.json",
        name="ChatGPT",
    )
    assert page(a, start(a, forged)).status_code == 200
    google.fetch_token.assert_not_awaited()
