"""Finite, operator-pinned personal experiment; absent config preserves normal UX.

Prior browser approval is not proof of current initiation. This deliberately
relaxes initiation consent, while retaining the native callback/token bindings.
"""

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.responses import RedirectResponse


CONFIG_ENV = "WORKSPACE_MCP_PERSONAL_AUTO_ADVANCE"


@dataclass(frozen=True)
class AutoAdvancePolicy:
    origin: str
    started_at: int
    expires_at: int
    contexts: frozenset[str]

    @classmethod
    def from_environment(cls):
        raw = os.getenv(CONFIG_ENV, "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
            if set(value) != {"origin", "started_at", "expires_at", "contexts"}:
                raise ValueError
            origin = value["origin"]
            parsed = urlsplit(origin)
            start, end = value["started_at"], value["expires_at"]
            contexts = value["contexts"]
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or type(start) is not int
                or type(end) is not int
                or not 0 < end - start <= 86400
                or not isinstance(contexts, list)
                or not 1 <= len(contexts) <= 4
                or any(
                    not isinstance(x, str) or re.fullmatch(r"[0-9a-f]{64}", x) is None
                    for x in contexts
                )
            ):
                raise ValueError
            _ = parsed.port  # Reject malformed authorities too.
            return cls(origin, start, end, frozenset(contexts))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise ValueError(
                f"{CONFIG_ENV} must contain an exact HTTPS origin, 1-4 SHA256 contexts and a <=24h integer epoch window"
            ) from None

    def active(self, issuer):
        return (
            self.origin == str(issuer).rstrip("/")
            and self.started_at <= time.time() < self.expires_at
        )


class FiniteAutoAdvanceMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._personal_auto_advance = AutoAdvancePolicy.from_environment()

    async def _handle_consent(self, request):
        policy = self._personal_auto_advance
        if (
            policy is not None
            and policy.active(self.issuer_url)
            and self._require_authorization_consent == "remember"
            and request.method == "GET"
        ):
            transaction, client, fingerprint = await self._consent_context(request)
            if (
                policy.active(self.issuer_url)
                and transaction is not None
                and client is not None
                and fingerprint in policy.contexts
                and "workspace-v1:" + fingerprint
                in self._decode_list_cookie(request, "MCP_APPROVED_CLIENTS")
            ):
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
