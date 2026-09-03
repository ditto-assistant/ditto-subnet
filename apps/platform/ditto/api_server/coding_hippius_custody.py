"""Protected spool and default-off custody wiring for Hippius Coding evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_evidence import CodingSealedEvidenceIdentity
from ditto.api_server.coding_hippius_evidence import (
    AiobotoHippiusSealedEvidenceTransport,
    HippiusSealedEvidenceError,
    HippiusSealedEvidenceMediator,
    HippiusSealedEvidencePreparedObject,
    HippiusSealedEvidenceReceipt,
    HippiusSealedEvidenceSourceAuthority,
    PostgresHippiusSealedEvidenceLedger,
    parse_hippius_sealed_evidence_config,
    prepare_hippius_sealed_evidence,
)
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt

_SPOOL_SCHEMA = "dittobench-coding-hippius-sealed-evidence-spool-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 64 << 10
_MAX_PUBLIC_KEY_BYTES = 64 << 10
_MIN_RSA_BITS = 3072
_MAX_RSA_BITS = 8192


class HippiusEvidenceCustodyError(RuntimeError):
    """Protected custody is absent or inconsistent without exposing its paths."""


@dataclass(frozen=True)
class HippiusEvidenceCustodyReadiness:
    configured: bool
    provider: str
    private_input_authority_sha256: str
    sealed_evidence_authority_sha256: str
    probe_receipt_payload_sha256: str
    wrapping_key_sha256: str
    spool_ready: bool
    runtime_wired: bool
    worker_active: bool
    weight_eligible: bool


class RsaOaepHippiusEvidenceKeyWrapper:
    """Public-key-only wrapper; private unwrap authority stays external."""

    def __init__(self, public_key_path: Path) -> None:
        body = _read_regular_file(
            public_key_path,
            maximum_bytes=_MAX_PUBLIC_KEY_BYTES,
            label="evidence wrapping public key",
        )
        try:
            loaded = serialization.load_pem_public_key(body)
        except (TypeError, ValueError) as error:
            raise HippiusEvidenceCustodyError(
                "evidence wrapping public key is invalid"
            ) from error
        if (
            not isinstance(loaded, rsa.RSAPublicKey)
            or not _MIN_RSA_BITS <= loaded.key_size <= _MAX_RSA_BITS
            or loaded.public_numbers().e != 65_537
        ):
            raise HippiusEvidenceCustodyError(
                "evidence wrapping key must be RSA-3072 through RSA-8192"
            )
        self._public_key = loaded
        der = loaded.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._wrapping_key_sha256 = hashlib.sha256(der).hexdigest()

    @property
    def wrapping_key_sha256(self) -> str:
        return self._wrapping_key_sha256

    async def wrap_data_key(self, *, data_key: bytes, aad_sha256: str) -> bytes:
        if len(data_key) != 32 or _SHA256.fullmatch(aad_sha256) is None:
            raise HippiusEvidenceCustodyError("evidence wrapping request is invalid")
        try:
            return self._public_key.encrypt(
                data_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=bytes.fromhex(aad_sha256),
                ),
            )
        except ValueError as error:
            raise HippiusEvidenceCustodyError(
                "evidence data-key wrapping failed"
            ) from error

    def __repr__(self) -> str:
        return "RsaOaepHippiusEvidenceKeyWrapper(configured=True)"


class HippiusEvidenceSpool:
    """Mode-0700 spool retaining exact prepared bytes for crash replay."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise HippiusEvidenceCustodyError("evidence spool root is invalid")
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HippiusEvidenceCustodyError(
                "evidence spool root ownership or mode is unsafe"
            )
        self._root = root

    def store(
        self, prepared: HippiusSealedEvidencePreparedObject
    ) -> HippiusSealedEvidencePreparedObject:
        identity_sha256 = prepared.identity.identity_sha256
        if _SHA256.fullmatch(identity_sha256) is None:
            raise HippiusEvidenceCustodyError("evidence spool identity is invalid")
        directory = self._root / identity_sha256
        for _attempt in range(2):
            existing = self._existing_prepared(directory, identity_sha256)
            if existing is not None:
                if existing != prepared:
                    raise HippiusEvidenceCustodyError(
                        "evidence spool identity already contains different bytes"
                    )
                return existing
            try:
                directory.mkdir(mode=0o700)
                os.chmod(directory, 0o700)
                _write_exclusive(
                    directory / "ciphertext.bin",
                    prepared.ciphertext,
                )
                _sync_directory(directory)
                _write_exclusive(
                    directory / "manifest.json",
                    _spool_manifest_bytes(prepared),
                )
                _sync_directory(directory)
                _sync_directory(self._root)
            except FileExistsError:
                continue
            except OSError as error:
                _discard_incomplete_identity(directory)
                raise HippiusEvidenceCustodyError(
                    "evidence spool persistence failed"
                ) from error
            return self.load(identity_sha256)
        raise HippiusEvidenceCustodyError(
            "evidence spool identity raced with another writer"
        )

    def _existing_prepared(
        self, directory: Path, identity_sha256: str
    ) -> HippiusSealedEvidencePreparedObject | None:
        if directory.is_symlink():
            raise HippiusEvidenceCustodyError("evidence spool entry is unavailable")
        if not directory.exists():
            return None
        if (directory / "manifest.json").is_file():
            return self.load(identity_sha256)
        _discard_incomplete_identity(directory)
        return None

    def load(self, identity_sha256: str) -> HippiusSealedEvidencePreparedObject:
        if _SHA256.fullmatch(identity_sha256) is None:
            raise HippiusEvidenceCustodyError("evidence spool identity is invalid")
        directory = self._root / identity_sha256
        if directory.is_symlink() or not directory.is_dir():
            raise HippiusEvidenceCustodyError("evidence spool entry is unavailable")
        info = directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HippiusEvidenceCustodyError("evidence spool entry is unsafe")
        manifest_body = _read_regular_file(
            directory / "manifest.json",
            maximum_bytes=_MAX_MANIFEST_BYTES,
            label="evidence spool manifest",
        )
        try:
            raw = json.loads(manifest_body, object_pairs_hook=_unique_object)
            if not isinstance(raw, dict) or raw.pop("schema") != _SPOOL_SCHEMA:
                raise ValueError("spool schema is invalid")
            if raw.pop("weight_eligible") is not False:
                raise ValueError("spool weight eligibility is invalid")
            identity_raw = raw.pop("identity")
            if not isinstance(identity_raw, dict):
                raise ValueError("spool identity is invalid")
            identity = CodingSealedEvidenceIdentity.model_validate(identity_raw)
            remote_key = str(raw.pop("remote_key"))
            nonce = base64.b64decode(str(raw.pop("nonce_b64")), validate=True)
            wrapped_data_key = base64.b64decode(
                str(raw.pop("wrapped_data_key_b64")),
                validate=True,
            )
            ciphertext_sha256 = str(raw.pop("ciphertext_sha256"))
            ciphertext_size_bytes = int(raw.pop("ciphertext_size_bytes"))
            if raw:
                raise ValueError("spool manifest fields are invalid")
            ciphertext = _read_regular_file(
                directory / "ciphertext.bin",
                maximum_bytes=identity.ciphertext_size_bytes,
                label="evidence spool ciphertext",
            )
            prepared = HippiusSealedEvidencePreparedObject(
                identity=identity,
                remote_key=remote_key,
                ciphertext=ciphertext,
                nonce=nonce,
                wrapped_data_key=wrapped_data_key,
            )
            if (
                identity.identity_sha256 != identity_sha256
                or ciphertext_sha256 != identity.ciphertext_sha256
                or ciphertext_size_bytes != identity.ciphertext_size_bytes
                or _spool_manifest_bytes(prepared) != manifest_body
            ):
                raise ValueError("spool manifest identity is inconsistent")
        except (KeyError, TypeError, ValueError) as error:
            raise HippiusEvidenceCustodyError(
                "evidence spool manifest is invalid"
            ) from error
        return prepared

    def __repr__(self) -> str:
        return "HippiusEvidenceSpool(configured=True)"


class HippiusEvidenceRuntime:
    """Default-off composition; no endpoint or worker invokes it in this PR."""

    def __init__(
        self,
        *,
        spool: HippiusEvidenceSpool,
        wrapper: RsaOaepHippiusEvidenceKeyWrapper,
        mediator: HippiusSealedEvidenceMediator,
        readiness: HippiusEvidenceCustodyReadiness,
    ) -> None:
        self._spool = spool
        self._wrapper = wrapper
        self._mediator = mediator
        self.readiness = readiness

    async def prepare_and_store(
        self,
        *,
        authority: HippiusSealedEvidenceSourceAuthority,
        plaintext: bytes,
    ) -> str:
        prepared = await prepare_hippius_sealed_evidence(
            authority=authority,
            plaintext=plaintext,
            key_wrapper=self._wrapper,
        )
        stored = self._spool.store(prepared)
        return stored.identity.identity_sha256

    async def publish(self, identity_sha256: str) -> HippiusSealedEvidenceReceipt:
        return await self._mediator.publish(prepared=self._spool.load(identity_sha256))

    def __repr__(self) -> str:
        return "HippiusEvidenceRuntime(configured=True, worker_active=False)"


def create_hippius_evidence_runtime_from_env(
    *,
    session_maker: async_sessionmaker[AsyncSession],
    environ: dict[str, str] | None = None,
) -> HippiusEvidenceRuntime | None:
    values = os.environ if environ is None else environ
    raw_enabled = values.get("DITTO_CODING_HIPPIUS_EVIDENCE_ENABLED", "false")
    if raw_enabled.lower() in {"false", "0", "no", "off"}:
        return None
    if raw_enabled.lower() not in {"true", "1", "yes", "on"}:
        raise HippiusEvidenceCustodyError(
            "DITTO_CODING_HIPPIUS_EVIDENCE_ENABLED must be true or false"
        )

    def required(name: str) -> str:
        value = values.get(name, "")
        if not value:
            raise HippiusEvidenceCustodyError(
                f"required evidence custody setting is missing: {name}"
            )
        return value

    probe_path = Path(required("DITTO_CODING_HIPPIUS_PROBE_RECEIPT_PATH"))
    spool_root = Path(required("DITTO_CODING_HIPPIUS_EVIDENCE_SPOOL_ROOT"))
    wrapping_public_key_path = Path(
        required("DITTO_CODING_HIPPIUS_EVIDENCE_WRAPPING_PUBLIC_KEY_PATH")
    )
    if not all(
        path.is_absolute()
        for path in (probe_path, spool_root, wrapping_public_key_path)
    ):
        raise HippiusEvidenceCustodyError("evidence custody paths must be absolute")
    try:
        config = parse_hippius_sealed_evidence_config(values)
    except HippiusSealedEvidenceError as error:
        raise HippiusEvidenceCustodyError(
            "evidence custody configuration is incomplete or unsafe"
        ) from error
    probe, probe_payload_sha256 = load_hippius_probe_receipt(probe_path)
    if probe.sealed_evidence_authority_sha256 != config.authority_sha256:
        raise HippiusEvidenceCustodyError(
            "evidence custody does not match its probe authority"
        )
    spool = HippiusEvidenceSpool(spool_root)
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(wrapping_public_key_path)
    transport = AiobotoHippiusSealedEvidenceTransport(config)
    mediator = HippiusSealedEvidenceMediator(
        config=config,
        probe_receipt_path=probe_path,
        transport=transport,
        ledger=PostgresHippiusSealedEvidenceLedger(session_maker),
    )
    readiness = HippiusEvidenceCustodyReadiness(
        configured=True,
        provider="hippius",
        private_input_authority_sha256=probe.private_input_authority_sha256,
        sealed_evidence_authority_sha256=probe.sealed_evidence_authority_sha256,
        probe_receipt_payload_sha256=probe_payload_sha256,
        wrapping_key_sha256=wrapper.wrapping_key_sha256,
        spool_ready=True,
        runtime_wired=True,
        worker_active=False,
        weight_eligible=False,
    )
    return HippiusEvidenceRuntime(
        spool=spool,
        wrapper=wrapper,
        mediator=mediator,
        readiness=readiness,
    )


def _spool_manifest_bytes(prepared: HippiusSealedEvidencePreparedObject) -> bytes:
    return coding_canonical_json_bytes(
        {
            "ciphertext_sha256": prepared.identity.ciphertext_sha256,
            "ciphertext_size_bytes": prepared.identity.ciphertext_size_bytes,
            "identity": prepared.identity.model_dump(mode="json", by_alias=True),
            "nonce_b64": base64.b64encode(prepared.nonce).decode("ascii"),
            "remote_key": prepared.remote_key,
            "schema": _SPOOL_SCHEMA,
            "weight_eligible": False,
            "wrapped_data_key_b64": base64.b64encode(prepared.wrapped_data_key).decode(
                "ascii"
            ),
        },
        maximum_bytes=_MAX_MANIFEST_BYTES,
        label="Hippius evidence spool manifest",
    )


def _read_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusEvidenceCustodyError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 1 <= info.st_size <= maximum_bytes
        ):
            raise HippiusEvidenceCustodyError(f"{label} is unsafe")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except HippiusEvidenceCustodyError:
        raise
    except OSError as error:
        raise HippiusEvidenceCustodyError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise HippiusEvidenceCustodyError(f"{label} exceeds bounds")
    return bytes(body)


def _discard_incomplete_identity(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        return
    if (directory / "manifest.json").exists():
        return
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise HippiusEvidenceCustodyError("evidence spool entry is unsafe")
        child.unlink(missing_ok=True)
    directory.rmdir()


def _write_exclusive(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise HippiusEvidenceCustodyError(
            "evidence spool file cannot be created"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HippiusEvidenceCustodyError(
                    "evidence spool write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise HippiusEvidenceCustodyError(
            "evidence spool directory sync failed"
        ) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


__all__ = [
    "HippiusEvidenceCustodyError",
    "HippiusEvidenceCustodyReadiness",
    "HippiusEvidenceRuntime",
    "HippiusEvidenceSpool",
    "RsaOaepHippiusEvidenceKeyWrapper",
    "create_hippius_evidence_runtime_from_env",
]
