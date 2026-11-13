"""Verify the Pylon image's TLS fixes using only synthetic loopback traffic."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

import anyio
import certifi
from anyio.streams.tls import TLSStream


class TLSImageTests(unittest.IsolatedAsyncioTestCase):
    def test_no_openssh_client_in_runtime(self):
        # CVE-2026-60002 is in the SSH client; keep Git for HTTPS use.
        for tool in ("ssh", "scp", "sftp"):
            with self.subTest(tool=tool):
                self.assertIsNone(shutil.which(tool))
        package = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", "openssh-client"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotIn("install ok installed", package.stdout)
        self.assertIn(
            "git version", subprocess.check_output(["git", "--version"], text=True)
        )
        self.assertEqual(subprocess.check_output(["dpkg", "--audit"], text=True), "")

    def test_uv_tools(self):
        for tool in ("uv", "uvx"):
            with self.subTest(tool=tool):
                version = subprocess.check_output([tool, "--version"], text=True)
                self.assertIn("0.13.7", version)

    def test_debian_security_packages(self):
        # linux-libc-dev supplies headers; it does not update the host kernel.
        for package, minimum in {
            "libbrotli1": "1.1.0-2+deb13u1",
            "perl": "5.40.1-6+deb13u1",
            "perl-base": "5.40.1-6+deb13u1",
            "perl-modules-5.40": "5.40.1-6+deb13u1",
            "libperl5.40": "5.40.1-6+deb13u1",
            "linux-libc-dev": "6.12.115-2",
        }.items():
            with self.subTest(package=package):
                installed = subprocess.check_output(
                    ["dpkg-query", "-W", "-f=${Version}", package], text=True
                )
                subprocess.run(
                    ["dpkg", "--compare-versions", installed, "ge", minimum], check=True
                )

    def _check_ca_fingerprints(self, fingerprints: set[str]):
        # Debian DSA-6572-1 removed both cross-signing trust anchors.
        affected = {
            # emSign ECC Root CA - G3
            "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
            # AAA Certificate Services
            "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
        }
        self.assertTrue(
            affected.isdisjoint(fingerprints), "affected CA root is trusted"
        )
        # An unaffected emSign root is a positive control against an empty store.
        self.assertIn(
            "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367",
            fingerprints,
        )

    def _check_ca_store(self, context: ssl.SSLContext):
        self._check_ca_fingerprints(
            {
                hashlib.sha256(cert).hexdigest()
                for cert in context.get_ca_certs(binary_form=True)
            }
        )

    def test_certifi_ca_store(self):
        self._check_ca_store(ssl.create_default_context(cafile=certifi.where()))

    def test_system_ca_store(self):
        self._check_ca_store(ssl.create_default_context())
        self._check_ca_store(
            ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
        )
        # OpenSSL can also lazily load hashed symlinks from this directory.
        self._check_ca_fingerprints(
            {
                hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest()
                for path in Path("/etc/ssl/certs").iterdir()
                if path.is_symlink() and path.is_file()
            }
        )

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
