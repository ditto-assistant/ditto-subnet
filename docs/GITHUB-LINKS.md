# Verified GitHub Account Links (`ditto link-github`)

Miners and bounty contributors can cryptographically bind their Bittensor hotkey to a verified GitHub account using `uv run ditto link-github`.

## How It Works

1. **Miner Session Proof**: `uv run ditto login` establishes an authenticated miner session proving control of the hotkey.
2. **Server-Side OAuth Round Trip**: `uv run ditto link-github` opens the GitHub OAuth consent flow on `dittobench.ai`. Platform exchanges the authorization code server-side, reads the numeric `id` and `login` from `GET /user`, and immediately discards the OAuth access token without logging or persisting it.
3. **Hotkey / Coldkey Attestation**: The CLI displays the verified numeric `github_user_id` and `github_login` and prompts the miner to sign:
   ```
   ditto-github-link:v1:<netuid>:<hotkey>:<github_user_id>:<github_login>:<link_attempt_id>:<nonce>:<issued_at>:<key_kind>:<signer>
   ```
4. **Append-Only Storage & 1:1 Uniqueness**: Platform verifies the signature via `verify_signature` and stores the link append-only in `miner_github_links`, enforcing partial unique indexes (`WHERE revoked_at IS NULL`) on both `hotkey` and numeric `github_user_id`.

## Subcommands

- `uv run ditto link-github`: Complete OAuth round trip and sign the attestation.
- `uv run ditto link-github status`: Display the active verified GitHub link for this hotkey.
- `uv run ditto link-github unlink`: Sign a revocation payload (`ditto-github-unlink:v1:...`) to set `revoked_at` while preserving the historical row.
