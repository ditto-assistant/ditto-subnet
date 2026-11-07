"""Verify the Pylon image's TLS fixes using only synthetic loopback traffic."""

from __future__ import annotations

import asyncio
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

import anyio
import certifi
from anyio.streams.tls import TLSStream


class TLSImageTests(unittest.IsolatedAsyncioTestCase):
    def test_compromised_intermediate_root_is_not_trusted(self):
        context = ssl.create_default_context(cafile=certifi.where())
        names = {
            value
            for cert in context.get_ca_certs()
            for rdn in cert["subject"]
            for key, value in rdn
            if key == "commonName"
        }
        self.assertFalse(
            "emSign ECC Root CA - G3" in names,
            "root of compromised intermediates is trusted",
        )
        self.assertIn("emSign Root CA - G1", names)

    async def _connect_with_certificate(self, dns_name: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.pem"
            cert = Path(tmp) / "cert.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    f"/CN={dns_name}",
                    "-addext",
                    f"subjectAltName=DNS:{dns_name}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(cert), str(key))
            client_context = ssl.create_default_context(cafile=str(cert))

            async def respond(reader, writer):
                try:
                    if await reader.readexactly(4) == b"ping":
                        writer.write(b"pong")
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(
                respond, "127.0.0.1", 0, ssl=server_context
            )
            async with server:
                port = server.sockets[0].getsockname()[1]
                stream = await anyio.connect_tcp("127.0.0.1", port)
                try:
                    tls = await TLSStream.wrap(
                        stream, hostname="faß.de", ssl_context=client_context
                    )
                except BaseException:
                    await stream.aclose()
                    raise
                async with tls:
                    await tls.send(b"ping")
                    return await tls.receive(4)

    async def test_idna_2008_certificate_is_accepted(self):
        self.assertEqual(await self._connect_with_certificate("xn--fa-hia.de"), b"pong")

    async def test_idna_2003_alias_certificate_is_rejected(self):
        loop = asyncio.get_running_loop()
        debug = loop.get_debug()
        # A rejected handshake resets the synthetic server connection; asyncio
        # logs that expected reset as an error when debug mode is enabled.
        loop.set_debug(False)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                await self._connect_with_certificate("fass.de")
            await asyncio.sleep(0)
        finally:
            loop.set_debug(debug)


if __name__ == "__main__":
    unittest.main()
