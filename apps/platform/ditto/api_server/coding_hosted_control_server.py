"""Authenticated bounded Unix transport for the native Platform control adapter."""

from __future__ import annotations

import asyncio
import hmac
import os
import socket
import stat
import struct
from pathlib import Path

from ditto.api_models.coding_hosted_control import ControlCommand
from ditto.api_models.coding_inference import parse_coding_inference_json
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.api_server.coding_hosted_control import HostedAuthoringControl

MAGIC = b"DITTO-HOSTED-CONTROL-V2\n"


class HostedControlServer:
    def __init__(self, control: HostedAuthoringControl, token: bytes):
        if type(token) is not bytes or len(token) != 32 or not any(token):
            raise ValueError("invalid private control credential")
        self._control, self._token, self._active = control, bytes(token), 0

    async def start(self, path: Path) -> asyncio.AbstractServer:
        info = path.parent.lstat()
        if (
            not path.is_absolute()
            or path.parent.resolve() != path.parent
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or path.exists()
            or path.is_symlink()
        ):
            raise ValueError("private control socket is unsafe")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(8)
            listener.setblocking(False)
            return await asyncio.start_unix_server(
                self.handle, sock=listener, limit=65536
            )
        except BaseException:
            listener.close()
            raise

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        admitted = self._active < 8
        if admitted:
            self._active += 1
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
                if not 0 < size <= 16384:
                    return
                command = parse_coding_inference_json(
                    ControlCommand, await reader.readexactly(size), maximum_bytes=16384
                )
            operation = command.operation
            source = command.source
            if operation not in {"retain", "grading"} and command.retention is not None:
                return
            if operation != "freeze" and command.patch_size:
                return
            if (
                operation not in {"freeze", "grading"}
                and command.evidence_sha256 is not None
            ):
                return
            if operation != "terminal" and (
                command.terminal_size or command.terminal_sha256 is not None
            ):
                return
            result = {
                # No private bytes are included in this correlation header.
                "schema": "dittobench-coding-hosted-control-result-v2",
                "request_id": str(command.request_id),
                "operation": operation,
                "source_sha256": source.digest(),
                "ok": True,
            }
            if operation != "grading_bundle" and command.claim_id is not None:
                return
            async with asyncio.timeout(
                600 if operation in {"retain", "terminal"} else 180
            ):
                async with self._control._sessions() as session, session.begin():
                    await self._control._owner(source, session)
                if operation in {
                    "grading",
                    "check_grading",
                    "grading_bundle",
                    "terminal",
                }:
                    grading = self._control._grading
                    if grading is None:
                        return
                    if operation == "grading_bundle":
                        if command.claim_id is None or await reader.read(1):
                            return
                        body = await grading.protected(source, command.claim_id)
                        result["payload_kind"] = "grading_bundle"
                        result["payload_size"] = len(body)
                        await self._reply(writer, result)
                        writer.write(body)
                        await writer.drain()
                        return
                    if operation == "grading":
                        if command.retention is None or command.evidence_sha256 is None:
                            return
                        self._control.retention_bounds(command.retention)
                        freeze = await reader.readexactly(command.retention.freeze_size)
                        if await reader.read(1):
                            return
                        header, objects = await grading.inputs(
                            source, command.evidence_sha256, command.retention, freeze
                        )
                        result["payload_kind"] = "grading"
                        await self._reply(writer, result)
                        encoded = canonical(header)
                        for body in (
                            struct.pack(">I", len(encoded)),
                            encoded,
                            *objects,
                        ):
                            await grading.check(source)
                            writer.write(body)
                            await writer.drain()
                        await grading.check(source)
                        writer.write(b"DITTO-GRADING-READY-V2\n")
                        await writer.drain()
                        return
                    if operation == "terminal":
                        if not command.terminal_size or command.terminal_sha256 is None:
                            return
                        body = await reader.readexactly(command.terminal_size)
                        from ditto.api_server.coding_hosted_authoring_evidence import (
                            sha,
                        )

                        if sha(body) != command.terminal_sha256 or await reader.read(1):
                            return
                        result["evidence_sha256"] = await grading.terminal(source, body)
                    else:
                        if await reader.read(1):
                            return
                        await grading.check(source)
                    await self._reply(writer, result)
                    return
                if operation == "retain":
                    header = command.retention
                    if header is None:
                        return
                    self._control.retention_bounds(header)
                    freeze = await reader.readexactly(header.freeze_size)

                    async def transcript():
                        remaining = header.transcript_size
                        while remaining:
                            body = await reader.readexactly(min(65536, remaining))
                            remaining -= len(body)
                            yield body
                        if await reader.read(1):
                            raise ValueError("trailing private control bytes")

                    result["evidence_sha256"] = await self._control.retain(
                        source, header, freeze, transcript()
                    )
                else:
                    patch = b""
                    if operation == "freeze":
                        if (
                            command.evidence_sha256 is None
                            or command.patch_size > self._control._patch_limit
                        ):
                            return
                        patch = await reader.readexactly(command.patch_size)
                    if await reader.read(1):
                        return
                    if operation == "authoring":
                        inputs = await self._control.authoring(source)
                        result["payload_kind"] = "authoring"
                        await self._reply(writer, result)
                        for body in (
                            struct.pack(">I", len(inputs.header)),
                            inputs.header,
                            *inputs.objects,
                        ):
                            await inputs.recheck()
                            writer.write(body)
                            await writer.drain()
                        await inputs.recheck()
                        writer.write(b"DITTO-AUTHORING-READY-V2\n")
                        await writer.drain()
                        return
                    if operation == "check":
                        await self._control.check(source)
                    elif operation == "inference":
                        result["bridge"] = await self._control.inference(source)
                    elif operation == "revoke":
                        await self._control.revoke(source)
                    elif operation == "close":
                        await self._control.close(source)
                    elif operation == "abort":
                        await self._control.abort(source)
                    elif operation == "freeze":
                        result["freeze"] = await self._control.freeze(
                            source, patch, command.evidence_sha256 or ""
                        )
                await self._reply(writer, result)
        except (asyncio.CancelledError, Exception):
            # EOF is a generic failure. Never serialize private payloads, paths,
            # database errors or credentials. Prepared evidence stays replayable.
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(5):
                    await writer.wait_closed()
            except Exception:
                pass
            if admitted:
                self._active -= 1

    async def _reply(self, writer: asyncio.StreamWriter, value: dict) -> None:
        body = canonical(value)
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()

    def __repr__(self) -> str:
        return "HostedControlServer(private=True)"
