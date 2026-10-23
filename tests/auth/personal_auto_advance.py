"""ISOLATED EXPERIMENT ONLY: never imported by the application entrypoints.

This deliberately relaxes cross-site initiation consent for an operator-pinned
context previously approved in this browser. It does NOT establish current user
intent. A copied authorize link can still be opened in another browser. Keep the
negative downstream-session tests: this is not a production recommendation.
"""

import secrets

from starlette.responses import RedirectResponse

from auth.google_oauth_provider import GoogleProvider


class PersonalAutoAdvanceExperiment(GoogleProvider):
    """Disabled by default; no environment switch or production installation."""

    experiment_enabled = False
    experiment_contexts = frozenset()

    async def _handle_consent(self, request):
        if (
            self.experiment_enabled
            and self._require_authorization_consent == "remember"
            and request.method == "GET"
        ):
            transaction, client, fingerprint = await self._consent_context(request)
            approved = self._decode_list_cookie(request, "MCP_APPROVED_CLIENTS")
            if (
                transaction is not None
                and client is not None
                and fingerprint in self.experiment_contexts
                and "workspace-v1:" + fingerprint in approved
            ):
                # Mirror ONLY the pinned remembered-approval branch. The POST
                # path retains production signed-context/double-submit checks.
                # No fake Sec-Fetch-Site, auto-submit script, false/external,
                # source patching, or Google/network operation occurs here.
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
