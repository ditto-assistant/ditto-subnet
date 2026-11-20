"""Sealed Docker build in a Cathedral room, with a saved build receipt.

An opt-in alternative to the local ``docker buildx build`` (``SCREENER_BUILD_BACKEND
=cathedral``). The miner's tarball goes, byte for byte, into a sealed AMD SEV-SNP
room that runs one canonical build command (``build_command``). The screener
fetches the image archive back in parts, verifies every hash, and saves a
``cathedral-build-receipt-v1`` file that ``ctcli build verify`` can check offline.

This module owns the HTTP flow and the receipt only. Loading the archive into the
local daemon and mapping outcomes to screening verdicts stay in ``gate.py``.

Failure model:
    * :class:`CathedralBuildRejected`: the Dockerfile build failed in the room
      (exit 91). The miner's fault, same as a local ``docker buildx build`` failure.
    * :class:`CathedralInfrastructureError`: anything else (HTTP, transport,
      timeout, malformed answer, hash mismatch). Never the miner's fault.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

RECIPE_TEMPLATE = "cathedral-docker-build-v1"
RESULT_SCHEMA = "cathedral-docker-build-result-v1"
RECEIPT_SCHEMA = "cathedral-build-receipt-v1"
EVIDENCE_SCHEMA = "cathedral_sandbox_evidence_v1"

# Prefix of the failure detail that the gate maps to RETRYABLE_INFRA. A local
# Docker failure detail never starts with it, so it cannot be forged by a build log.
INFRASTRUCTURE_DETAIL_PREFIX = "cathedral-build-infrastructure: "

BUILD_FAILED_EXIT_CODE = 91
BUILD_TIMED_OUT_EXIT_CODE = 92
DEFAULT_PART_BYTES = 64 << 20
MIN_PART_BYTES = 1 << 20
MAX_PART_BYTES = 2 << 30
# The API front limit is 1800 s; leave room for the room's own setup.
MAX_EXEC_TIMEOUT_SECONDS = 1750
# The in-room build limit; the exec gets this plus slack so the script can
# report its own time-out (exit 92) before the exec clock ends it.
MAX_BUILD_TIMEOUT_SECONDS = 1700
EXEC_SLACK_SECONDS = 45
_MAX_PARTS = 256
_MAX_EVIDENCE_PAGES = 10_000
_RESULT_MAX_BYTES = 65_536
_PART_NAME = re.compile(r"image\.tar\.part-[a-z]{3}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GET_ATTEMPTS = 4
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_SANDBOX_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class CathedralBuildRejected(Exception):
    """The Dockerfile build failed inside the room (miner-owned)."""

    def __init__(self, stdout: str) -> None:
        super().__init__("cathedral sealed build failed")
        self.stdout = stdout


class CathedralBuildTimedOut(Exception):
    """Ditto's own build time limit ran out in the room (miner-owned).

    Raised only when Cathedral could honour the configured limit in full.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"cathedral sealed build exceeded {limit}s")
        self.limit = limit


class CathedralLeaseExpired(Exception):
    """The screening lease ran out while the room was building (retryable)."""

    def __init__(self, elapsed: float) -> None:
        super().__init__(f"lease expired after {elapsed:g}s")
        self.elapsed = elapsed


class CathedralImageTooLarge(Exception):
    """The room reports an image archive over the screened-image cap (miner-owned)."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"image archive of {size} bytes exceeds the {cap} byte cap")
        self.size = size
        self.cap = cap


class CathedralInfrastructureError(Exception):
    """Cathedral, the transport, or the room failed; the archive was never judged."""


def build_command(
    source_sha256: str,
    *,
    memory: str = "7g",
    part_bytes: int = DEFAULT_PART_BYTES,
    build_timeout: int = MAX_BUILD_TIMEOUT_SECONDS,
) -> list[str]:
    """The canonical ``cathedral-docker-build-v1`` argv for one source hash.

    Pure and byte-identical to the reference producer: the offline checker
    recomputes this exact command from the recipe, so any change here breaks
    verification. Pinned by a golden hash test.
    """
    if len(source_sha256) != 64 or not all(
        c in "0123456789abcdef" for c in source_sha256
    ):
        raise ValueError("source_sha256 must be 64 lowercase hex characters")
    # Both are pasted into the shell line; a forged value could make the room
    # build nothing, so refuse anything outside the plain shapes.
    if not isinstance(memory, str) or not re.fullmatch(r"[1-9][0-9]{0,5}[mg]", memory):
        raise ValueError("memory must match ^[1-9][0-9]{0,5}[mg]$")
    if (
        isinstance(part_bytes, bool)
        or not isinstance(part_bytes, int)
        or not MIN_PART_BYTES <= part_bytes <= MAX_PART_BYTES
    ):
        raise ValueError("part_bytes must be an integer from 1 MiB to 2 GiB")
    if isinstance(build_timeout, bool) or not isinstance(build_timeout, int):
        raise ValueError("build_timeout must be an integer number of seconds")
    if build_timeout < 1:
        raise ValueError("build_timeout must be at least 1 second")
    tag = "cathedral-build:" + source_sha256[:24]
    lines = [
        "set -eu",
        "SRC=/work/in/src.tgz; OUT=/work/out; rm -rf $OUT; mkdir -p $OUT",
        f"echo '{source_sha256}  /work/in/src.tgz' | sha256sum -c - >/dev/null",
        "export DOCKER_HOST=unix:///var/run/docker.sock",
        "i=0; until docker info >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 120 ] && { echo 'docker daemon not ready' >&2; exit 90; }; sleep 1; done",  # noqa: E501
        "set +e",
        "S=$(date +%s)",
        f"DOCKER_BUILDKIT=1 timeout {build_timeout} docker buildx build --load --iidfile $OUT/iid --network host --pull=false --provenance=false --sbom=false --memory {memory} --memory-swap {memory} --cpu-period 100000 --cpu-quota 200000 --shm-size 64m --ulimit nofile=1024:1024 -t {tag} -f Dockerfile - < $SRC > $OUT/build.log 2>&1",  # noqa: E501
        "rc=$?; set -e",
        f"if [ $rc -ne 0 ]; then tail -c 6000 $OUT/build.log; [ $(( $(date +%s) - S )) -ge {build_timeout} ] && exit 92; exit 91; fi",  # noqa: E501
        f"IMG=$(docker image inspect --format '{{{{.Id}}}}' {tag})",
        "docker image save --output $OUT/image.tar $IMG",
        "ASHA=$(sha256sum $OUT/image.tar | cut -d' ' -f1); ASIZE=$(stat -c %s $OUT/image.tar)",  # noqa: E501
        f"split -b {part_bytes} -a 3 $OUT/image.tar $OUT/image.tar.part-",
        "rm -f $OUT/image.tar",
        'PARTS=\'\'; for p in $OUT/image.tar.part-*; do s=$(sha256sum $p | cut -d\' \' -f1); n=$(stat -c %s $p); PARTS="$PARTS${PARTS:+,}{\\"name\\":\\"$(basename $p)\\",\\"sha256\\":\\"$s\\",\\"size\\":$n}"; done',  # noqa: E501
        "DV=$(docker version --format '{{.Server.Version}}'); BV=$(docker buildx version | cut -d' ' -f2)",  # noqa: E501
        f'printf \'{{"schema":"cathedral-docker-build-result-v1","source_sha256":"{source_sha256}","image_id":"%s","archive_sha256":"%s","archive_size":%s,"parts":[%s],"docker_version":"%s","buildx_version":"%s"}}\\n\' "$IMG" "$ASHA" "$ASIZE" "$PARTS" "$DV" "$BV" > $OUT/result.json',  # noqa: E501
        "cat $OUT/result.json",
    ]
    # env -i by absolute path: the room's or the request's environment (PATH,
    # DOCKER_HOST, BUILDX_*) cannot redirect the build. The checker recomputes
    # this exact argv.
    return [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME=/root",
        "/bin/sh",
        "-c",
        "\n".join(lines),
    ]


def idempotency_key(
    agent_id: object, source_sha256: str, builder_image: str, attempt_id: object
) -> str:
    """Create-room key: one attempt of one source never starts a second paid room."""
    material = "\n".join((str(agent_id), source_sha256, builder_image, str(attempt_id)))
    return hashlib.sha256(material.encode()).hexdigest()


def merge_evidence_pages(
    fetch: Callable[[int, int], dict[str, Any]], sandbox_id: str
) -> dict[str, Any]:
    """Merge the evidence pages into one bundle (ctcli ``merge_pages``, minimal copy).

    Each next page asks for Cathedral's receipts after the last one held and the
    guest receipts after those held, so receipts added while reading are never
    skipped or held twice.
    """

    def check(page: object) -> dict[str, Any]:
        if (
            not isinstance(page, dict)
            or page.get("schema") != EVIDENCE_SCHEMA
            or not isinstance(page.get("cathedral_receipts"), dict)
        ):
            raise CathedralInfrastructureError("evidence is not a sandbox bundle")
        if page.get("sandbox_id") != sandbox_id:
            raise CathedralInfrastructureError("evidence is for another sandbox")
        return page

    def guest_key(entry: object) -> object:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("hotel_receipt_b64"), str)
            and entry["hotel_receipt_b64"]
        ):
            return ("header", entry["hotel_receipt_b64"])
        return (
            "id",
            entry.get("receipt_id") if isinstance(entry, dict) else repr(entry),
        )

    bundle = check(fetch(0, 0))
    guest = bundle.get("guest_receipts")
    receipts = list(bundle["cathedral_receipts"].get("receipts") or [])
    guest_receipts = (
        list(guest.get("receipts") or []) if isinstance(guest, dict) else []
    )
    page = bundle
    for _ in range(_MAX_EVIDENCE_PAGES):
        page_guest = (
            page["guest_receipts"]
            if isinstance(page.get("guest_receipts"), dict)
            else {}
        )
        cathedral_next = page["cathedral_receipts"].get("next_after")
        if cathedral_next is None and page_guest.get("next_offset") is None:
            break
        last_n = (
            receipts[-1].get("n", 0)
            if receipts and isinstance(receipts[-1], dict)
            else 0
        )
        page = check(fetch(last_n, len(guest_receipts)))
        new = [
            r
            for r in page["cathedral_receipts"].get("receipts") or []
            if isinstance(r, dict) and isinstance(r.get("n"), int) and r["n"] > last_n
        ]
        page_guest = (
            page["guest_receipts"]
            if isinstance(page.get("guest_receipts"), dict)
            else {}
        )
        more = list(page_guest.get("receipts") or []) if isinstance(guest, dict) else []
        held = {guest_key(r) for r in guest_receipts}
        more = [r for r in more if guest_key(r) not in held]
        receipts += new
        guest_receipts += more
        if (
            not new
            and not more
            and (
                page["cathedral_receipts"].get("next_after") is not None
                or page_guest.get("next_offset") is not None
            )
        ):
            raise CathedralInfrastructureError("evidence pages did not move forward")
    else:
        raise CathedralInfrastructureError("evidence has too many pages")
    merged = dict(bundle)
    merged["cathedral_receipts"] = {
        **bundle["cathedral_receipts"],
        "receipts": receipts,
        "after": 0,
        "next_after": None,
        "head": page["cathedral_receipts"].get("head"),
    }
    if isinstance(guest, dict):
        last_guest = (
            page["guest_receipts"]
            if isinstance(page.get("guest_receipts"), dict)
            else guest
        )
        merged["guest_receipts"] = {
            **guest,
            "receipts": guest_receipts,
            "offset": 0,
            "next_offset": None,
            "count": max(int(last_guest.get("count") or 0), len(guest_receipts)),
        }
    merged["complete"] = True
    return merged


@dataclass(frozen=True)
class CathedralBuildSettings:
    """What the builder needs from :class:`ditto_screener.config.ScreenerConfig`."""

    api_url: str
    api_key_file: str
    template: str
    builder_image: str
    builder_digest: str
    memory: str
    max_spend_usd: float
    receipt_dir: str
    lifetime_seconds: int = 2700


@dataclass(frozen=True)
class CathedralBuildResult:
    """A verified image archive on local disk (the caller deletes it)."""

    archive_path: str
    image_id: str
    sandbox_id: str
    receipt_path: str | None


class CathedralBuilder:
    """One sealed build per :meth:`build` call."""

    def __init__(
        self,
        settings: CathedralBuildSettings,
        *,
        client: httpx.AsyncClient | None = None,
        rooms: dict[str, str] | None = None,
        poll_interval: float = 3.0,
        ready_timeout: float = 360.0,
        delete_timeout: float = 180.0,
        part_bytes: int = DEFAULT_PART_BYTES,
        retry_delay: float = 2.0,
    ) -> None:
        self._settings = settings
        self._retry_delay = retry_delay
        self._client = client
        # Idempotency key -> room id, for this process (see the module notes in the PR).
        self._rooms = rooms if rooms is not None else {}
        self._poll_interval = poll_interval
        self._ready_timeout = ready_timeout
        self._delete_timeout = delete_timeout
        self._part_bytes = part_bytes
        self._limit_is_full = True
        self._build_limit = MAX_BUILD_TIMEOUT_SECONDS

    # -- HTTP ----------------------------------------------------------------

    def _read_key(self) -> str:
        try:
            key = Path(self._settings.api_key_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CathedralInfrastructureError(
                f"Cathedral API key file is unreadable ({type(error).__name__})"
            ) from error
        if not key:
            raise CathedralInfrastructureError("Cathedral API key file is empty")
        return key

    async def _call(
        self,
        client: httpx.AsyncClient,
        key: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        idempotency: str | None = None,
        timeout: float = 60.0,
    ) -> httpx.Response:
        if method in {"POST", "PUT", "DELETE"} and idempotency is None:
            # Cathedral refuses a write without one (HTTP 422).
            raise CathedralInfrastructureError(f"{method} needs an Idempotency-Key")
        headers = {"Authorization": f"Bearer {key}"}
        if idempotency is not None:
            headers["Idempotency-Key"] = idempotency
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        # A read is retried on a dropped connection or a gateway error, so one
        # network blip during a long build poll does not throw the build away.
        # Writes are not retried here: their Idempotency-Key makes a caller
        # retry safe, but that is the caller's decision.
        attempts = _GET_ATTEMPTS if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await client.request(
                    method,
                    self._settings.api_url + path,
                    headers=headers,
                    json=json_body,
                    content=content,
                    timeout=httpx.Timeout(timeout, connect=10.0),
                )
            except httpx.HTTPError as error:
                if attempt < attempts:
                    await asyncio.sleep(self._retry_delay * attempt)
                    continue
                # The exception text can carry the request; name only its type.
                raise CathedralInfrastructureError(
                    f"{method} {path.split('?')[0]} transport error "
                    f"({type(error).__name__})"
                ) from error
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                await asyncio.sleep(self._retry_delay * attempt)
                continue
            break
        if response.status_code >= 300:
            raise CathedralInfrastructureError(
                f"{method} {path.split('?')[0]} answered HTTP {response.status_code}"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            raise CathedralInfrastructureError(f"{what} did not answer JSON") from error
        if not isinstance(body, dict):
            raise CathedralInfrastructureError(f"{what} did not answer an object")
        return body

    # -- the build -----------------------------------------------------------

    async def build(
        self,
        tar_path: str,
        *,
        agent_id: object,
        attempt_id: object,
        timeout: float,
        max_archive_bytes: int,
        disk_reserve_bytes: int,
        lease_remaining: Callable[[], float | None] | None = None,
    ) -> CathedralBuildResult:
        """Build ``tar_path`` in a sealed room and return the verified archive.

        ``timeout`` is Ditto's own build limit (``build_timeout_seconds``). The
        room can honour at most :data:`MAX_BUILD_TIMEOUT_SECONDS` of it. The
        screening lease is not a cap: ``lease_remaining`` is read on each poll
        round, so a renewal extends the build, and a lapsed lease raises
        :class:`CathedralLeaseExpired` after the room is deleted.
        """
        source = await asyncio.to_thread(Path(tar_path).read_bytes)
        source_sha256 = hashlib.sha256(source).hexdigest()
        settings = self._settings
        limit = max(1, int(min(timeout, MAX_BUILD_TIMEOUT_SECONDS)))
        # Cathedral's cap, not Ditto's, ended the build when it is the smaller one.
        self._limit_is_full = timeout <= MAX_BUILD_TIMEOUT_SECONDS
        self._build_limit = limit
        command = build_command(
            source_sha256,
            memory=settings.memory,
            part_bytes=self._part_bytes,
            build_timeout=limit,
        )
        key = self._read_key()
        attempt_key = idempotency_key(
            agent_id, source_sha256, settings.builder_image, attempt_id
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        sandbox_id: str | None = None
        result_bytes: bytes | None = None
        archive_path: str | None = None
        try:
            if attempt_key in self._rooms:
                raise CathedralInfrastructureError(
                    "a room already exists for this attempt in this process; "
                    "not starting a second one"
                )
            sandbox_id = await self._create_room(client, key, attempt_key)
            self._rooms[attempt_key] = sandbox_id
            await self._wait_running(client, key, sandbox_id)
            await self._call(
                client,
                key,
                "PUT",
                f"/v1/sandboxes/{sandbox_id}/files?path=/work/in/src.tgz",
                content=source,
                idempotency=f"{attempt_key}-put",
                timeout=300.0,
            )
            await self._run_exec(
                client, key, sandbox_id, command, limit, attempt_key, lease_remaining
            )
            # Must be the next guest receipt after the exec: nothing in between.
            response = await self._call(
                client,
                key,
                "GET",
                f"/v1/sandboxes/{sandbox_id}/files?path=/work/out/result.json"
                f"&max_bytes={_RESULT_MAX_BYTES}",
                timeout=60.0,
            )
            result_bytes = response.content
            result = self._parse_result(result_bytes, source_sha256, max_archive_bytes)
            archive_path = await self._fetch_archive(
                client,
                key,
                sandbox_id,
                result,
                disk_reserve_bytes=disk_reserve_bytes,
            )
        except BaseException:
            if archive_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(archive_path)
            raise
        finally:
            receipt_path: str | None = None
            try:
                if sandbox_id is not None:
                    deleted = await self._cleanup(client, key, sandbox_id, attempt_key)
                    if deleted:
                        self._rooms.pop(attempt_key, None)
                    if result_bytes is not None:
                        receipt_path = await self._save_receipt(
                            client,
                            key,
                            sandbox_id,
                            source_sha256,
                            command,
                            result_bytes,
                        )
            finally:
                if owns_client:
                    await client.aclose()
        assert archive_path is not None and sandbox_id is not None
        return CathedralBuildResult(
            archive_path=archive_path,
            image_id=result["image_id"],
            sandbox_id=sandbox_id,
            receipt_path=receipt_path,
        )

    async def _create_room(
        self, client: httpx.AsyncClient, key: str, attempt_key: str
    ) -> str:
        settings = self._settings
        body = {
            "required_hardware": "amd_sev_snp",
            "template": settings.template,
            "image": settings.builder_image,
            "network": "internet",
            "lifetime_seconds": settings.lifetime_seconds,
            "max_spend_usd": settings.max_spend_usd,
        }
        response = await self._call(
            client,
            key,
            "POST",
            "/v1/sandboxes",
            json_body=body,
            idempotency=attempt_key,
            timeout=60.0,
        )
        sandbox_id = self._json(response, "create room").get("id")
        if not isinstance(sandbox_id, str) or not _SANDBOX_ID.fullmatch(sandbox_id):
            raise CathedralInfrastructureError("create room returned no usable id")
        return sandbox_id

    async def _wait_running(
        self, client: httpx.AsyncClient, key: str, sandbox_id: str
    ) -> None:
        give_up = time.monotonic() + self._ready_timeout
        while True:
            response = await self._call(
                client, key, "GET", f"/v1/sandboxes/{sandbox_id}", timeout=30.0
            )
            room = self._json(response, "room state")
            state = room.get("state")
            if state == "running":
                digest = room.get("image_digest")
                if digest != self._settings.builder_digest:
                    # A different builder than the one pinned: never build on it.
                    raise CathedralInfrastructureError(
                        f"room builder digest {digest!r} is not the pinned "
                        f"{self._settings.builder_digest}"
                    )
                return
            if state in {"failed", "deleted", "deleting"}:
                raise CathedralInfrastructureError(f"room ended before use ({state})")
            if time.monotonic() >= give_up:
                raise CathedralInfrastructureError("room did not become ready in time")
            await asyncio.sleep(self._poll_interval)

    async def _run_exec(
        self,
        client: httpx.AsyncClient,
        key: str,
        sandbox_id: str,
        command: list[str],
        limit: int,
        attempt_key: str,
        lease_remaining: Callable[[], float | None] | None = None,
    ) -> str:
        exec_timeout = min(limit + EXEC_SLACK_SECONDS, MAX_EXEC_TIMEOUT_SECONDS)
        response = await self._call(
            client,
            key,
            "POST",
            f"/v1/sandboxes/{sandbox_id}/execs",
            json_body={"command": command, "timeout_seconds": exec_timeout},
            idempotency=f"{attempt_key}-exec",
            timeout=60.0,
        )
        exec_id = self._json(response, "start exec").get("exec_id")
        if not isinstance(exec_id, str) or not _SANDBOX_ID.fullmatch(exec_id):
            raise CathedralInfrastructureError("start exec returned no usable id")
        # A little slack over the exec's own timeout so the room can report it.
        started = time.monotonic()
        give_up = started + exec_timeout + 60
        while True:
            # Each round, like the local build: a renewal moves the lease out.
            if lease_remaining is not None:
                left = lease_remaining()
                if left is not None and left <= 0:
                    raise CathedralLeaseExpired(round(time.monotonic() - started))
            response = await self._call(
                client,
                key,
                "GET",
                f"/v1/sandboxes/{sandbox_id}/execs/{quote(exec_id)}?wait=25",
                timeout=60.0,
            )
            status = self._json(response, "exec status")
            if status.get("state") != "running":
                break
            if time.monotonic() >= give_up:
                raise CathedralInfrastructureError("exec did not finish in time")
        exit_code = status.get("exit_code")
        stdout = status.get("stdout")
        stdout = stdout if isinstance(stdout, str) else ""
        if exit_code == 0:
            return stdout
        if exit_code == BUILD_FAILED_EXIT_CODE:
            raise CathedralBuildRejected(stdout)
        if exit_code == BUILD_TIMED_OUT_EXIT_CODE and self._limit_is_full:
            raise CathedralBuildTimedOut(self._build_limit)
        # Includes a timed-out exec (no exit code): Cathedral's clock, not the miner's.
        raise CathedralInfrastructureError(
            f"exec ended in state {status.get('state')!r} with exit code {exit_code!r}"
        )

    @staticmethod
    def _parse_result(
        raw: bytes, source_sha256: str, max_archive_bytes: int
    ) -> dict[str, Any]:
        try:
            result = json.loads(raw)
        except ValueError as error:
            raise CathedralInfrastructureError("result.json is not JSON") from error
        if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
            raise CathedralInfrastructureError("result.json has the wrong schema")
        if result.get("source_sha256") != source_sha256:
            raise CathedralInfrastructureError("result.json names another source")
        image_id = result.get("image_id")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise CathedralInfrastructureError("result.json image_id is malformed")
        archive_sha256 = result.get("archive_sha256")
        archive_size = result.get("archive_size")
        parts = result.get("parts")
        if (
            isinstance(archive_size, int)
            and not isinstance(archive_size, bool)
            and archive_size > max_archive_bytes
        ):
            # Same verdict as a local build whose export is over the cap.
            raise CathedralImageTooLarge(archive_size, max_archive_bytes)
        if (
            not isinstance(archive_sha256, str)
            or not _HEX64.fullmatch(archive_sha256)
            or not isinstance(archive_size, int)
            or isinstance(archive_size, bool)
            or not 0 < archive_size <= max_archive_bytes
            or not isinstance(parts, list)
            or not 0 < len(parts) <= _MAX_PARTS
        ):
            raise CathedralInfrastructureError(
                "result.json archive fields are malformed"
            )
        total = 0
        for part in parts:
            if (
                not isinstance(part, dict)
                or not isinstance(part.get("name"), str)
                or not _PART_NAME.fullmatch(part["name"])
                or not isinstance(part.get("sha256"), str)
                or not _HEX64.fullmatch(part["sha256"])
                or not isinstance(part.get("size"), int)
                or isinstance(part["size"], bool)
                or part["size"] <= 0
            ):
                raise CathedralInfrastructureError("result.json part is malformed")
            total += part["size"]
        if total != archive_size:
            raise CathedralInfrastructureError("result.json part sizes do not add up")
        return result

    async def _fetch_archive(
        self,
        client: httpx.AsyncClient,
        key: str,
        sandbox_id: str,
        result: dict[str, Any],
        *,
        disk_reserve_bytes: int,
    ) -> str:
        size = int(result["archive_size"])
        fd, path = tempfile.mkstemp(prefix="ditto-cathedral-image-", suffix=".tar")
        os.close(fd)
        try:
            free = shutil.disk_usage(Path(path).parent).free
            required = size * 2 + disk_reserve_bytes
            if free < required:
                raise CathedralInfrastructureError(
                    "insufficient temporary disk for the image archive "
                    f"(need {required} bytes, have {free})"
                )
            digest = hashlib.sha256()
            written = 0
            with open(path, "wb") as out:
                for part in result["parts"]:
                    response = await self._call(
                        client,
                        key,
                        "GET",
                        f"/v1/sandboxes/{sandbox_id}/files?path=/work/out/"
                        f"{part['name']}&max_bytes={part['size']}",
                        timeout=900.0,
                    )
                    data = response.content
                    if (
                        len(data) != part["size"]
                        or hashlib.sha256(data).hexdigest() != part["sha256"]
                    ):
                        raise CathedralInfrastructureError(
                            f"part {part['name']} does not match its hash"
                        )
                    digest.update(data)
                    out.write(data)
                    written += len(data)
            if written != size or digest.hexdigest() != result["archive_sha256"]:
                raise CathedralInfrastructureError(
                    "joined archive does not match archive_sha256"
                )
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return path

    # -- cleanup and receipt -------------------------------------------------

    async def _cleanup(
        self, client: httpx.AsyncClient, key: str, sandbox_id: str, attempt_key: str
    ) -> bool:
        """Delete the room and wait for ``deleted``. Never raises.

        The DELETE is re-sent (same Idempotency-Key) until the room reports
        ``deleted`` or the delete window ends: one gateway error must not leave
        a paid room running until its lifetime (seen on Cathedral prod).
        """
        path = f"/v1/sandboxes/{sandbox_id}"
        give_up = time.monotonic() + self._delete_timeout
        delete_accepted = False
        while True:
            if not delete_accepted:
                try:
                    await self._call(
                        client,
                        key,
                        "DELETE",
                        path,
                        idempotency=f"{attempt_key}-delete",
                        timeout=60.0,
                    )
                    delete_accepted = True
                except Exception as error:
                    logger.warning(
                        "cathedral room %s delete failed: %s", sandbox_id, error
                    )
            try:
                response = await self._call(client, key, "GET", path, timeout=30.0)
                state = self._json(response, "room state").get("state")
                if state == "deleted":
                    return True
            except CathedralInfrastructureError as error:
                logger.warning(
                    "cathedral room %s state check failed: %s", sandbox_id, error
                )
            if time.monotonic() >= give_up:
                logger.warning(
                    "cathedral room %s was not confirmed deleted", sandbox_id
                )
                return False
            await asyncio.sleep(self._poll_interval)

    async def _save_receipt(
        self,
        client: httpx.AsyncClient,
        key: str,
        sandbox_id: str,
        source_sha256: str,
        command: list[str],
        result_bytes: bytes,
    ) -> str | None:
        """Write the build receipt. A failure is logged, never raised."""
        settings = self._settings
        try:
            loop = asyncio.get_running_loop()

            async def fetch_page(after: int, offset: int) -> dict[str, Any]:
                response = await self._call(
                    client,
                    key,
                    "GET",
                    f"/v1/sandboxes/{sandbox_id}/evidence"
                    f"?after={after}&room_offset={offset}",
                    timeout=180.0,
                )
                return self._json(response, "evidence")

            # merge_evidence_pages is synchronous; drive the async fetch from a
            # worker thread so one event loop serves both.
            def fetch(after: int, offset: int) -> dict[str, Any]:
                return asyncio.run_coroutine_threadsafe(
                    fetch_page(after, offset), loop
                ).result()

            evidence = await asyncio.to_thread(merge_evidence_pages, fetch, sandbox_id)
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "recipe": {
                    "template": RECIPE_TEMPLATE,
                    "builder_image": settings.builder_image,
                    "builder_digest": settings.builder_digest,
                    "memory": settings.memory,
                    "part_bytes": self._part_bytes,
                    "build_timeout": self._build_limit,
                    "command": command,
                },
                "source_sha256": source_sha256,
                "result_json_b64": base64.b64encode(result_bytes).decode("ascii"),
                "result": json.loads(result_bytes),
                "sandbox_id": sandbox_id,
                "evidence": evidence,
            }
            directory = Path(settings.receipt_dir)
            directory.mkdir(parents=True, exist_ok=True)
            final = directory / f"{source_sha256}-{sandbox_id}.json"
            temp = directory / f".{final.name}.tmp"
            temp.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
            os.replace(temp, final)
            return str(final)
        except Exception as error:
            logger.warning(
                "cathedral build receipt for %s was not saved: %s", sandbox_id, error
            )
            return None
