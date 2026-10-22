"""Token-route compatibility for client IDs carried only in HTTP Basic.

Normalize before the supported SDK handler. Secret checking, PKCE, grant
validation and public-client policy remain owned by the provider. Dependency
source files are never modified.
"""

import base64
import binascii
from urllib.parse import unquote, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse


class BasicClientIDEndpoint:
    """ASGI adapter for the provider's existing /token route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        authorization = request.headers.get("authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic":
            return await self.app(scope, receive, send)
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            raw_id, _ = decoded.split(":", 1)
            client_id = unquote(raw_id)
            if not client_id:
                raise ValueError("Empty client ID")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return await self._invalid(scope, receive, send)
        form = await request.form()
        if "client_id" in form and form["client_id"] != client_id:
            return await self._invalid(scope, receive, send)
        values = list(form.multi_items())
        if "client_id" not in form:
            values.append(("client_id", client_id))
        body = urlencode(values).encode()
        normalized = dict(scope)
        normalized["headers"] = [
            (k, v)
            for k, v in scope["headers"]
            if k.lower() not in (b"content-length", b"content-type", b"authorization")
        ]
        # The pinned SDK requires this casing when it checks the Basic scheme.
        normalized["headers"] += [
            (b"content-length", str(len(body)).encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"authorization", f"Basic {encoded}".encode()),
        ]
        consumed = False

        async def normalized_receive():
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(normalized, normalized_receive, send)

    @staticmethod
    async def _invalid(scope, receive, send):
        response = JSONResponse(
            {"error": "invalid_client"},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
        return await response(scope, receive, send)
