"""One-time federated-node enrollment and atomic credential persistence."""

from __future__ import annotations

import os
import time
from base64 import b64decode
from binascii import Error as BinasciiError
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ditto_screener.errors import ScreenerConfigError


class NodeCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str
    node_id: str
    provider: str
    provider_resource_id: str
    screener_hotkey: str
    mnemonic: str = Field(repr=False)
    api_token: str = Field(repr=False)
    expires_at: datetime
    pending_refresh_id: UUID | None = Field(default=None, repr=False)


class NodeEnrollmentIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registration_id: UUID
    mnemonic: str = Field(repr=False)


def registration_signing_message(
    *,
    environment: str,
    node_id: str,
    provider: str,
    provider_resource_id: str,
    screener_hotkey: str,
    timestamp: int,
    registration_id: UUID,
) -> bytes:
    return (
        "ditto-screener-node-register:v1:"
        f"{environment}:{node_id}:{provider}:{provider_resource_id}:"
        f"{screener_hotkey}:{timestamp}:{registration_id}"
    ).encode()


def refresh_signing_message(
    *, node_id: str, screener_hotkey: str, timestamp: int, refresh_id: UUID
) -> bytes:
    return (
        "ditto-screener-node-refresh:v1:"
        f"{node_id}:{screener_hotkey}:{timestamp}:{refresh_id}"
    ).encode()


def load_node_credential(path: str | Path) -> NodeCredential:
    try:
        return NodeCredential.model_validate_json(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ScreenerConfigError(
            "federated node credential file is invalid"
        ) from error


def store_node_credential(path: str | Path, credential: NodeCredential) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(credential.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_or_create_enrollment_intent(path: Path) -> NodeEnrollmentIntent:
    if path.exists():
        try:
            return NodeEnrollmentIntent.model_validate_json(path.read_text())
        except (OSError, ValueError) as error:
            raise ScreenerConfigError(
                "federated enrollment intent is invalid"
            ) from error

    import bittensor

    intent = NodeEnrollmentIntent(
        registration_id=uuid4(),
        mnemonic=bittensor.Keypair.generate_mnemonic(n_words=12),
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(intent.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return intent


def apply_node_credential(credential: NodeCredential) -> None:
    """Populate the existing worker config contract without logging values."""
    os.environ["SCREENER_NODE_ENVIRONMENT"] = credential.environment
    os.environ["SCREENER_NODE_ID"] = credential.node_id
    os.environ["SCREENER_NODE_PROVIDER"] = credential.provider
    os.environ["SCREENER_NODE_PROVIDER_RESOURCE_ID"] = credential.provider_resource_id
    os.environ["SCREENER_HOTKEY"] = credential.screener_hotkey
    os.environ["SCREENER_MNEMONIC"] = credential.mnemonic
    os.environ["SCREENER_API_TOKEN"] = credential.api_token


async def ensure_node_credentials_from_env() -> None:
    """Load existing credentials or consume a one-time registration token."""
    await _materialize_source_review_secret()
    credential_path = os.environ.get("SCREENER_NODE_CREDENTIAL_FILE")
    if not credential_path:
        return
    target = Path(credential_path)
    if target.exists():
        apply_node_credential(load_node_credential(target))
        return

    required = {
        "SCREENER_NODE_ENVIRONMENT": os.environ.get("SCREENER_NODE_ENVIRONMENT"),
        "SCREENER_NODE_ID": os.environ.get("SCREENER_NODE_ID"),
        "SCREENER_NODE_PROVIDER": os.environ.get("SCREENER_NODE_PROVIDER"),
        "SCREENER_NODE_PROVIDER_RESOURCE_ID": os.environ.get(
            "SCREENER_NODE_PROVIDER_RESOURCE_ID"
        ),
        "SCREENER_PLATFORM_API_URL": os.environ.get("SCREENER_PLATFORM_API_URL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ScreenerConfigError(
            "federated enrollment is missing " + ", ".join(sorted(missing))
        )
    registration_token = os.environ.pop("SCREENER_REGISTRATION_TOKEN", "").strip()
    registration_path_raw = os.environ.get("SCREENER_REGISTRATION_TOKEN_FILE")
    registration_path = Path(registration_path_raw) if registration_path_raw else None
    if not registration_token and registration_path is not None:
        try:
            registration_token = registration_path.read_text().strip()
        except OSError as error:
            raise ScreenerConfigError(
                "registration token file is unreadable"
            ) from error
    if not registration_token:
        raise ScreenerConfigError(
            "federated enrollment is missing a registration token"
        )
    if len(registration_token) < 43:
        raise ScreenerConfigError("registration token file is invalid")

    import bittensor

    intent_path = target.with_name(f".{target.name}.enrollment")
    intent = _load_or_create_enrollment_intent(intent_path)
    mnemonic = intent.mnemonic
    keypair: Any = bittensor.Keypair.create_from_mnemonic(mnemonic)
    node_id = str(required["SCREENER_NODE_ID"])
    environment = str(required["SCREENER_NODE_ENVIRONMENT"])
    provider = str(required["SCREENER_NODE_PROVIDER"])
    resource_id = str(required["SCREENER_NODE_PROVIDER_RESOURCE_ID"])
    timestamp = int(time.time())
    signature = keypair.sign(
        registration_signing_message(
            environment=environment,
            node_id=node_id,
            provider=provider,
            provider_resource_id=resource_id,
            screener_hotkey=keypair.ss58_address,
            timestamp=timestamp,
            registration_id=intent.registration_id,
        )
    ).hex()
    url = (
        str(required["SCREENER_PLATFORM_API_URL"]).rstrip("/")
        + "/api/v1/screener/nodes/register"
    )
    payload = {
        "environment": environment,
        "node_id": node_id,
        "provider": provider,
        "provider_resource_id": resource_id,
        "screener_hotkey": keypair.ss58_address,
        "timestamp": timestamp,
        "signature": signature,
        "registration_id": str(intent.registration_id),
    }
    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"Authorization": f"Bootstrap {registration_token}"},
                    )
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    continue
                if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
    except httpx.HTTPError as error:
        raise ScreenerConfigError("federated enrollment request failed") from error
    if response is None:
        raise ScreenerConfigError("federated enrollment request failed")
    if response.status_code != 200:
        raise ScreenerConfigError(
            f"federated enrollment was rejected ({response.status_code})"
        )
    try:
        body = response.json()
        credential = NodeCredential(
            environment=environment,
            node_id=node_id,
            provider=provider,
            provider_resource_id=resource_id,
            screener_hotkey=body["screener_hotkey"],
            mnemonic=mnemonic,
            api_token=body["api_token"],
            expires_at=body["expires_at"],
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ScreenerConfigError("federated enrollment response is invalid") from error
    store_node_credential(target, credential)
    intent_path.unlink(missing_ok=True)
    if registration_path is not None:
        try:
            registration_path.unlink()
        except OSError as error:
            target.unlink(missing_ok=True)
            raise ScreenerConfigError(
                "could not erase the consumed registration token"
            ) from error
    apply_node_credential(credential)


async def _materialize_source_review_secret() -> None:
    """Exchange bootstrap authority and keep the provider key out of env."""
    value = os.environ.pop("SCREENER_SOURCE_REVIEW_API_KEY", "").strip()
    access_token = os.environ.pop("SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN", "").strip()
    secret_resource = os.environ.pop(
        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE", ""
    ).strip()
    if not value and not access_token and not secret_resource:
        return
    credential_path = os.environ.get("SCREENER_NODE_CREDENTIAL_FILE")
    if not credential_path:
        raise ScreenerConfigError(
            "SCREENER_NODE_CREDENTIAL_FILE is required for source-review secret"
        )
    target = Path(credential_path).with_name("source-review-api-key")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"] = str(target)
        return
    if bool(access_token) != bool(secret_resource):
        raise ScreenerConfigError("source-review secret bootstrap is incomplete")
    if not value:
        url = (
            "https://secretmanager.googleapis.com/v1/"
            f"{secret_resource}/versions/latest:access"
        )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as error:
            raise ScreenerConfigError(
                "source-review secret bootstrap request failed"
            ) from error
        if response.status_code != 200:
            raise ScreenerConfigError(
                f"source-review secret bootstrap was rejected ({response.status_code})"
            )
        try:
            encoded = response.json()["payload"]["data"]
            value = b64decode(encoded, validate=True).decode().strip()
        except (BinasciiError, KeyError, TypeError, ValueError, UnicodeError) as error:
            raise ScreenerConfigError(
                "source-review secret bootstrap response is invalid"
            ) from error
        if len(value) < 16:
            raise ScreenerConfigError("source-review secret bootstrap returned no key")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"] = str(target)
