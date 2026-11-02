# Historical auto-advance diagnostic

The finite pinned runtime trial has been retired. Current operator configuration
is documented in [Automatic continuation](oauth-approval.md#automatic-continuation).
The test-only lab below remains useful for investigating the downstream boundary;
it is not imported by application entrypoints and changes no deployment policy.

## Forwarded-link boundary

`tests/auth/personal_auto_advance.py` models the older remembered-only experiment.
Its opt-in and pin are LAB settings, not the current runtime configuration.
`tests/auth/auto_advance_browser_harness.py` supplies native HTTP routes, two
independent browser contexts, MemoryStore and mocked Google token boundaries.

An initial authorize URL can be copied BEFORE a redirect and opened in another
browser. That browser can receive its own valid proxy callback-binding cookie.
The proxy binds Google's callback to that browser, not to a downstream user account.
A downstream requiring the initiating browser session rejects account attachment
before the DOWNSTREAM MCP code exchange. The mocked upstream Google token exchange
has already happened at the proxy callback. A deliberately permissive downstream
negative control can attach the synthetic grant. Genuine same-browser flows pass.

PKCE/state/client authentication/callback binding do not establish which person
initiated the downstream transaction. These generic tests do NOT show that actual
ChatGPT accepts a forwarded callback across users/browsers or that an attacker can
sign as ChatGPT. No live ChatGPT exploit has been demonstrated.

## Reproduce without real credentials

Use the pinned environment and isolated HOME/credential directories:

```sh
uv run --frozen pytest -q tests/auth/test_personal_auto_advance_experiment.py
uv run --frozen python3 -m tests.auth.auto_advance_browser_harness
```

The harness binds 127.0.0.1:18087 only; no production store/config is read. Use two
isolated unattended browser sessions. Bootstrap B through the normal page at
http://127.0.0.1:18087/experiment/client/start. A copies its Start connection link
before navigating; B opens it. A's attempt to use B's Google callback is rejected.
Completing in B reaches the session-bound downstream rejection. Repeat with
`?binding=omitted` for the permissive negative control; a genuine B start succeeds.
`/experiment/evidence` exposes sanitized statuses, not tokens. Stop the listener
and close both named browser sessions after the check; discard MemoryStore.

Public DCR is a public-client path even if confidential auth was requested at
registration. Separately seeded confidential Basic and synthetic CIMD/private_key_jwt
regressions test real secret/signature enforcement without claiming different DCR
policy. No Google data writes, new Cloud callbacks or real-account consent are
needed for the diagnostic. Live client acceptance remains separate evidence.
