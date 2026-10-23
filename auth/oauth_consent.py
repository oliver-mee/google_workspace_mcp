"""Compact consent presentation around FastMCP's unchanged CSRF/callback machinery.

Private hooks are deliberately isolated here and covered against the pinned SDK.
No installed dependency is modified. Unknown upstream HTML fails closed.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
from contextvars import ContextVar
from html.parser import HTMLParser
from functools import lru_cache
from pathlib import Path
from ipaddress import ip_address
from urllib.parse import urlsplit

import idna

from starlette.requests import Request
from starlette.responses import HTMLResponse


_approval_context: ContextVar[str | None] = ContextVar("approval_context", default=None)


class ConsentForm(HTMLParser):
    """Recognize only the upstream form contract we know how to preserve."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.forms = 0
        self.valid = True
        self.inside = False
        self.actions = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms += 1
            self.inside = True
            self.valid &= (
                attrs.get("method", "").lower() == "post"
                and attrs.get("action", "") == ""
                and attrs.get("id") == "consentForm"
            )
        if self.inside and tag == "input":
            name = attrs.get("name")
            if attrs.get("type") != "hidden" or name in self.fields:
                self.valid = False
            self.fields[name] = attrs.get("value", "")
        if self.inside and tag == "button" and attrs.get("name") == "action":
            self.actions.add(attrs.get("value"))

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False

    @property
    def recognized(self):
        return (
            self.valid
            and self.forms == 1
            and set(self.fields) == {"txn_id", "csrf_token", "submit"}
            and self.actions == {"approve", "deny"}
            and all(self.fields.values())
        )


_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f8fafd;color:#1f1f1f;
font:16px/1.5 Arial,sans-serif;min-height:100vh;display:grid;place-items:center;
padding:24px}main{width:100%;max-width:440px;background:#fff;
border:1px solid #e1e3e1;border-radius:16px;padding:40px}
.preface-mark{display:block;width:216px;max-width:100%;height:auto;margin-bottom:28px}
h1{font-size:28px;font-weight:400;line-height:1.25;letter-spacing:-.3px;
margin:0 0 20px}p{margin:0 0 12px;overflow-wrap:anywhere}strong{font-weight:600}
.destination{color:#444746;font-size:14px;margin-bottom:32px;overflow-wrap:anywhere}
.reminder{color:#444746;font-size:13px;margin:0 0 16px}
button{font:inherit;cursor:pointer;width:100%;min-height:44px}
button[value=approve]{display:flex;align-items:center;justify-content:center;
gap:10px;padding:0 12px;background:#fff;color:#1f1f1f;border:1px solid #747775;
border-radius:4px;font:500 14px/20px Arial,sans-serif;letter-spacing:.25px}
.google-g{display:block;flex:none;width:20px;height:20px}
button[value=approve]:hover{background:#f7f8f8;border-color:#5c5f5e}
button[value=deny]{background:transparent;border:0;border-radius:4px;color:#174ea6;
font-size:14px;font-weight:500;margin-top:8px}button[value=deny]:hover{background:#f1f5fe}
:focus-visible{outline:3px solid #1a73e8;outline-offset:4px}
::selection{background:#d3e3fd;color:#1f1f1f}
@media(max-width:480px){body{padding:16px}main{padding:28px 24px}}
"""
_CSP = (
    "default-src 'none'; style-src 'sha256-"
    + base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
    + "'; base-uri 'none'; frame-ancestors 'none'"
)


def destination_identity(redirect_uri: str) -> str:
    """Describe the validated return destination, without trusting app branding.

    Keep full ASCII/IDNA hosts (no ellipsis or registrable-domain shortening).
    Local and custom-scheme destinations must not look like verified websites.
    This is display only; redirect validation remains entirely upstream.
    """
    try:
        parsed = urlsplit(redirect_uri)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if not scheme:
            return "Return address unavailable"
        if host:
            try:
                address = ip_address(host)
            except ValueError:
                host = idna.encode(host, uts46=True).decode("ascii").lower()
                loopback = host.rstrip(".") == "localhost"
            else:
                loopback = address.is_loopback
                host = f"[{host}]" if address.version == 6 else host
            authority = host + (f":{parsed.port}" if parsed.port is not None else "")
        else:
            authority = ""
            loopback = False
        if scheme not in {"https", "http"}:
            return f"App link · {scheme}:" + (f"//{authority}" if authority else "")
        if not authority:
            return "Return address unavailable"
        if loopback:
            return f"Local app · {scheme}://{authority}"
        return authority if scheme == "https" else f"http://{authority}"
    except (ValueError, idna.IDNAError):
        return "Return address unavailable"


@lru_cache(maxsize=2)
def _bundled_svg(name):
    # Fixed, reviewed local files only. Never load client/operator-supplied URLs
    # or interpolate client data into SVG markup.
    if name not in {"google-g.svg", "preface-wordmark.svg"}:
        raise ValueError("Unknown bundled consent asset")
    svg = (Path(__file__).with_name("assets") / name).read_text()
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[1].lstrip()
    if name == "google-g.svg":
        return svg.replace(
            "<svg ", '<svg class="google-g" aria-hidden="true" focusable="false" ', 1
        )
    return svg.replace(
        "<svg ", '<svg class="preface-mark" role="img" aria-label="Preface" ', 1
    )


def compact_consent(fields, *, client, transaction, server_name, approval_context):
    """Only the next human action and validated app destination; no protocol UI."""
    esc = html.escape
    name = getattr(client, "client_name", None) or "this app"
    inputs = "".join(
        f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
        for key, value in {**fields, "approval_context": approval_context}.items()
    )
    destination = destination_identity(transaction.client_redirect_uri)
    operator_logo = (
        _bundled_svg("preface-wordmark.svg")
        if os.getenv("WORKSPACE_MCP_CONSENT_LOGO", "").strip() == "preface"
        else ""
    )
    google_icon = _bundled_svg("google-g.svg")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Workspace</title><style>{_STYLE}</style></head>
<body><main aria-labelledby="title">{operator_logo}
<h1 id="title">Google Workspace</h1>
<p>Connect your Google account to <strong>{esc(name)}</strong>.</p>
<p class="destination">For <bdi>{esc(destination)}</bdi></p>
<p class="reminder">Only continue if you started this connection.</p>
<form id="consentForm" method="post" action="">{inputs}
<button type="submit" name="action" value="approve">{google_icon}<span>Continue with Google</span></button>
<button type="submit" name="action" value="deny">Cancel</button></form>
</main></body></html>"""


class WorkspaceConsentMixin:
    """Presentation adapter and narrower, exact-context remembered approval.

    FastMCP still issues/validates CSRF, consent binding, PKCE, state, tokens and
    client authentication. Remembered approvals are not a cross-site bypass.
    """

    async def _consent_context(self, request):
        form = await request.form() if request.method == "POST" else None
        txn_id = (
            form.get("txn_id")
            if form is not None
            else request.query_params.get("txn_id")
        )
        transaction = await self._transaction_store.get(key=txn_id) if txn_id else None
        client = await self.get_client(transaction.client_id) if transaction else None
        if not transaction or not client:
            return transaction, client, None
        # Refresh timestamps are transport/cache data, not a client identity change.
        metadata = client.model_dump(mode="json", exclude={"cimd_fetched_at"})
        payload = {
            "client": metadata,
            "redirect": transaction.client_redirect_uri,  # retain query and path exactly
            "scopes": sorted(set(transaction.scopes)),
            "resource": transaction.resource,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return transaction, client, fingerprint

    def _make_client_key(self, client_id, redirect_uri):
        fingerprint = _approval_context.get()
        # Never accept old, weaker cookies or remember missing/unresolved clients.
        return "workspace-v1:" + (fingerprint or secrets.token_hex(32))

    async def _handle_consent(self, request: Request):
        transaction, client, fingerprint = await self._consent_context(request)
        token = _approval_context.set(fingerprint)
        try:
            if request.method == "POST":
                form = await request.form()
                expected = self._sign_cookie(
                    f"{fingerprint}:{form.get('txn_id')}:{form.get('csrf_token')}"
                )
                if not fingerprint or not hmac.compare_digest(
                    str(form.get("approval_context", "")), expected
                ):
                    return HTMLResponse(
                        "Connection changed or expired. Start again.", status_code=400
                    )
            # Fetch metadata is browser-enforced. Even 'none' (a pasted/phished
            # URL) and same-site (an untrusted sibling) must show the prompt.
            scope = dict(request.scope)
            if request.headers.get("sec-fetch-site") != "same-origin":
                scope["headers"] = [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() != b"sec-fetch-site"
                ] + [(b"sec-fetch-site", b"cross-site")]
            adapted = Request(scope, request.receive)
            # Preserve the parsed POST form without consuming the receive stream twice.
            if request.method == "POST":
                adapted._form = await request.form()
            response = await super()._handle_consent(adapted)
            if (
                request.method != "GET"
                or response.status_code != 200
                or not fingerprint
            ):
                return response
            parser = ConsentForm()
            parser.feed(response.body.decode())
            if not parser.recognized:
                return HTMLResponse(
                    "Approval page unavailable. Start again after the server is updated.",
                    status_code=503,
                )
            signed = self._sign_cookie(
                f"{fingerprint}:{parser.fields['txn_id']}:{parser.fields['csrf_token']}"
            )
            server = getattr(request.app.state, "fastmcp_server", None)
            response.body = compact_consent(
                parser.fields,
                client=client,
                transaction=transaction,
                server_name=getattr(server, "name", None),
                approval_context=signed,
            ).encode()
            response.headers["content-length"] = str(len(response.body))
            response.headers["content-security-policy"] = _CSP
            response.headers["cache-control"] = "no-store"
            response.headers["referrer-policy"] = "no-referrer"
            return response  # preserves every upstream Set-Cookie header
        finally:
            _approval_context.reset(token)
