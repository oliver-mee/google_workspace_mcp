"""Loopback-only disposable browser lab; no real provider/config/store is used.

Run with a fresh HOME: python -m tests.auth.auto_advance_browser_harness.
AS localhost:18087 and downstream 127.0.0.1:18087 are different browser sites.
The deliberately session-less downstream option is a negative control, NOT a
claim about ChatGPT. Do not run this harness on a public listening address.
"""

import base64
import hashlib
import html
import secrets
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.parse import urlencode, urlsplit

import httpx
import uvicorn
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from tests.auth.personal_auto_advance import PersonalAutoAdvanceExperiment


AS = "http://localhost:18087"
CLIENT = "http://127.0.0.1:18087"
REDIRECT = CLIENT + "/experiment/client/callback"


def create_lab():
    events, pending = [], {}
    client_id = None

    class ObservedExperiment(PersonalAutoAdvanceExperiment):
        async def _handle_consent(self, request):
            response = await super()._handle_consent(request)
            events.append(
                {
                    "event": "consent",
                    "fetch_site": request.headers.get("sec-fetch-site"),
                    "method": request.method,
                    "status": response.status_code,
                }
            )
            return response

    p = ObservedExperiment(
        client_id="synthetic-google",
        client_secret="synthetic-secret",
        jwt_signing_key="synthetic-lab-key",
        base_url=AS,
        redirect_path="/oauth2callback",
        client_storage=MemoryStore(),
        required_scopes=["openid"],
        valid_scopes=["openid"],
        require_authorization_consent="remember",
    )
    p.experiment_enabled = True
    google = AsyncMock()
    google.fetch_token.return_value = {
        "access_token": "synthetic-browser-Google-grant",
        "refresh_token": "synthetic-refresh",
        "expires_in": 3600,
        "scope": "openid",
        "token_type": "Bearer",
    }

    @asynccontextmanager
    async def google_boundary():
        yield google

    p._upstream_oauth_client = google_boundary
    p._build_upstream_authorize_url = lambda txn_id, txn: (
        CLIENT + "/experiment/google?" + urlencode({"state": txn_id})
    )
    server = FastMCP("Google Workspace · isolated experiment", auth=p)

    @server.custom_route("/experiment/client/start", methods=["GET"])
    async def client_start(request):
        nonlocal client_id
        async with httpx.AsyncClient(trust_env=False) as client:
            if client_id is None:
                registration = await client.post(
                    AS + "/register",
                    json={
                        "client_name": "Synthetic downstream (NOT ChatGPT)",
                        "redirect_uris": [REDIRECT],
                        "scope": "openid",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registration.status_code == 201
                client_id = registration.json()["client_id"]
            state, sid, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            binding = request.query_params.get("binding", "required") == "required"
            pending[state] = {"sid": sid, "verifier": verifier, "binding": binding}
            initial = (
                AS
                + "/authorize?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": REDIRECT,
                        "response_type": "code",
                        "scope": "openid",
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "resource": AS + "/mcp",
                    }
                )
            )
            # Operator fixture pinning of an exact native context, not a domain
            # allowlist or a claim that DCR/name proves real ChatGPT identity.
            probe = await client.get(initial, follow_redirects=False)
            context_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "headers": [],
                    "query_string": urlsplit(probe.headers["location"]).query.encode(),
                }
            )
            fingerprint = (await p._consent_context(context_request))[2]
            p.experiment_contexts = frozenset({fingerprint})
        result = HTMLResponse(
            "<h1>Synthetic downstream</h1><p>Browser-session check: "
            + ("required" if binding else "DELIBERATELY OMITTED negative control")
            + '</p><a id="start" href="'
            + html.escape(initial, quote=True)
            + '">Start connection</a><p>Link remains copyable BEFORE redirect.</p>'
        )
        result.set_cookie(
            "synthetic_downstream_session", sid, httponly=True, samesite="lax"
        )
        return result

    @server.custom_route("/experiment/google", methods=["GET"])
    async def mock_google(request):
        callback = (
            AS
            + "/oauth2callback?"
            + urlencode(
                {
                    "state": request.query_params["state"],
                    "code": "synthetic-google-code",
                }
            )
        )
        return HTMLResponse(
            '<h1>Mock Google — no real account</h1><a id="finish" href="'
            + html.escape(callback, quote=True)
            + '">Complete synthetic Google callback</a>'
        )

    @server.custom_route("/experiment/client/callback", methods=["GET"])
    async def client_callback(request):
        context = pending.get(request.query_params.get("state"))
        if context is None:
            return JSONResponse(
                {"status": "UNKNOWN_OR_REPLAYED_STATE"}, status_code=400
            )
        if (
            context["binding"]
            and request.cookies.get("synthetic_downstream_session") != context["sid"]
        ):
            events.append(
                {
                    "event": "downstream",
                    "status": "BROWSER_SESSION_REJECTED",
                    "token_exchange": False,
                }
            )
            return JSONResponse(
                {"status": "BROWSER_SESSION_REJECTED", "token_exchange": False},
                status_code=403,
            )
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                AS + "/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": request.query_params["code"],
                    "redirect_uri": REDIRECT,
                    "code_verifier": context["verifier"],
                },
            )
        if response.status_code == 200:
            del pending[request.query_params["state"]]
        result = {
            "event": "downstream",
            "status": "SYNTHETIC_ATTACHMENT"
            if response.status_code == 200
            else "TOKEN_REJECTED",
            "session_check": context["binding"],
            "token_status": response.status_code,
        }
        events.append(result)
        return JSONResponse(result, status_code=response.status_code)

    @server.custom_route("/experiment/evidence", methods=["GET"])
    async def evidence(request):
        return JSONResponse(
            {
                "events": events,
                "mock_google_exchanges": google.fetch_token.await_count,
                "real_Google": "NOT CALLED",
                "store": "MEMORY ONLY",
            }
        )

    return server.http_app()


if __name__ == "__main__":
    uvicorn.run(create_lab(), host="127.0.0.1", port=18087, log_level="warning")
