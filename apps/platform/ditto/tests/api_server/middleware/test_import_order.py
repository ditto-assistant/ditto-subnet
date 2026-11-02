"""Import order must not leave routing or error handling partially initialized."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "first", ["middleware.error_envelope", "endpoints.admin_ath_rulings"]
)
def test_court_and_error_handlers_import_in_fresh_process(first: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import ditto.api_server.{first}; "
            "from ditto.api_server.middleware.error_envelope "
            "import ERROR_CODE_HTTP_EXCEPTION, envelope_response; "
            "from ditto.api_server.endpoints import scoring, validator; "
            "assert envelope_response(503, ERROR_CODE_HTTP_EXCEPTION, 'test')"
            ".status_code == 503",
        ],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
