# Isolated remembered auto-advance experiment

The diagnostic harness is **test-only**. A separate finite runtime adapter now
supports an explicitly configured operator experiment; it is off by default.
Scopes, stores and the normal same-origin remembered policy are unchanged when
not configured. Runtime use requires review of that adapter and an operator
choice accepting the initiation-consent relaxation described below.

## Question and narrow policy

Can a previously approving browser continue automatically while preserving the
proxy's callback binding, and what happens when an initiation link is forwarded?

`tests/auth/personal_auto_advance.py` contains a disabled-by-default subclass. The
fixture must explicitly enable it AND pin the complete context fingerprint. That
fingerprint includes the resolved client model, exact redirect, scopes and
resource; a valid signed browser approval for that same fingerprint is also
required. Names, domains, badges, public DCR, ordinary PKCE and Google account
eligibility do not establish present user intent. New/changed/unremembered
contexts retain the reviewed page. Remote JWKS contents behind an unchanged URL
are not fingerprinted; ordinary client-assertion signature validation remains.

For an eligible GET, the experiment mirrors only FastMCP4.0.10's remembered branch:
issue a fresh per-transaction consent token, save it and set the inherited signed
browser-binding cookie before redirecting to Google. It does not rewrite fetch
headers, submit a form in JavaScript, select false/external mode, patch dependency
sources or change the token endpoint. The POST path still uses the reviewed
signed form context and upstream double-submit CSRF checks. State, PKCE, callback
binding, code/redirect/client checks, confidential secrets and private_key_jwt
remain enforced through native routes.

**This intentionally relaxes the cross-site initiation/consent safeguard.** A
signed remembered cookie is prior approval, not proof that the browser initiated
this particular transaction. Preserving manual POST CSRF and callback binding
must not be described as preserving all initiation-CSRF protection. There is no
new transaction-specific anti-CSRF proof in the automatic GET. This experiment
measures that boundary; it is not a security-equivalent replacement for the live
policy and does not recommend unconditional first-visit auto-advance.

## Forwarded-link result and threat model

1. Browser A creates a downstream transaction with a nonce/state and S256 PKCE.
   Its full initial authorize URL remains available BEFORE any redirect. Redirecting
   to Google does not conceal or invalidate that copy.
2. Browser B previously approved the exact pinned context. B opens A's copied
   initial URL (or the preapproval consent URL). The experiment can automatically
   continue B to Google and issue a NEW valid binding for B.
3. A cannot use B's callback: absent/wrong browser binding is rejected before
   Google's token boundary. B can complete its own callback. State still refers
   to A's original downstream transaction; that is what state is meant to preserve.
4. A downstream requiring the matching initiating-browser session rejects B's
   callback before downstream MCP code exchange/attachment. The proxy has already
   exchanged the mocked Google code at this point; no downstream attachment yet. A deliberately permissive downstream
   which relies only on server-held state and PKCE can exchange the code and attach
   B's synthetic grant to A's pending transaction. This is a negative control.

The proxy binds the callback to the browser continued to Google. It does not
prove which downstream account should receive that browser's grant. Neither a
copied URL nor its state alone gives someone the PKCE verifier or a confidential
client's private key. A generic attacker-controlled downstream can hold its own
verifier; an authentic downstream's behavior depends on its actual session checks.

The lab's native CIMD tests resolve synthetic authentic metadata and perform real
private_key_jwt verification, including missing-assertion rejection. That proves
the proxy still authenticates the client at exchange. It does **not** show that
ChatGPT accepts an account-link callback in a different browser/user session, or
that any attacker can sign as ChatGPT. No live ChatGPT exploit was demonstrated.

## Reproduce without real credentials

Use pinned dependencies and a fresh HOME/credential directory:

```sh
uv run --frozen pytest -q tests/auth/test_personal_auto_advance_experiment.py
uv run --frozen python3 -m tests.auth.auto_advance_browser_harness
```

The harness binds **127.0.0.1:18087 only**, has MemoryStore, synthetic Google
credentials and a mocked Google token boundary. No production env/store/keys are
read. Its AS origin is localhost; downstream and mocked Google use127.0.0.1, so
real Chromium generates cross-site headers. The handler exposes a deliberately
session-less negative control solely in this disposable lab. No real grant,
public callback, DNS, Cloud client or Google data operation is required.

Use two isolated unattended browser sessions. Bootstrap B once through the normal
page at `http://127.0.0.1:18087/experiment/client/start`. A then opens the same start
page and copies its Start connection href BEFORE clicking. Open that URL in B.
The mock-Google page supplies a callback link: opening it in A is rejected;
completing in B reaches a downstream browser-session rejection. Repeat A's start
with `?binding=omitted` to observe the deliberately permissive negative control.
A new genuine B start succeeds with its own session. `/experiment/evidence`
contains sanitized event/status/header evidence, never bearer tokens. Stop the
listener and close both named sessions afterwards; MemoryStore is discarded.

Tests also cover absent/tampered approvals, disabled/always policy, changed
client/name/inline-key/redirect/scopes/resource, forged CIMD domain/name, POST CSRF,
wrong/missing/other-transaction binding, code/PKCE/client/redirect mismatch, replay,
public DCR, separately seeded confidential Basic records and CIMD/private_key_jwt.
Pinned GoogleProvider coerces DCR registrations to public clients even when Basic
is requested; the confidential fixture does not claim a different live DCR policy.

## Bounded human-only personal acceptance proposal

First test the downstream boundary with the existing reviewed personal service;
no automatic-continuation code or Google-client change is needed:

1. In browser A, logged into your own ChatGPT account, start a personal connector
   reconnect. Copy the initial MCP authorize URL before approving it. Keep A open.
2. Open that exact URL in a separate private browser B using only your own personal
   Google account. Complete the normal approval/Google step yourself. Do not send
   the URL to anyone else or log B into another person's ChatGPT account.
3. Record B's callback outcome (error, sign-in prompt, completed connection) and
   whether A's original ChatGPT session actually gains a usable connection. A Google
   success page alone is insufficient. If B asks for another ChatGPT login, stop and
   record it instead of changing sessions to make the test pass.
4. Complete an ordinary reconnect in A and one bounded read to leave the connector
   usable. Do not delete OAuth stores, keys or existing Google grants.

A personal automatic-continuation window would require **separately reviewed
runtime wiring**, an explicit personal-only off-by-default toggle, a finite expiry,
an operator-pinned actual ChatGPT client/redirect/scope/resource context and a
prior matching browser approval. Retain the reviewed image/config rollback and
keep work unchanged. Do not infer an actual identity/pin from this synthetic DCR
fixture. Unknown or changed contexts must prompt. After review, a short window
could repeat A/B above and compare a genuine same-browser run, then turn the
experiment off without resetting stores. The finite runtime adapter below provides the separate operator configuration;
no human/live-ChatGPT flow is executed by the diagnostic harness.

Recommendation: keep the reviewed live policy while this evidence is reviewed.
Automatic continuation cannot both accept arbitrary forwarded initial links and
prove current user intent solely from remembered client metadata/cookies. If
actual ChatGPT rejects mismatched-browser attachment, that supplies evidence for
its downstream boundary, not a universal guarantee or a property of redirecting
quickly. Any later personal relaxation should name and accept its remaining
initiation risk explicitly.

## Finite runtime adapter

`auth/auto_advance.py` implements the same remembered-context branch around the
normal provider. `WORKSPACE_MCP_PERSONAL_AUTO_ADVANCE` is absent by default. An
explicit JSON object must contain exactly `origin`, `started_at`, `expires_at`
and `contexts`: an exact HTTPS issuer origin, integer Unix seconds with a
positive window of at most24hours, and1-4 lowercase SHA256 context fingerprints.
Invalid configuration rejects startup. Future/expired windows, a different issuer,
non-remember consent mode and unmatched/missing signed approvals all fall back
to the normal reviewed page. Expiry is checked on every request and again after
context resolution. Already-started callbacks retain their ordinary transaction
lifetime; expiry stops new automatic continuations rather than revoking grants.

Obtain the pin from the actual native resolved client and a real validated
transaction's exact redirect/scopes/resource, not a guessed client name/domain.
Keep activation and metadata snapshots host-private. Copying a personal-origin
configuration to a different issuer cannot activate the branch there. Removing
the config and recreating the personal service disables it without resetting
OAuth stores, keys or grants. Stable image/config rollback remains required.
This flag deliberately relaxes cross-site initiation consent only for that
finite pinned remembered context; it does not establish current browser intent.
