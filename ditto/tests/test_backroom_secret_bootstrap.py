from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "apps" / "backroom" / "scripts" / "bootstrap-worker-secrets.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def test_bootstrap_streams_secrets_without_printing_them(tmp_path: Path) -> None:
    oauth = tmp_path / "oauth.json"
    oauth.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "oauth-client-id-sensitive",
                    "client_secret": "oauth-client-secret-sensitive",
                }
            }
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "gcloud",
        "#!/usr/bin/env bash\nprintf '%s' 'platform-admin-token-sensitive-and-long'\n",
    )
    _executable(
        fake_bin / "pnpm",
        """#!/usr/bin/env bash
set -euo pipefail
test "$1" = exec
test "$2" = wrangler
test "$3" = deploy
test "$4" = --secrets-file
python3 - "$5" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload["GOOGLE_CLIENT_ID"] == "oauth-client-id-sensitive"
assert payload["GOOGLE_CLIENT_SECRET"] == "oauth-client-secret-sensitive"
assert payload["DITTO_ADMIN_API_TOKEN"] == "platform-admin-token-sensitive-and-long"
assert payload["BACKROOM_ADMIN_EMAILS"] == "peyton@omniaura.ai"
assert len(payload["SESSION_SECRET"]) >= 32
PY
printf '%s\\n' 'mock deployment accepted encrypted bindings'
""",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "CLOUDFLARE_API_TOKEN": "cloudflare-token-sensitive",
        "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account",
        "BACKROOM_ADMIN_EMAILS": "peyton@omniaura.ai",
    }
    result = subprocess.run(
        [SCRIPT, oauth],
        cwd=SCRIPT.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    combined = result.stdout + result.stderr
    for secret in (
        "oauth-client-id-sensitive",
        "oauth-client-secret-sensitive",
        "platform-admin-token-sensitive-and-long",
        "cloudflare-token-sensitive",
    ):
        assert secret not in combined
    assert not list(tmp_path.glob("ditto-*-secrets.*"))
    assert not list(tmp_path.glob("ditto-platform-token.*"))
    assert not list(tmp_path.glob("ditto-session-secret.*"))
