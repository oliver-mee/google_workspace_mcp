"""Normal provider native auth routes; only Google's HTTP boundary is mocked."""

import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request

from auth.auto_advance import AutoAdvancePolicy, CONFIG_ENV
from auth.google_oauth_provider import GoogleProvider
from tests.auth.test_oauth_consent import (
    BASE,
    REDIRECT,
    VERIFIER,
    Fields,
    approve,
    callback,
    native as native_fixture,
    page,
    start,
)


@pytest.fixture
def native(monkeypatch):
    yield from native_fixture.__wrapped__(monkeypatch)


def arm(c, p, cid, monkeypatch):
    url = start(c, cid)
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "query_string": urlsplit(url).query.encode(),
        }
    )
    fingerprint = c.portal.call(p._consent_context, req)[2]
    raw = {
        "origin": BASE,
        "started_at": int(time.time()) - 1,
        "expires_at": int(time.time()) + 3600,
        "contexts": [fingerprint],
    }
    monkeypatch.setenv(CONFIG_ENV, json.dumps(raw))
    p._personal_auto_advance = AutoAdvancePolicy.from_environment()
    assert approve(c, page(c, url)).status_code == 302
    return p._personal_auto_advance


def test_missing_configuration_leaves_normal_provider_unarmed(native, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    assert AutoAdvancePolicy.from_environment() is None
    c, p, _, cid = native
    assert p._personal_auto_advance is None
    assert approve(c, page(c, start(c, cid))).status_code == 302
    assert page(c, start(c, cid)).status_code == 200


@pytest.mark.parametrize(
    "mode",
    [
        "expired",
        "future",
        "wrong-origin",
        "wrong-context",
        "always",
        "missing-cookie",
        "tampered-cookie",
    ],
)
def test_runtime_fails_back_to_normal_prompt(native, monkeypatch, mode):
    c, p, google, cid = native
    policy = arm(c, p, cid, monkeypatch)
    if mode == "expired":
        p._personal_auto_advance = replace(policy, expires_at=int(time.time()) - 1)
    elif mode == "future":
        p._personal_auto_advance = replace(policy, started_at=int(time.time()) + 3600)
    elif mode == "wrong-origin":
        p._personal_auto_advance = replace(policy, origin="https://other.example.test")
    elif mode == "wrong-context":
        p._personal_auto_advance = replace(policy, contexts=frozenset({"a" * 64}))
    elif mode == "always":
        p._require_authorization_consent = True
    else:
        cookies = list(c.cookies.jar)
        c.cookies.clear()
        if mode == "tampered-cookie":
            for x in cookies:
                c.cookies.set(x.name, x.value + "bad", domain=x.domain, path=x.path)
    response = page(c, start(c, cid))
    assert response.status_code == 200 and "Continue with Google" in response.text
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize("site", ["cross-site", "none", None])
def test_runtime_auto_continues_with_native_callback_pkce_and_replay(
    native, monkeypatch, site
):
    c, p, google, cid = native
    arm(c, p, cid, monkeypatch)
    automatic = page(c, start(c, cid), site)
    assert automatic.status_code == 302
    saved = list(c.cookies.jar)
    c.cookies.clear()
    assert callback(c, automatic).status_code == 403
    google.fetch_token.assert_not_awaited()
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
        "code_verifier": VERIFIER,
    }
    assert c.post("/token", data=data).status_code == 200
    assert c.post("/token", data=data).status_code in (400, 401)
    assert callback(c, automatic).status_code == 400
    google.fetch_token.assert_awaited_once()


def test_unremembered_context_post_csrf_unchanged_when_runtime_armed(
    native, monkeypatch
):
    c, p, google, cid = native
    arm(c, p, cid, monkeypatch)
    response = page(c, start(c, cid, scope="openid extra"))
    assert response.status_code == 200
    fields = Fields(response.text).fields
    assert (
        c.post(
            str(response.url),
            data={**fields, "csrf_token": "forged", "action": "approve"},
        ).status_code
        == 400
    )
    google.fetch_token.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        {},
        {"origin": "https://example.test"},
        {"origin": BASE, "started_at": 1, "expires_at": 86402, "contexts": ["a" * 64]},
        {"origin": BASE, "started_at": 1, "expires_at": 2, "contexts": []},
        {"origin": BASE, "started_at": True, "expires_at": 2, "contexts": ["a" * 64]},
        {
            "origin": "https://user:password@example.test",
            "started_at": 1,
            "expires_at": 2,
            "contexts": ["a" * 64],
        },
    ],
)
def test_invalid_runtime_configuration_rejected(value, monkeypatch):
    monkeypatch.setenv(CONFIG_ENV, json.dumps(value))
    with pytest.raises(ValueError, match=CONFIG_ENV):
        AutoAdvancePolicy.from_environment()


def test_environment_policy_loaded_by_real_provider_constructor(monkeypatch):
    monkeypatch.setenv(
        CONFIG_ENV,
        json.dumps(
            {"origin": BASE, "started_at": 1, "expires_at": 2, "contexts": ["a" * 64]}
        ),
    )
    p = GoogleProvider(
        client_id="synthetic",
        client_secret="synthetic",
        base_url=BASE,
        jwt_signing_key="synthetic",
    )
    assert p._personal_auto_advance.contexts == frozenset({"a" * 64})
    assert not p._personal_auto_advance.active(BASE)
