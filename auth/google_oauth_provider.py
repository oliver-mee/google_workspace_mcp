"""Google OAuth defaults independent of protocol-level token requirements."""

from starlette.routing import Route

from auth.auto_advance import FiniteAutoAdvanceMixin
from auth.basic_client_auth import BasicClientIDEndpoint
from auth.oauth_consent import WorkspaceConsentMixin
from fastmcp.server.auth.providers.google import GoogleProvider as _GoogleProvider
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull


class GoogleProvider(FiniteAutoAdvanceMixin, WorkspaceConsentMixin, _GoogleProvider):
    def get_routes(self, mcp_path=None):
        routes = super().get_routes(mcp_path)
        return [
            Route(
                route.path,
                endpoint=BasicClientIDEndpoint(route.app),
                methods=route.methods,
                name=route.name,
                include_in_schema=route.include_in_schema,
            )
            if isinstance(route, Route) and route.path == "/token"
            else route
            for route in routes
        ]

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # FastMCP falls back to required_scopes for scope-less authorization,
        # even when DCR/CIMD defaults advertise the enabled Workspace scopes.
        # Keep bearer validation minimal while requesting the configured defaults.
        # An explicit scope selection must remain the client's choice.
        if not params.scopes:
            params = params.model_copy(
                update={"scopes": self._default_scope_str.split()}
            )
        return await super().authorize(client, params)
