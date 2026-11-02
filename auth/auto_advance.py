"""Operator-configured automatic continuation with native callback binding.

This removes explicit initiation confirmation, not OAuth protocol validation.
The setting is off by default; enable it only on the intended deployment.
"""

import os
import secrets

from mcp.shared.auth import InvalidRedirectUriError
from starlette.responses import HTMLResponse, RedirectResponse


CONFIG_ENV = "WORKSPACE_MCP_OAUTH_PROXY_AUTO_ADVANCE"


def auto_advance_enabled():
    raw = os.getenv(CONFIG_ENV, "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{CONFIG_ENV} must be true or false")
    return raw == "true"


class AutoAdvanceMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._auto_advance_enabled = auto_advance_enabled()

    async def _handle_consent(self, request):
        if (
            self._auto_advance_enabled
            and self._require_authorization_consent in (True, "remember")
            and request.method == "GET"
        ):
            transaction, client, _ = await self._consent_context(request)
            if transaction is not None and client is not None:
                # Recheck resolved metadata rather than trusting a stale client record.
                try:
                    client.validate_redirect_uri(transaction.client_redirect_uri)
                except InvalidRedirectUriError:
                    return HTMLResponse(
                        "Connection changed. Start again.", status_code=400
                    )
                txn_id = request.query_params["txn_id"]
                token = secrets.token_urlsafe(32)
                transaction.consent_token = token
                await self._transaction_store.put(
                    key=txn_id, value=transaction, ttl=15 * 60
                )
                response = RedirectResponse(
                    self._build_upstream_authorize_url(
                        txn_id, transaction.model_dump()
                    ),
                    status_code=302,
                )
                self._set_consent_binding_cookie(request, response, txn_id, token)
                return response
        return await super()._handle_consent(request)
