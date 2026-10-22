"""HTTP OAuth probes against pinned providers, without Google or real state."""

import base64
import hashlib
import time
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import (
    GoogleProvider as UpstreamGoogleProvider,
)
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthToken
from starlette.testclient import TestClient

from auth.google_oauth_provider import GoogleProvider


def provider(kind=GoogleProvider):
    return kind(
        client_id="synthetic-google-client",
        client_secret="synthetic-google-secret",
        base_url="https://mcp.example.test",
        jwt_signing_key="synthetic-test-key",
        client_storage=MemoryStore(),
        required_scopes=["openid"],
        valid_scopes=["openid"],
        require_authorization_consent=False,
    )


def register(client, method="none"):
    response = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9876/callback"],
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid",
        },
    )
    assert response.status_code == 201, response.text
    value = response.json()
    # FastMCP 4's supported downstream clients are public, even when Basic is requested.
    assert value["token_endpoint_auth_method"] == "none"
    assert not value.get("client_secret")
    return value["client_id"]


def seed_code(p, client_id):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(b"synthetic-verifier").digest())
        .decode()
        .rstrip("=")
    )
    p.load_authorization_code = AsyncMock(
        return_value=AuthorizationCode(
            code="synthetic-code",
            client_id=client_id,
            scopes=["openid"],
            expires_at=time.time() + 60,
            code_challenge=challenge,
            redirect_uri="http://localhost:9876/callback",
            redirect_uri_provided_explicitly=True,
        )
    )
    p.exchange_authorization_code = AsyncMock(
        return_value=OAuthToken(
            access_token="synthetic-access",
            token_type="Bearer",
            expires_in=60,
            scope="openid",
        )
    )


def code_data():
    return {
        "grant_type": "authorization_code",
        "code": "synthetic-code",
        "code_verifier": "synthetic-verifier",
        "redirect_uri": "http://localhost:9876/callback",
    }


@pytest.mark.parametrize(
    "method", ["none", "client_secret_basic", "client_secret_post"]
)
def test_dcr_and_public_code_exchange(method):
    p = provider()
    with TestClient(FastMCP("oauth-test", auth=p).http_app()) as client:
        client_id = register(client, method)
        seed_code(p, client_id)
        response = client.post("/token", data={**code_data(), "client_id": client_id})
        assert response.status_code == 200, response.text
        assert response.json()["access_token"] == "synthetic-access"
        p.exchange_authorization_code.assert_awaited_once()


@pytest.mark.parametrize(
    "kind,expected", [(UpstreamGoogleProvider, 401), (GoogleProvider, 200)]
)
def test_basic_header_without_body_client_id_regression(kind, expected):
    p = provider(kind)
    with TestClient(FastMCP("oauth-test", auth=p).http_app()) as client:
        client_id = register(client, "client_secret_basic")
        seed_code(p, client_id)
        response = client.post(
            "/token", data=code_data(), auth=(client_id, "ignored-for-public-client")
        )
        assert response.status_code == expected, response.text
        if expected == 401:
            assert "Missing client_id" in response.text
            p.exchange_authorization_code.assert_not_awaited()
        else:
            assert response.json()["access_token"] == "synthetic-access"


def test_basic_refresh_and_invalid_client_pkce_paths():
    p = provider()
    with TestClient(FastMCP("oauth-test", auth=p).http_app()) as client:
        client_id = register(client)
        seed_code(p, client_id)
        p.load_refresh_token = AsyncMock(
            return_value=RefreshToken(
                token="synthetic-refresh", client_id=client_id, scopes=["openid"]
            )
        )
        p.exchange_refresh_token = AsyncMock(
            return_value=OAuthToken(
                access_token="refreshed", token_type="Bearer", expires_in=60
            )
        )
        response = client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": "synthetic-refresh"},
            auth=(client_id, ""),
        )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"] == "refreshed"
        for auth, data in [
            ((client_id, ""), {**code_data(), "client_id": "different-client"}),
            (("unknown-client", ""), code_data()),
            ((client_id, ""), {**code_data(), "code_verifier": "wrong"}),
        ]:
            response = client.post("/token", data=data, auth=auth)
            assert response.status_code in (400, 401)
        p.exchange_authorization_code.assert_not_awaited()
        response = client.post(
            "/token", data=code_data(), headers={"Authorization": "Basic !!!!"}
        )
        assert response.status_code == 401


def test_confidential_client_still_checks_secret(monkeypatch):
    """Normalization also leaves the SDK's confidential-client checks intact."""
    from mcp.shared.auth import OAuthClientInformationFull

    p = provider()
    record = OAuthClientInformationFull(
        client_id="confidential",
        client_secret="correct",
        token_endpoint_auth_method="client_secret_basic",
        redirect_uris=["http://localhost:9876/callback"],
        grant_types=["authorization_code", "refresh_token"],
    )
    monkeypatch.setattr(p, "get_client", AsyncMock(return_value=record))
    seed_code(p, "confidential")
    with TestClient(FastMCP("oauth-test", auth=p).http_app()) as client:
        assert (
            client.post(
                "/token", data=code_data(), auth=("confidential", "wrong")
            ).status_code
            == 401
        )
        p.exchange_authorization_code.assert_not_awaited()
        response = client.post(
            "/token", data=code_data(), auth=("confidential", "correct")
        )
        assert response.status_code == 200, response.text
