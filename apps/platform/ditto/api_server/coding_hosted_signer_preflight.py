"""Deploy-time, metadata-only check of the hosted-v2 control signer placement.

``python -m ditto.api_server.coding_hosted_signer_preflight --check-metadata``
parses the signer settings from the environment ditto-api is about to start
with and applies the loader's own ancestor, directory, owner, mode, link and
size checks through ``lstat``. It never opens, reads or hashes the seed, so it
cannot prove the seed derives the configured hotkey: API startup still does that
and fails closed. It runs as the dedicated ``ditto-api`` user, the only owner
the checks accept: from a newly sealed release before ``scripts/update.sh``
activates it, and in ditto-platform-api.service's launcher before every start
(infra/docs/coding-hosted-control-signer-v2.md).
"""

from __future__ import annotations

import argparse
import sys

from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    private_file_metadata,
)
from ditto.api_server.coding_hosted_signer_config import (
    HostedControlSignerConfig,
    check_hosted_signer_config,
    parse_hosted_signer_config_from_env,
)
from ditto.api_server.errors import ApiServerConfigError

SEED_BYTES = 32
_UNSAFE = "hosted Coding signer seed placement is missing or unsafe"


def check_hosted_signer_seed_metadata(config: HostedControlSignerConfig) -> bool:
    """Return whether an enabled placement was checked; raise if it is unsafe."""
    check_hosted_signer_config(config)
    if not config.enabled:
        return False
    if config.seed_file is None:
        raise ApiServerConfigError(_UNSAFE)
    try:
        info = private_file_metadata(config.seed_file, SEED_BYTES)
    except (OSError, HostedRuntimeError):
        raise ApiServerConfigError(_UNSAFE) from None
    if info.st_size != SEED_BYTES:
        raise ApiServerConfigError(_UNSAFE)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ditto.api_server.coding_hosted_signer_preflight",
        description=(
            "Check hosted-v2 control signer settings and seed metadata "
            "without opening the seed."
        ),
    )
    parser.add_argument(
        "--check-metadata",
        action="store_true",
        required=True,
        help="the only mode: stat-only checks, the seed is never read",
    )
    parser.parse_args(argv)
    try:
        checked = check_hosted_signer_seed_metadata(
            parse_hosted_signer_config_from_env()
        )
    except ApiServerConfigError as error:
        # Fixed diagnostics only: no path, hotkey or file detail.
        print(f"hosted-v2 control signer preflight failed: {error}", file=sys.stderr)
        return 1
    if checked:
        print("hosted-v2 control signer seed metadata ok (seed not read)")
    else:
        print("hosted-v2 control signer disabled; seed path not inspected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
