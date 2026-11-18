"""``ditto model-context``: the CPM context-model competition (shadow).

Miners submit a small open-weight model (a LoRA or a full fine-tune of an
allowlisted Qwen3.5 base) that ranks and prunes Ditto's context. The contract
is ``docs/research/cpm/competition-v1.md``. The track is scored and shown on a
board but pays nothing while it is in shadow.

* ``manifest DIR``: hash the model files and write ``cpm-manifest.json``
  (contract section 2.3).
* ``commit --manifest PATH``: write the on-chain commitment
  ``dcpm1:<manifest_sha256>`` for your hotkey (section 3.1). Its block is your
  priority.
* ``submit --repo OWNER/NAME --revision SHA --manifest PATH``: sign the
  section 3.2 payload and submit the public Hugging Face revision.
* ``status`` / ``board``: your submissions and the current season's board.
* ``practice MODEL``: run ``cpm-scorer score`` locally, the scorer's own path.

The order is: push the weights to a private repo, ``manifest``, push the
manifest, ``commit``, make the repo public, ``submit``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import prompt_input
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

# Wire constants. Platform pins the same values in
# ``apps/platform/ditto/api_models/model_context.py``; the tests check both.
SIGNATURE_DOMAIN = "ditto-model-context-v1"
COMMITMENT_PREFIX = "dcpm1:"
MANIFEST_SCHEMA = "ditto-cpm-manifest-v1"
MANIFEST_FILENAME = "cpm-manifest.json"
TRACK = "model_context"
ALLOWED_LICENSES = ("apache-2.0", "mit")
KINDS = ("lora", "full")
KNOWN_BASES = ("qwen3.5-0.8b", "qwen3.5-2b")
DEFAULT_NETUID = 118
MAX_ATTESTATION_CHARS = 4_000
MAX_MANIFEST_BYTES = 64 << 10
CHUNK_BYTES = 1 << 20

LORA_FILES = ("adapter_config.json", "adapter_model.safetensors")
FULL_CONFIG = "config.json"
FULL_INDEX = "model.safetensors.index.json"
_FULL_SHARD = re.compile(r"^model[A-Za-z0-9._-]*\.safetensors$")
_BASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HF_REPO = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Code and pickle formats the scorer refuses anywhere in the repo.
FORBIDDEN_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".pkl",
    ".pickle",
    ".bin",
    ".pt",
    ".pth",
    ".ckpt",
    ".joblib",
    ".npy",
    ".npz",
    ".h5",
    ".msgpack",
)


class ModelContextError(MinerCliError):
    """A model_context command refused its input or could not finish."""


# --- wire format ---------------------------------------------------------------


def build_signature_payload(
    *,
    hotkey: str,
    hf_repo: str,
    revision: str,
    manifest_sha256: str,
    ts: int,
    nonce: UUID | str,
) -> bytes:
    """The exact bytes a submission signs (contract section 3.2)."""
    return (
        f"{SIGNATURE_DOMAIN}:{hotkey}:{hf_repo}:{revision}:"
        f"{manifest_sha256}:{ts}:{nonce}"
    ).encode("ascii")


def commitment_value(manifest_sha256: str) -> str:
    """The on-chain ``Commitments`` text for a manifest (contract section 3.1)."""
    if not _SHA256.fullmatch(manifest_sha256):
        raise ModelContextError("manifest_sha256 must be 64 lowercase hex digits")
    return f"{COMMITMENT_PREFIX}{manifest_sha256}"


def manifest_sha256(raw: bytes) -> str:
    """SHA-256 of the manifest file's exact bytes (contract section 2.3)."""
    return hashlib.sha256(raw).hexdigest()


def submission_body(
    *,
    hotkey: str,
    hf_repo: str,
    revision: str,
    manifest_text: str,
    signature_hex: str,
    ts: int,
    nonce: UUID | str,
) -> dict[str, Any]:
    """The ``POST /model-context/submissions`` body (contract section 5.3).

    ``manifest`` is the file's exact text: Platform hashes these bytes, so it
    is never re-serialized.
    """
    return {
        "hotkey": hotkey,
        "hf_repo": hf_repo,
        "revision": revision,
        "manifest": manifest_text,
        "signature": signature_hex,
        "ts": ts,
        "nonce": str(nonce),
    }


# --- manifest --------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def select_files(directory: Path, kind: str) -> list[str]:
    """The root files a ``kind`` submission lists (contract section 2.2)."""
    names = sorted(p.name for p in directory.iterdir() if p.is_file())
    if kind == "lora":
        missing = [name for name in LORA_FILES if name not in names]
        if missing:
            raise ModelContextError(f"a lora submission needs {missing} in {directory}")
        return list(LORA_FILES)
    if FULL_CONFIG not in names:
        raise ModelContextError(f"a full submission needs config.json in {directory}")
    shards = [name for name in names if _FULL_SHARD.fullmatch(name)]
    if not shards:
        raise ModelContextError(
            f"a full submission needs model*.safetensors shards in {directory}"
        )
    extra = [FULL_INDEX] if FULL_INDEX in names else []
    return sorted([FULL_CONFIG, *extra, *shards])


def forbidden_files(directory: Path) -> list[str]:
    """Code or pickle files anywhere under ``directory`` (contract section 2.2)."""
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(directory).parts
        and path.name.lower().endswith(FORBIDDEN_SUFFIXES)
    )


def build_manifest(
    directory: Path,
    *,
    base_id: str,
    kind: str,
    license_id: str,
    attestation: str,
) -> bytes:
    """``cpm-manifest.json`` bytes for the model in ``directory``."""
    if not _BASE_ID.fullmatch(base_id):
        raise ModelContextError(f"--base {base_id!r} is not a base id")
    if kind not in KINDS:
        raise ModelContextError(f"--kind must be one of {list(KINDS)}")
    if license_id not in ALLOWED_LICENSES:
        raise ModelContextError(f"--license must be one of {list(ALLOWED_LICENSES)}")
    if len(attestation) > MAX_ATTESTATION_CHARS:
        raise ModelContextError(
            f"--attestation is longer than {MAX_ATTESTATION_CHARS} characters"
        )
    if not directory.is_dir():
        raise ModelContextError(f"{directory} is not a directory")
    forbidden = forbidden_files(directory)
    if forbidden:
        raise ModelContextError(
            "remove code and pickle files before publishing this repo (the "
            f"scorer refuses them anywhere in it): {forbidden}"
        )
    files = []
    for name in select_files(directory, kind):
        path = directory / name
        files.append(
            {"path": name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
        )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "track": TRACK,
        "base_id": base_id,
        "kind": kind,
        "files": files,
        "license": license_id,
        "training_data_attestation": attestation,
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def read_manifest(path: Path) -> tuple[str, str]:
    """``(exact text, manifest_sha256)`` of a manifest file.

    Read as bytes, never in text mode: newline translation would change the
    bytes the commitment and Platform hash.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ModelContextError(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ModelContextError(f"{path} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelContextError(f"{path} is not a UTF-8 JSON manifest") from exc
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ModelContextError(f"{path} is not a {MANIFEST_SCHEMA} manifest")
    return text, manifest_sha256(raw)


# --- chain -----------------------------------------------------------------------


def _commitment_bytes(raw: Any) -> bytes | None:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        try:
            return bytes.fromhex(raw.removeprefix("0x"))
        except ValueError:
            return None
    if isinstance(raw, (list, tuple)) and all(
        type(b) is int and 0 <= b <= 255 for b in raw
    ):
        return bytes(raw)
    return None


def decode_commitment(value: Any) -> tuple[int, bytes] | None:
    """``(block, data)`` from a decoded ``Commitments.CommitmentOf`` value.

    The same reading as Platform's ``decode_commitment_registration``: the
    first ``Raw*`` field of ``info.fields``, whose list the decoder may wrap
    once more.
    """
    if not isinstance(value, dict):
        return None
    block = value.get("block")
    if type(block) is not int or block < 0:
        return None
    info = value.get("info")
    fields = info.get("fields") if isinstance(info, dict) else None
    while (
        isinstance(fields, (list, tuple))
        and len(fields) == 1
        and isinstance(fields[0], (list, tuple))
    ):
        fields = fields[0]
    if not isinstance(fields, (list, tuple)) or not fields:
        return block, b""
    first = fields[0]
    if isinstance(first, dict) and len(first) == 1:
        key, raw = next(iter(first.items()))
        if str(key).startswith("Raw"):
            data = _commitment_bytes(raw)
            return block, data if data is not None else b""
    return block, b""


def set_commitment(
    *, wallet: Any, chain_target: str, netuid: int, value: str
) -> int | None:
    """Write ``value`` with ``Commitments.set_commitment``; return its block.

    Waits for finalization, then reads the commitment back so the block
    printed is the one Platform reads as the submission's priority. ``None``
    when the write landed but the read-back did not show it.
    """
    import bittensor

    try:
        subtensor = bittensor.Subtensor(network=chain_target)
    except Exception as exc:
        raise ModelContextError(
            f"could not connect to subtensor {chain_target!r}: {exc}"
        ) from exc
    response = subtensor.set_commitment(
        wallet=wallet,
        netuid=netuid,
        data=value,
        wait_for_inclusion=True,
        wait_for_finalization=True,
    )
    if not getattr(response, "success", False):
        raise ModelContextError(
            f"set_commitment failed: {getattr(response, 'message', response)}"
        )
    stored = subtensor.get_commitment_metadata(netuid, wallet.hotkey.ss58_address)
    decoded = decode_commitment(stored)
    if decoded is None or decoded[1] != value.encode("ascii"):
        return None
    return decoded[0]


# --- commands --------------------------------------------------------------------


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "model-context",
        help="Context-model (CPM) competition: manifest, commit, submit.",
        description=(
            "Submit small open-weight context models (LoRA or full fine-tunes "
            "of an allowlisted Qwen3.5 base). Weight-zero while the "
            "competition is in shadow."
        ),
        parents=parents or [],
    )
    subs = parser.add_subparsers(dest="model_context_command", required=True)

    manifest = subs.add_parser(
        "manifest", help="Hash the model files and write cpm-manifest.json."
    )
    manifest.add_argument("dir", type=Path)
    manifest.add_argument(
        "--base",
        required=True,
        help=f"Base model id, e.g. {' or '.join(KNOWN_BASES)}.",
    )
    manifest.add_argument("--kind", required=True, choices=KINDS)
    manifest.add_argument(
        "--license", dest="license_id", default="apache-2.0", choices=ALLOWED_LICENSES
    )
    manifest.add_argument(
        "--attestation",
        required=True,
        help="Where the training data came from. Shown to reviewers.",
    )
    manifest.add_argument(
        "-o",
        "--out",
        type=Path,
        help=f"Write here instead of DIR/{MANIFEST_FILENAME}.",
    )
    manifest.set_defaults(func=run)

    commit = subs.add_parser(
        "commit",
        help="Write the on-chain commitment dcpm1:<manifest_sha256> for your hotkey.",
    )
    commit.add_argument("--manifest", type=Path, required=True)
    commit.add_argument(
        "--netuid",
        type=int,
        # A string default is converted by ``type`` only when this command is
        # parsed, so a bad NETUID cannot break --help or other commands.
        default=os.environ.get("NETUID") or str(DEFAULT_NETUID),
        help=f"Subnet to commit on. Env: NETUID. Defaults to {DEFAULT_NETUID}.",
    )
    _wallet_flags(commit)
    commit.set_defaults(func=run)

    submit = subs.add_parser(
        "submit", help="Sign and submit a public Hugging Face revision."
    )
    submit.add_argument("--repo", required=True, help="Hugging Face OWNER/NAME.")
    submit.add_argument(
        "--revision", required=True, help="Full 40-hex commit SHA, not a branch."
    )
    submit.add_argument("--manifest", type=Path, required=True)
    _wallet_flags(submit)
    submit.set_defaults(func=run)

    status = subs.add_parser("status", help="List a hotkey's submissions.")
    status.add_argument("--ss58", dest="ss58", help="Hotkey address to list.")
    _wallet_flags(status)
    status.set_defaults(func=run)

    board = subs.add_parser("board", help="Show the current season's board.")
    board.set_defaults(func=run)

    practice = subs.add_parser(
        "practice", help="Score a model locally with cpm-scorer (the scorer's path)."
    )
    practice.add_argument(
        "model", help="'base', a model directory, or HF_REPO@REVISION."
    )
    practice.add_argument("--base", default=KNOWN_BASES[0])
    practice.add_argument(
        "--seeds", help="Comma-separated public seeds (default: the scorer's)."
    )
    practice.add_argument("--device", help="cpu, cuda, cuda:N or mps.")
    practice.add_argument(
        "--limit-cases", type=int, help="First N memory and N tool cases."
    )
    practice.add_argument(
        "--scorer-bin",
        default=os.environ.get("DITTO_CPM_SCORER_BIN"),
        help="Path to cpm-scorer. Default: cpm-scorer on PATH.",
    )
    practice.set_defaults(func=run)
    return parser


def _wallet_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
    )
    parser.add_argument("-y", "--yes", action="store_true")


def _load_wallet(args: argparse.Namespace) -> tuple[str, Any]:
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )
    handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    return str(handle.hotkey_ss58), wallet


def _manifest(args: argparse.Namespace) -> int:
    raw = build_manifest(
        args.dir,
        base_id=args.base,
        kind=args.kind,
        license_id=args.license_id,
        attestation=args.attestation,
    )
    out = args.out or args.dir / MANIFEST_FILENAME
    out.write_bytes(raw)
    digest = manifest_sha256(raw)
    print(digest)
    print(
        f"wrote {out}\n"
        f"manifest_sha256 {digest}\n"
        f"commitment      {commitment_value(digest)}\n"
        "next: push this file to the repo root, then run "
        f"`ditto model-context commit --manifest {out}`",
        file=sys.stderr,
    )
    return 0


def _commit(args: argparse.Namespace) -> int:
    _, digest = read_manifest(args.manifest)
    value = commitment_value(digest)
    hotkey, wallet = _load_wallet(args)
    network = resolve_network(args.network)
    chain_target = getattr(args, "chain_endpoint", None) or network.subtensor_network
    print(
        "\nOn-chain commitment (Commitments.set_commitment)\n"
        f"  Hotkey:     {hotkey}\n"
        f"  Netuid:     {args.netuid}\n"
        f"  Network:    {chain_target}\n"
        f"  Commitment: {value}\n"
        "This is an extrinsic signed by the hotkey. It replaces the hotkey's\n"
        "current commitment on this subnet, and its block becomes the\n"
        "submission's priority. Keep the repo private until it lands.",
        file=sys.stderr,
    )
    if not args.yes:
        answer = prompt_input("Write this commitment? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            raise ModelContextError("cancelled; no commitment was written")
    block = set_commitment(
        wallet=wallet, chain_target=chain_target, netuid=args.netuid, value=value
    )
    if block is None:
        print(
            "commitment submitted, but reading it back did not show it yet; "
            "Platform reads the finalized head, so retry submit shortly",
            file=sys.stderr,
        )
        return 0
    print(block)
    print(
        f"committed {value} at block {block}. Make the repo public, then run "
        "`ditto model-context submit`.",
        file=sys.stderr,
    )
    return 0


def _submit(args: argparse.Namespace) -> int:
    if not _HF_REPO.fullmatch(args.repo) or ".." in args.repo:
        raise ModelContextError("--repo must be a Hugging Face OWNER/NAME")
    if not _REVISION.fullmatch(args.revision):
        raise ModelContextError(
            "--revision must be the full 40-hex commit SHA, not a branch or tag"
        )
    text, digest = read_manifest(args.manifest)
    hotkey, wallet = _load_wallet(args)
    network = resolve_network(args.network)
    with ApiClient(base_url=network.api_url) as client:
        terms = client.get_model_context_upload_terms(hotkey=hotkey)
        blockers = terms.get("blockers") or []
        if blockers:
            raise ModelContextError(
                "platform would refuse this submission: "
                + "; ".join(str(b) for b in blockers)
            )
        nonce = uuid4()
        ts = int(time.time())
        signature = wallet.hotkey.sign(
            build_signature_payload(
                hotkey=hotkey,
                hf_repo=args.repo,
                revision=args.revision,
                manifest_sha256=digest,
                ts=ts,
                nonce=nonce,
            )
        ).hex()
        result = client.post_model_context_submission(
            submission_body(
                hotkey=hotkey,
                hf_repo=args.repo,
                revision=args.revision,
                manifest_text=text,
                signature_hex=signature,
                ts=ts,
                nonce=nonce,
            )
        )
    print(result.get("submission_id"))
    print(
        f"model_context submission {result.get('status')}: base "
        f"{result.get('base_id')} kind {result.get('kind')}, commitment block "
        f"{result.get('commitment_block')}, manifest_sha256 {digest}",
        file=sys.stderr,
    )
    return 0


def practice_command(args: argparse.Namespace) -> list[str]:
    """The ``cpm-scorer score`` argv for ``practice``."""
    scorer = args.scorer_bin or shutil.which("cpm-scorer")
    if not scorer:
        raise ModelContextError(
            "cpm-scorer is not installed. From a ditto-subnet checkout with "
            "services/cpm-scorer, run `uv tool install ./services/cpm-scorer` "
            "(or `cd services/cpm-scorer && uv sync`, then pass "
            "--scorer-bin services/cpm-scorer/.venv/bin/cpm-scorer). See "
            "docs/research/cpm/competition-v1.md section 6."
        )
    argv = [scorer, "score", "--model", args.model, "--base", args.base]
    if args.seeds:
        argv += ["--seeds", args.seeds]
    if args.device:
        argv += ["--device", args.device]
    if args.limit_cases is not None:
        argv += ["--limit-cases", str(args.limit_cases)]
    return argv


def run(args: argparse.Namespace) -> int:
    command = args.model_context_command

    if command == "manifest":
        return _manifest(args)

    if command == "commit":
        return _commit(args)

    if command == "submit":
        return _submit(args)

    if command == "status":
        hotkey = args.ss58 or _load_wallet(args)[0]
        network = resolve_network(args.network)
        with ApiClient(base_url=network.api_url) as client:
            print(
                json.dumps(
                    client.list_model_context_submissions(hotkey=hotkey), indent=2
                )
            )
        return 0

    if command == "board":
        network = resolve_network(args.network)
        with ApiClient(base_url=network.api_url) as client:
            print(json.dumps(client.get_model_context_board(), indent=2))
        return 0

    if command == "practice":
        return subprocess.run(practice_command(args), check=False).returncode

    raise ModelContextError(f"unknown model-context command {command!r}")
