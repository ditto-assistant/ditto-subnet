"""Native v2 adapter to the separately provisioned key-custody executable."""

from __future__ import annotations

import base64
import time
from dataclasses import asdict
from pathlib import Path

from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    private_directory,
    protected_helper,
    run_private_process,
)
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2UnwrapRequest,
    PrivateV2UnwrapResult,
)
from ditto.coding_hosted_private import AUTHORING_ROLES, GRADING_ROLES


class ProcessPrivateV2Unwrapper:
    def __init__(self, *, executable: Path, work_root: Path):
        protected_helper(executable)
        private_directory(work_root)
        self._executable, self._root = executable, work_root

    async def unwrap(self, request: PrivateV2UnwrapRequest) -> PrivateV2UnwrapResult:
        phase = request.phase
        remaining = request.expires_at_unix - time.time()
        if (
            request.schema != "dittobench-coding-private-v2-unwrap-v1"
            or phase not in {"authoring", "grading"}
            or request.audience != f"platform-{phase}"
            or remaining <= 0
            or request.role
            not in (AUTHORING_ROLES if phase == "authoring" else GRADING_ROLES)
            or (request.frozen_patch_sha256 is None) != (phase == "authoring")
        ):
            raise HostedRuntimeError("native unwrap authority invalid")
        body = await run_private_process(
            (str(self._executable),),
            root=self._root,
            body=canonical(asdict(request)),
            timeout=min(20, remaining),
            maximum_output=1024,
            shutdown_grace=1,
        )
        raw = _decode_json_document(body, maximum_bytes=1024)
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "dittobench-coding-private-v2-unwrap-result-v1"
            or raw.get("request_sha256") != request.digest()
            or raw.get("weight_eligible") is not False
            or type(raw.get("data_key_b64")) is not str
        ):
            raise HostedRuntimeError("native unwrap response invalid")
        key = base64.b64decode(raw["data_key_b64"], validate=True)
        if len(key) != 32 or time.time() >= request.expires_at_unix:
            raise HostedRuntimeError("native unwrap response expired or invalid")
        return PrivateV2UnwrapResult(request.digest(), key)

    def __repr__(self) -> str:
        return "ProcessPrivateV2Unwrapper(private=True)"
