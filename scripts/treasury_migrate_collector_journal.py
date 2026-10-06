#!/usr/bin/env python3
"""Copy a quiesced collector history under an exact offline cold approval.

No cloud/chain client, key loading, new journal initialization or overwrite.
Both policies and the migration manifest must be approved by the same coldkey.
"""

import argparse
import json
from pathlib import Path

from ditto.treasury.collector_chain import load_policy
from ditto.treasury.collector_migration import migrate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-policy", type=Path, required=True)
    parser.add_argument("--old-policy-digest", required=True)
    parser.add_argument("--new-policy", type=Path, required=True)
    parser.add_argument("--new-policy-digest", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    args = parser.parse_args()
    old = load_policy(args.old_policy, args.old_policy_digest)
    new = load_policy(args.new_policy, args.new_policy_digest)
    # Only public manifest and cold signature. Unknown envelope fields do not
    # enter authority; the manifest itself must equal the reconstructed one.
    approval = json.loads(args.approval.read_text())
    result = migrate(
        args.source, args.target, old, new, approval["manifest"], approval["signature"]
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
