FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency management
RUN pip install --no-cache-dir uv

COPY . .

# Install Python dependencies using uv sync
# --extra otel ships the OpenTelemetry SDK/exporter so tracing can be enabled at
# runtime via OTEL_* env vars; it stays a no-op unless an OTLP endpoint is set.
RUN uv sync --frozen --no-dev --extra disk --extra otel

# ---------------------------------------------------------------------------
# Vendor patches to the `mcp` SDK inside site-packages. TEMPORARY — remove each
# one as its upstream fix lands in modelcontextprotocol/python-sdk.
#
# 1. register.py — DCR refuses to issue a client secret when the client asks for
#    token_endpoint_auth_method "none", but ChatGPT needs one. We issue a secret
#    and set the method to client_secret_basic so response and expectation agree.
# 2. client_auth.py + token.py — the SDK reads client_id from the form body and
#    raises "Missing client_id" before it ever looks at the Authorization header.
#    RFC 6749 Sec 2.3.1 allows a client using HTTP Basic to omit client_id from the
#    body, which is exactly what Codex does. Confirmed still present on
#    python-sdk main, 2026-08-18.
#
# Every patch asserts its anchor and exits non-zero if it is missing, so an SDK
# upgrade fails the build loudly. The previous version of this used `sed -i`,
# which silently no-ops when the pattern moves — that would have shipped an image
# with broken OAuth and no signal at all.
# ---------------------------------------------------------------------------
RUN /app/.venv/bin/python - <<'PY'
from pathlib import Path

register_path = Path("/app/.venv/lib/python3.11/site-packages/mcp/server/auth/handlers/register.py")
register_source = register_path.read_text()
register_old = '''        client_secret = None
        if client_metadata.token_endpoint_auth_method != "none":  # pragma: no branch
            # cryptographically secure random 32-byte hex string
            client_secret = secrets.token_hex(32)
'''
register_new = '''        # The compatibility secret must be paired with the auth method we
        # return, otherwise clients choose Basic while the server expects a
        # public form request.
        if client_metadata.token_endpoint_auth_method == "none":
            client_metadata.token_endpoint_auth_method = "client_secret_basic"

        # cryptographically secure random 32-byte hex string
        client_secret = secrets.token_hex(32)
'''
if register_old not in register_source:
    raise SystemExit("FastMCP registration patch anchor not found")
register_path.write_text(register_source.replace(register_old, register_new, 1))

auth_path = Path("/app/.venv/lib/python3.11/site-packages/mcp/server/auth/middleware/client_auth.py")
auth_source = auth_path.read_text()
auth_old = '''        form_data = await request.form()
        client_id = form_data.get("client_id")
        if not client_id:
            raise AuthenticationError("Missing client_id")

        client = await self.provider.get_client(str(client_id))
        if not client:
            raise AuthenticationError("Invalid client_id")  # pragma: no cover

        request_client_secret: str | None = None
        auth_header = request.headers.get("Authorization", "")
'''
auth_new = '''        form_data = await request.form()
        client_id = form_data.get("client_id")
        auth_header = request.headers.get("Authorization", "")
        basic_client_id: str | None = None
        basic_client_secret: str | None = None

        # RFC 6749 client_secret_basic carries both values in the Authorization
        # header, not in the form body. FastMCP 3.4.4 checked the body first.
        if auth_header.startswith("Basic "):
            try:
                encoded_credentials = auth_header[6:]
                decoded = base64.b64decode(encoded_credentials).decode("utf-8")
                if ":" in decoded:
                    raw_client_id, basic_client_secret = decoded.split(":", 1)
                    basic_client_id = unquote(raw_client_id)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                pass

        if not client_id:
            client_id = basic_client_id
        if not client_id:
            raise AuthenticationError("Missing client_id")

        client = await self.provider.get_client(str(client_id))
        if not client:
            raise AuthenticationError("Invalid client_id")  # pragma: no cover

        request_client_secret: str | None = None
'''
if auth_old not in auth_source:
    raise SystemExit("FastMCP client-auth patch anchor not found")
auth_source = auth_source.replace(auth_old, auth_new, 1)
auth_old = '''        if client.token_endpoint_auth_method == "client_secret_basic":
            if not auth_header.startswith("Basic "):
                raise AuthenticationError("Missing or invalid Basic authentication in Authorization header")

            try:
                encoded_credentials = auth_header[6:]  # Remove "Basic " prefix
                decoded = base64.b64decode(encoded_credentials).decode("utf-8")
                if ":" not in decoded:
                    raise ValueError("Invalid Basic auth format")
                basic_client_id, request_client_secret = decoded.split(":", 1)

                # URL-decode both parts per RFC 6749 Section 2.3.1
                basic_client_id = unquote(basic_client_id)
                request_client_secret = unquote(request_client_secret)

                if basic_client_id != client_id:
                    raise AuthenticationError("Client ID mismatch in Basic auth")
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid Basic authentication header")
'''
auth_new = '''        if client.token_endpoint_auth_method == "client_secret_basic":
            if basic_client_id is None or basic_client_secret is None:
                raise AuthenticationError("Missing or invalid Basic authentication in Authorization header")
            if basic_client_id != str(client_id):
                raise AuthenticationError("Client ID mismatch in Basic auth")
            request_client_secret = unquote(basic_client_secret)
'''
if auth_old not in auth_source:
    raise SystemExit("FastMCP Basic-auth patch anchor not found")
auth_source = auth_source.replace(auth_old, auth_new, 1)
auth_source = auth_source.replace(
    '''        elif client.token_endpoint_auth_method == "none":
            request_client_secret = None
''',
    '''        elif client.token_endpoint_auth_method == "none":
            # Accept old registrations created by the previous compatibility
            # patch, which stored a secret while still declaring "none".
            request_client_secret = basic_client_secret if basic_client_id else None
''',
    1,
)
auth_path.write_text(auth_source)

token_path = Path("/app/.venv/lib/python3.11/site-packages/mcp/server/auth/handlers/token.py")
token_source = token_path.read_text()
token_old = '''            form_data = await request.form()
            token_request = TokenRequest.model_validate(dict(form_data)).root
'''
token_new = '''            form_data = await request.form()
            form_values = dict(form_data)
            # client_secret_basic authenticates the client in HTTP Basic, so
            # the form may legitimately omit client_id.
            form_values.setdefault("client_id", client_info.client_id)
            token_request = TokenRequest.model_validate(form_values).root
'''
if token_old not in token_source:
    raise SystemExit("FastMCP token-handler patch anchor not found")
token_path.write_text(token_source.replace(token_old, token_new, 1))
PY

# Create non-root user for security

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

# Give read and write access to the store_creds volume
RUN mkdir -p /app/store_creds \
    && chown -R app:app /app/store_creds \
    && chmod 755 /app/store_creds

USER app

# Expose port (use default of 8000 if PORT not set)
EXPOSE 8000
# Expose additional port if PORT environment variable is set to a different value
ARG PORT
EXPOSE ${PORT:-8000}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

# Set environment variables for Python startup args
ENV TOOL_TIER=""
ENV TOOLS=""

# Use entrypoint for the base command and CMD for args
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["uv run main.py --transport streamable-http ${TOOL_TIER:+--tool-tier \"$TOOL_TIER\"} ${TOOLS:+--tools $TOOLS}"]
