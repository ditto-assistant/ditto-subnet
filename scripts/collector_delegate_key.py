"""One-time GCE collector delegate ceremony; only public receipts may leave.

No gcloud subprocess, disk wallet, CLI secret, rotation or transaction support.
An intent is fsynced BEFORE key generation. Any interrupted/uncertain attempt
requires manual reconciliation, even when the secret still appears empty.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import importlib.metadata
import json
import os
import re
import resource
import stat
import sys
from pathlib import Path
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

ROLES = ("registration", "transfer")
SDK_VERSION = "10.5.0"
META = "http://metadata.google.internal/computeMetadata/v1/"
API = "https://secretmanager.googleapis.com/v1/"


class Refusal(RuntimeError):
    """Public refusal codes only; never include exceptions/payloads."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise Refusal("redirect-refused")


def crc32c(data: bytes) -> int:
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


class Cloud:
    def __init__(self, project: str, role: str, mode: str):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.role = role
        self.project = project
        self.secret = f"sn118-collector-{role}-delegate"
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
            raise Refusal("project-invalid")
        expected_host = f"sn118-collector-{role}-signer"
        self.account = f"sn118-collector-{role}@{project}.iam.gserviceaccount.com"
        if (
            self.metadata("project/project-id") != project
            or self.metadata("instance/name") != expected_host
            or self.metadata("instance/service-accounts/default/email") != self.account
        ):
            raise Refusal("metadata-identity-mismatch")
        tags = json.loads(self.metadata("instance/tags"))
        phase = "armed" if mode == "generate" else "sealed"
        if (
            not isinstance(tags, list)
            or f"collector-{role}-{phase}" not in tags
            or any(str(t).endswith("-bootstrap") for t in tags)
        ):
            raise Refusal("generation-phase-mismatch")
        number = self.metadata("project/numeric-project-id")
        if not re.fullmatch(r"[1-9][0-9]+", number):
            raise Refusal("project-number-invalid")
        self.parent = f"projects/{number}/secrets/{self.secret}"
        token = json.loads(self.metadata("instance/service-accounts/default/token"))
        if not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise Refusal("metadata-token-invalid")
        self.token = token["access_token"]

    def metadata(self, suffix: str) -> str:
        with self.opener.open(
            Request(META + suffix, headers={"Metadata-Flavor": "Google"}), timeout=5
        ) as response:
            if response.headers.get("Metadata-Flavor") != "Google":
                raise Refusal("metadata-header-invalid")
            raw = response.read(32769)
            if len(raw) > 32768:
                raise Refusal("metadata-too-large")
            return raw.decode().strip()

    def call(self, suffix: str, body: dict | None = None) -> dict:
        # Call sites use fixed suffixes. API responses never select a URL.
        if suffix not in ("/versions?pageSize=1", ":addVersion", "/versions/1:access"):
            raise Refusal("api-method-refused")
        request = Request(
            API + self.parent + suffix,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        with self.opener.open(request, timeout=30) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise Refusal("api-response-too-large")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise Refusal("api-response-invalid")
            return value

    def assert_empty(self) -> None:
        result = self.call("/versions?pageSize=1")
        if result.get("versions", []) != [] or result.get("nextPageToken"):
            raise Refusal("existing-secret-version-no-rotation")

    def add(self, mnemonic: str) -> None:
        payload = mnemonic.encode("ascii")
        result = self.call(
            ":addVersion",
            {"payload": {"data": base64.b64encode(payload).decode(),
                         "dataCrc32c": str(crc32c(payload))}},
        )
        if (
            result.get("name") != self.parent + "/versions/1"
            or result.get("state") != "ENABLED"
            or result.get("clientSpecifiedPayloadChecksum") is not True
        ):
            raise Refusal("upload-result-uncertain")

    def access(self) -> str:
        result = self.call("/versions/1:access")
        if result.get("name") != self.parent + "/versions/1":
            raise Refusal("access-version-mismatch")
        payload = result["payload"]
        raw = base64.b64decode(payload["data"], validate=True)
        if str(crc32c(raw)) != str(payload.get("dataCrc32c")):
            raise Refusal("access-checksum-mismatch")
        return raw.decode("ascii")


def keypair_type():
    if importlib.metadata.version("bittensor") != SDK_VERSION:
        raise Refusal("sdk-version-mismatch")
    from bittensor_wallet import Keypair

    return Keypair


def check_private(fd: int, *, directory: bool = False) -> None:
    item = os.fstat(fd)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(item.st_mode) or item.st_uid != os.geteuid() or item.st_mode & 0o077:
        raise Refusal("ceremony-state-permissions")


def read_receipt(root: Path) -> dict:
    fd = os.open(root / "receipt.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        check_private(handle.fileno())
        raw = handle.read(8193)
        if len(raw) > 8192:
            raise Refusal("receipt-too-large")
        return json.loads(raw)


def write_receipt(root: Path, receipt: dict, *, first: bool = False) -> None:
    target = root / ("receipt.json" if first else ".receipt.pending")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if not first:
        os.replace(target, root / "receipt.json")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ceremony(cloud: Cloud, mode: str, root: Path, forbidden: list[str]) -> dict:
    # Parent is precreated by root; never provision directories on invocation.
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        check_private(directory_fd, directory=True)
    finally:
        os.close(directory_fd)
    lock_fd = os.open(root / "ceremony.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        check_private(lock.fileno())
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == "verify":
            receipt = read_receipt(root)
            if (
                receipt.get("status") not in ("pending-upload", "stored")
                or receipt.get("role") != cloud.role
                or receipt.get("secret_version") != cloud.parent + "/versions/1"
                or not receipt.get("address")
            ):
                raise Refusal("receipt-binding-mismatch")
            mnemonic = cloud.access()
            if len(mnemonic.split()) != 24:
                raise Refusal("mnemonic-format-invalid")
            address = keypair_type().create_from_mnemonic(mnemonic).ss58_address
            if address != receipt["address"] or address in forbidden:
                raise Refusal("stored-address-mismatch")
            return {**receipt, "status": "independently-rederived"}
        # Existing, invalid, disabled, destroyed or uncertain attempts never retry.
        if os.path.lexists(root / "receipt.json") or os.path.lexists(root / ".receipt.pending"):
            raise Refusal("existing-intent-no-regeneration")
        cloud.assert_empty()
        receipt = {"role": cloud.role, "secret_version": cloud.parent + "/versions/1",
                   "status": "started", "address": None}
        write_receipt(root, receipt, first=True)
        Keypair = keypair_type()
        mnemonic = Keypair.generate_mnemonic(n_words=24)
        if len(mnemonic.split()) != 24:
            raise Refusal("mnemonic-format-invalid")
        address = Keypair.create_from_mnemonic(mnemonic).ss58_address
        if address in forbidden or not re.fullmatch(r"5[1-9A-HJ-NP-Za-km-z]{47}", address):
            raise Refusal("generated-address-invalid")
        receipt.update(address=address, status="pending-upload")
        write_receipt(root, receipt)
        cloud.add(mnemonic)  # exactly one request; no upload/replacement retry
        receipt["status"] = "stored"
        write_receipt(root, receipt)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--mode", required=True, choices=("generate", "verify"))
    parser.add_argument("--forbidden-address", action="append", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    expected = f"{args.mode.upper()} GCP COLLECTOR {args.role.upper()} DELEGATE"
    if args.confirm != expected:
        parser.error("exact role-specific ceremony confirmation required")
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.umask(0o077)
        if len(set(args.forbidden_address)) != 5:
            raise Refusal("five-offline-public-identities-required")
        cloud = Cloud(args.project, args.role, args.mode)
        root = Path("/var/lib/sn118-collector-key-ceremony")
        receipt = ceremony(cloud, args.mode, root, args.forbidden_address)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception:
        # Includes dependency/transport/HTTP/SDK errors. Their text can contain
        # auth headers or payloads; never emit repr, body, traceback or cause.
        print("Collector delegate ceremony refused. Preserve state; reconcile before retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
