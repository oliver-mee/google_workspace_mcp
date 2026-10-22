# OAuth approval and client listing metadata

The compact approval page uses the configured server name, readable client name,
visible full return address and collapsed client ID/authentication/scopes. Client
names are explicitly labelled as client supplied. A server monogram avoids remote
image requests on this sensitive page; MCP initialize still advertises configured
icons and website. No JavaScript submits or approves the form.

## Consent policy and threat model

`WORKSPACE_MCP_OAUTH_PROXY_REQUIRE_CONSENT` accepts `always` and `remember`.
Unset keeps FastMCP's default explicit approval. `always` remains the recommended
policy for Internet-facing connections. Neither `false` nor `external` is accepted
by the environment configuration: both would remove FastMCP's consent/browser
binding protection.

In `remember` mode, an explicit approval records a signed, host-only browser
cookie. This adapter narrows the pinned FastMCP implementation:

- Only `Sec-Fetch-Site: same-origin` can use a remembered decision. Cross-site,
  same-site, `none` (including pasted links) and absent metadata still prompt.
- The decision fingerprint includes the current resolved client record (including
  CIMD metadata, authentication method, inline keys and JWKS URL), exact redirect including query,
  effective scope set and resource. Cache refresh timestamps are excluded.
- New or changed clients, metadata, inline keys, JWKS URLs, redirects, scopes or resources prompt.
  Missing/tampered cookies and older weaker decision keys cannot confer approval.
  Remote JWKS contents rotating behind an unchanged URL are not fingerprinted;
  token-endpoint signature verification still uses FastMCP’s normal JWKS handling.
- The rendered form carries a signed snapshot bound to its transaction and CSRF
  token. Metadata changes between rendering and submission require restarting.
  FastMCP still checks its CSRF store/double-submit cookies, issues the browser
  binding cookie, validates it at callback, and owns state, PKCE, code consumption,
  downstream client authentication and token refresh.

A remembered decision is a prior explicit browser approval of that exact context;
it is not an operator allowlist or a claim that the client initiated this browser
navigation. The same-origin condition assumes no hostile content, open redirect
or script execution on the authorization server's origin. Browser Fetch Metadata
is not authentication for arbitrary HTTP callers; the signed browser cookies are
also required. Compromised browsers/origins and stolen cookies are outside this
protection's boundary. Clearing browser cookies simply requires another approval.

### Why ChatGPT cannot safely skip every prompt

An attacker can start a connection in their **own ChatGPT session**, obtain its
legitimate authorization link and send that link to another user. The real ChatGPT
client can possess the PKCE verifier and later sign a valid `private_key_jwt`.
CIMD domain authenticity, correct redirect registration, a matching client name,
Google Internal eligibility and even authenticated ChatGPT transport identify
software/organization, not which user intended to connect that ChatGPT session.
Silently approving that link can deliver the victim's Google access to the
attacker's ChatGPT connection. Ordinary state binds the downstream session but
is not an independently authenticated browser-intent signal at this proxy.

Consequently there is **no cross-site ChatGPT preauthorization bypass** here.
Genuine cross-site ChatGPT arrivals continue to show the compact page even after
approval. Broader silent continuation would require a protocol establishing an
unforgeable initiating-browser/user binding, which has not been demonstrated.
Synthetic same-origin remembered-flow tests are not evidence of live ChatGPT
silent continuation.

OpenAI documents CIMD public-client and signed-assertion flows, with a transition
between singular and plural method metadata. This candidate keeps the pinned
FastMCP resolver/authenticator and tests real signature, audience, subject, expiry
and replay rejection using synthetic keys. It does not pin an assumed ChatGPT
client URL, redirect or name. Inspect the actual resolved metadata for a live
connection rather than treating synthetic fixtures as captured ChatGPT traffic.
See [OpenAI authentication documentation](https://developers.openai.com/plugins/build/auth).

## Adapter and migration

`auth/oauth_consent.py` is a local provider mixin. It delegates to the pinned
FastMCP 4.0.10 consent handler, recognizes its form, and replaces only the successful
HTML body while preserving all response cookies. Unknown form contracts fail
closed with HTTP 503; dependency updates require the native integration tests.
The response uses a hashed inline stylesheet CSP, no scripts or remote assets,
frame blocking, no-store and no-referrer. As in upstream, `form-action` is omitted
because browsers can enforce it across the Google/custom-scheme redirect chain;
the form posts to its current URL and redirects remain controlled by the server.
This compact page uses its fixed restrictive CSP rather than the SDK's optional
presentation override.

No dependency, OAuth store, signing key, profile or Google scope migration is
required. Existing issued access/refresh tokens and registrations are unaffected.
An approval form opened before this adapter was installed must be restarted;
older remembered decisions require one new explicit approval. Do not delete
credential stores to accomplish this. No new trust configuration is required.
A deployment and live browser/ChatGPT acceptance remain separate release gates.

## Source metadata and ChatGPT listing checklist

| Field | Source ownership/default | What an app owner should check |
|---|---|---|
| MCP server name | `WORKSPACE_MCP_BRAND_NAME`; default `Google Workspace` | Set a distinct operator/service name; compare native initialize and approval page |
| MCP server version | Installed `workspace-mcp` package metadata; `dev` only when package metadata is absent | Compare initialize to the installed release; there is no app-listing version override |
| MCP icons | `WORKSPACE_MCP_BRAND_ICON_URL`; absent when unset | Configure an operator-owned public HTTPS image; verify native initialize and client rendering |
| MCP website | `WORKSPACE_MCP_BRAND_WEBSITE_URL`; absent when unset | Configure the actual operator/service website; a fork must not invent an operator URL |
| Listing name/description | Entered in ChatGPT app creation | Review app/connection settings; existing listing text may be independent of server metadata |
| Listing logo/name edits | ChatGPT app management, where available | Owner: Settings → Apps → the app → Manage; verify available controls |
| Developer, category, displayed app version, listing website | Ownership/mapping not established for every ChatGPT listing surface | Inspect supported app-management fields; do not assume MCP metadata populates them |
| Tool catalogue/snapshot | Registered MCP tools, with ChatGPT-side refresh | Refresh/scan through the available workspace app controls; review changed actions before enabling them |

A displayed app `1.0.0` does **not establish** that the server initialize version
is wrong. Its source has not been verified for that listing UI. Do not overwrite
the truthful package version or add speculative app-version fields to mask it.
Likewise, providing website/icon metadata does not prove a saved listing will
update automatically. No generic developer identity/category/operator website is
supplied by this fork. A readable server-name default can be supplied safely;
operator branding needs explicit configuration.

The [OpenAI custom MCP connection guide](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
documents creation metadata, optional owner name/logo edits and explicit tool
refresh. Available menus vary by account/workspace experience. This checklist
requires no OAuth reset. Reading/editing the ChatGPT UI and confirming its
result remain manual acceptance, separate from native MCP metadata tests.
