# Backroom OAuth boundary

`backroom.dittobench.ai` is a separate OAuth relying party from the private
Ditto product Backroom. The Google web client must allow exactly this redirect:

```text
https://backroom.dittobench.ai/auth/callback
```

Reusing the private client's credentials is safe only if that redirect is added
without removing the existing `backroom.heyditto.ai` callback. A dedicated web
client is preferred because it permits independent rotation and deletion.

The Worker requires these server-side bindings:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `SESSION_SECRET` (at least 32 random characters)
- `BACKROOM_ADMIN_EMAILS` (comma-separated `@omniaura.ai` administrators)
- `DITTO_ADMIN_API_TOKEN` (the Platform admin bearer token)

Google's `hd` request hint is not authorization. The callback verifies the ID
token signature against Google's cached JWKS, restricts the algorithm to
`RS256`, and verifies audience, issuer, issued-at age, expiry, nonce, hosted
domain, verified email, and exact `@omniaura.ai` suffix. Verified domain members receive read access. Only email
addresses in `BACKROOM_ADMIN_EMAILS` receive write access. The Platform still
enforces its admin token on every operation and receives the signed-in email as
`X-Admin-Actor` for audit attribution.

Sessions are intentionally bounded to 12 hours. Removing an address from
`BACKROOM_ADMIN_EMAILS` revokes write access on its next request because the
binding is re-read every time. Disabling or deleting the underlying Google
Workspace account can leave its already-issued read-only session usable until
that 12-hour expiry; this is the accepted read-access revocation window. Clear
the session cookie at the edge when immediate read revocation is required.

Do not add `FIREBASE_API_KEY`, `DITTO_API_BASE_URL`, or the private
`/api/v5/admin/backroom-access` endpoint to this deployment.
