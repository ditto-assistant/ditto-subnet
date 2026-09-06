"""Authenticated local socket bridge for one native Platform worker incarnation."""

from __future__ import annotations

import asyncio
import base64
import hmac
import os
import socket
import stat
import struct
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ditto.api_models.coding_hosted_relay import HostedRelayBinding, HostedRelayCommand
from ditto.api_models.coding_inference import (
    coding_inference_canonical_json_bytes,
    parse_coding_inference_json,
)
from ditto.api_server.coding_hosted_provider import (
    HostedProviderAdapter,
    ProviderResult,
)

MAGIC = b"DITTO-HOSTED-INFERENCE-V2\n"
MAX_COMMAND = 6 << 20
MAX_RESULT = 12 << 20


class HostedRelayBridge:
    """No factory or CLI wiring. Caller owns a private 0700 directory and socket.

    Exactly one successful open per process. Worker recovery must not recreate a
    started attempt/bridge. Evidence persistence is a mandatory trusted callback;
    it runs before output delivery and is never a miner-provided implementation.
    """

    def __init__(
        self,
        *,
        adapter: HostedProviderAdapter,
        binding: HostedRelayBinding,
        token: bytes,
        retain_evidence: Callable[[ProviderResult], Awaitable[None]],
    ):
        if (
            type(token) is not bytes
            or len(token) != 32
            or not any(token)
            or not callable(retain_evidence)
        ):
            raise ValueError("hosted relay configuration is invalid")
        self._adapter = adapter
        self._binding = HostedRelayBinding.model_validate_json(
            binding.model_dump_json(by_alias=True)
        )
        self._token = bytes(token)
        self._retain = retain_evidence
        self._opened = False
        self._claimed = False
        self._closed = False
        self._active: asyncio.Task[dict[str, Any]] | None = None
        self._connections = 0
        self._revoke_lock = asyncio.Lock()

    async def start(self, path: Path) -> asyncio.AbstractServer:
        parent = path.parent.lstat()
        if (
            not path.is_absolute()
            or path.parent.resolve() != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.geteuid()
            or path.exists()
            or path.is_symlink()
        ):
            raise ValueError("hosted relay socket path is not private")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))  # Never unlink/replace an existing path.
            path.chmod(0o600)
            listener.listen(4)
            listener.setblocking(False)
            return await asyncio.start_unix_server(
                self.handle, sock=listener, limit=65536
            )
        except BaseException:
            listener.close()
            raise

    async def revoke(self) -> bool:
        self._closed = True
        async with self._revoke_lock:
            active = self._active
            if active is not None and not active.done():
                if not active.cancelling():
                    active.cancel()
                try:
                    await asyncio.shield(active)
                except asyncio.CancelledError:
                    if not active.done():
                        raise  # Control cancellation is not provider quiescence.
                except Exception:
                    pass  # Terminal provider failure still needs ledger revocation.
            return await self._adapter.revoke()

    async def _complete(self, command: HostedRelayCommand) -> dict[str, Any]:
        await self._adapter.authorize_relay(self._binding)
        request = base64.b64decode(command.miner_request_base64, validate=True)
        if not 0 < len(request) <= 4 << 20:
            raise ValueError("hosted relay request exceeds bound")
        result = await self._adapter.complete_miner(
            request_id=command.request_id, miner_request=request
        )
        await self._retain(result)
        await self._adapter.authorize_relay(self._binding)
        if self._closed:
            raise ValueError("hosted relay closed")
        return {
            "response_base64": base64.b64encode(result.response).decode("ascii"),
            "settlement": result.settlement.model_dump(mode="json", by_alias=True),
        }

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        active: asyncio.Task[dict[str, Any]] | None = None
        watch: asyncio.Task[bytes] | None = None
        admitted = self._connections < 4
        if admitted:
            self._connections += 1
        try:
            if not admitted:
                return
            async with asyncio.timeout(10):
                auth = await reader.readexactly(len(MAGIC) + 32)
                if auth[: len(MAGIC)] != MAGIC or not hmac.compare_digest(
                    auth[len(MAGIC) :], self._token
                ):
                    return
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                if not 0 < size <= MAX_COMMAND:
                    return
                command = parse_coding_inference_json(
                    HostedRelayCommand,
                    await reader.readexactly(size),
                    maximum_bytes=MAX_COMMAND,
                )
            if command.binding_sha256 != self._binding.digest():
                return
            reply: dict[str, Any] = {
                "schema": "dittobench-coding-hosted-relay-result-v2",
                "binding_sha256": self._binding.digest(),
                "request_id": str(command.request_id),
                "operation": command.operation,
            }
            if command.operation != "complete" and command.miner_request_base64:
                return
            if command.operation == "revoke":
                async with asyncio.timeout(30):
                    reply["ledger_drained"] = await self.revoke()
            else:
                remaining = self._binding.expires_at_unix - time.time()
                if self._closed or remaining <= 0:
                    return
                async with asyncio.timeout(min(remaining, 340)):
                    if command.operation == "open":
                        if self._claimed:
                            return
                        # Claim before awaiting: two open requests cannot both succeed.
                        self._claimed = True
                        await self._adapter.authorize_relay(self._binding)
                        self._opened = True
                    else:
                        if not self._opened or self._active is not None:
                            return
                        active = asyncio.create_task(self._complete(command))
                        self._active = active
                        # EOF/extra bytes while waiting mean the single-frame
                        # caller disappeared or violated framing. Cancel and drain.
                        watch = asyncio.create_task(reader.read(1))
                        await asyncio.wait(
                            {active, watch}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if watch.done():
                            raise ValueError("hosted relay caller disconnected")
                        reply.update(await active)
            encoded = coding_inference_canonical_json_bytes(
                reply, maximum_bytes=MAX_RESULT
            )
            writer.write(struct.pack(">I", len(encoded)) + encoded)
            async with asyncio.timeout(5):
                await writer.drain()
        except (asyncio.CancelledError, Exception):
            # No raw exceptions, provider text, database details or capabilities
            # leave this private boundary. Reserved rows remain fail-closed.
            if active is not None:
                self._closed = True
        finally:
            if watch is not None:
                watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)
            if active is not None:
                if not active.done() and not active.cancelling():
                    active.cancel()
                await asyncio.gather(active, return_exceptions=True)
                if self._active is active:
                    self._active = None
            writer.close()
            try:
                async with asyncio.timeout(5):
                    await writer.wait_closed()
            except Exception:
                pass
            if admitted:
                self._connections -= 1
