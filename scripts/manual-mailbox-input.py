#!/usr/bin/env python3
"""Credential-free typed intent for the protected custody plan."""

import json
import sys
from pathlib import Path

if len(sys.argv) != 3 or sys.argv[1] not in {"true", "false"}:
    raise SystemExit("explicit mailbox boolean and output path required")
enabled = sys.argv[1] == "true"
Path(sys.argv[2]).write_text(
    json.dumps(
        {
            "enable_manual_mailbox": enabled,
            "manual_mailbox_platform_service_account": (
                "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
            )
            if enabled
            else "",
        }
    )
)
