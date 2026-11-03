#!/usr/bin/env python3
"""Offline public approval only. Packet builder pins all input bytes below."""

import contextlib
import getpass
import hashlib
import importlib.metadata
import json
import os
import resource
import socket
import stat
import sys
import tempfile
import warnings
from datetime import UTC, datetime
from pathlib import Path

CONFIG = None  # PACKET_CONFIGURATION
BUNDLE = Path(__file__).resolve().parent
DOMAIN = "ditto-treasury-manual-limit-v1:"


class Refusal(Exception):
    pass


def require(ok, code):
    if not ok:
        raise Refusal(code)


def safe_path(path):
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), "symlink-path")


def read_file(path, limit=16384, *, private=False):
    safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "unsafe-file")
        if private:
            require(
                info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "private-file-permissions",
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit, "oversized-file")
        return data
    finally:
        os.close(fd)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate-json-key")
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def atomic_json(path, value):
    safe_path(path)
    fd, name = tempfile.mkstemp(prefix=".gamma-public-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def status(state, code=None):
    safe_path(BUNDLE / "public")
    (BUNDLE / "public").mkdir(exist_ok=True)
    atomic_json(
        BUNDLE / "public/signing-result.json",
        {
            "status": state,
            "code": code,
            "time_utc": datetime.now(UTC).isoformat(),
            "approval_digest": CONFIG["digest"],
            "max_manual_alpha": "2500",
            "recurring_enabled": False,
            "keys_or_passwords_recorded": False,
            "transactions_signed_or_sent": 0,
        },
    )


def request():
    require(isinstance(CONFIG, dict), "prepared-packet-required")
    for name, expected in CONFIG["input_hashes"].items():
        require(
            hashlib.sha256(read_file(BUNDLE / name)).hexdigest() == expected,
            "input-file-mismatch",
        )
    body = json.loads(
        read_file(BUNDLE / "manual-limit-policy.json"), object_pairs_hook=no_duplicates
    )
    require(
        set(body)
        == {
            "version",
            "scope",
            "collector_policy_digest",
            "collector_coldkey",
            "revision",
            "min_amount_rao",
            "max_amount_rao",
            "max_sources",
        },
        "policy-fields-changed",
    )
    require(body == CONFIG["policy"], "policy-changed")
    require(
        hashlib.sha256((DOMAIN + canonical(body)).encode()).hexdigest()
        == CONFIG["digest"],
        "canonical-digest-mismatch",
    )
    return body, (DOMAIN + CONFIG["digest"]).encode()


def run():
    body, message = request()
    if sys.argv[1:] == ["--check-request"]:
        print(
            canonical(
                {
                    "digest": CONFIG["digest"],
                    "max_manual_alpha": "2500",
                    "transactions_signed_or_sent": 0,
                }
            )
        )
        return 0
    require(not sys.argv[1:], "unexpected-arguments")
    require(sys.platform == "linux" and os.getuid() != 0, "offline-linux-user-required")
    require(sys.version_info[:2] == (3, 12), "python-3.12-required")
    require(
        all(s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr)),
        "unredirected-terminal-required",
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    status("started")

    def refuse_network(*_args, **_kwargs):
        raise Refusal("network-disabled")

    socket.socket.connect = refuse_network
    socket.socket.connect_ex = refuse_network
    socket.create_connection = refuse_network
    socket.getaddrinfo = refuse_network
    install = Path.home() / ".local/share/gamma-btcli-11.1.0"
    wallets = Path.home() / ".bittensor/wallets"
    safe_path(install)
    safe_path(wallets)
    require(install.is_dir() and wallets.is_dir(), "existing-installation-missing")
    require(wallets.stat().st_dev != BUNDLE.stat().st_dev, "wallets-on-usb-refused")
    sys.path.insert(0, str(install / "site-packages"))
    require(importlib.metadata.version("bittensor") == "11.1.0", "wrong-bittensor")
    require(importlib.metadata.version("bittensor-core") == "0.1.3", "wrong-core")
    from bittensor.sp_core import (
        CRYPTO_SR25519,
        Keypair,
        decrypt_keyfile_data,
        deserialize_keypair_from_keyfile_data,
        keyfile_data_is_encrypted,
    )

    public = Keypair(ss58_address=body["collector_coldkey"])
    previous = json.loads(
        read_file(BUNDLE / "collector-approval.json"), object_pairs_hook=no_duplicates
    )
    digest = hashlib.sha256(canonical(previous["policy"]).encode()).hexdigest()
    require(
        digest == body["collector_policy_digest"]
        and public.verify(
            ("ditto-collector-policy-v1:" + digest).encode(),
            bytes.fromhex(previous["signature"].removeprefix("0x")),
        ),
        "previous-cold-approval-invalid",
    )
    target = BUNDLE / "public/manual-limit-approval.json"
    safe_path(target)
    if target.exists():
        saved = json.loads(read_file(target), object_pairs_hook=no_duplicates)
        require(
            set(saved) == {"policy", "signature"} and saved["policy"] == body,
            "existing-approval-conflict",
        )
        require(
            public.verify(message, bytes.fromhex(saved["signature"][2:])),
            "existing-approval-invalid",
        )
        status("already_signed")
        print("Verified approval already saved. Return the USB.")
        return 0
    with open("/dev/tty") as reader, open("/dev/tty", "w", buffering=1) as writer:
        writer.write("\nGAMMA MANUAL TRANSFER RANGE: UP TO 2,500 SN118 ALPHA\n")
        writer.write(
            "One manual transfer at a time, to existing approved service wallets.\n"
        )
        writer.write("Only proved earnings; retain your chosen stake reserve.\n")
        writer.write(
            "No Gamma allocation change, recurring authority or fee-limit increase.\n"
        )
        writer.write("No transaction is signed or sent. No journal migration.\n")
        writer.write(message.decode() + "\nType SIGN GAMMA MANUAL 2500 to approve: ")
        require(
            reader.readline().strip() == "SIGN GAMMA MANUAL 2500", "human-cancelled"
        )
        encrypted = read_file(wallets / "gamma-collector/coldkey", 65536, private=True)
        public_file = read_file(wallets / "gamma-collector/coldkeypub.txt")
        require(keyfile_data_is_encrypted(encrypted), "unencrypted-key-refused")
        require(
            deserialize_keypair_from_keyfile_data(public_file).ss58_address
            == body["collector_coldkey"],
            "public-wallet-mismatch",
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(
                "Collector coldkey password (hidden): ", stream=writer
            )
        require(bool(password), "empty-password")
        with (
            open(os.devnull, "w") as null,
            contextlib.redirect_stdout(null),
            contextlib.redirect_stderr(null),
        ):
            plaintext = bytes(decrypt_keyfile_data(encrypted, password))
            pair = deserialize_keypair_from_keyfile_data(plaintext)
            del plaintext, password
            require(
                pair.ss58_address == body["collector_coldkey"]
                and pair.crypto_type == CRYPTO_SR25519,
                "unlocked-key-mismatch",
            )
            signature = bytes(pair.sign(message))
            require(
                len(signature) == 64 and public.verify(message, signature),
                "signature-verification-failed",
            )
            atomic_json(target, {"policy": body, "signature": "0x" + signature.hex()})
            del pair, encrypted
        status("signed")
        os.sync()
        writer.write(
            "\nPublic approval and sanitized status saved. Eject and return the USB.\n"
        )
    return 0


if __name__ == "__main__":
    try:
        result = run()
    except Refusal as error:
        if CONFIG:
            status("refused", error.args[0])
        print("Stopped safely: " + error.args[0], file=sys.stderr)
        result = 1
    except (KeyboardInterrupt, EOFError):
        if CONFIG:
            status("cancelled", "human-cancelled")
        result = 1
    except Exception as error:
        if CONFIG:
            status("error", type(error).__name__)
        print("Stopped safely. Return USB; sanitized status saved.", file=sys.stderr)
        result = 1
    sys.exit(result)
