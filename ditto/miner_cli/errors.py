"""Errors raised by the miner CLI.

Each subclass carries a "This can happen when:" docstring per
``context-docs/practices/CODE-QUALITY-STANDARDS.md §122-167`` so the
``__main__`` exit-code mapper can document what each non-zero exit
means without callers chasing through implementation files.
"""

from __future__ import annotations


class MinerCliError(Exception):
    """Base exception for miner CLI errors.

    All typed errors raised inside :mod:`ditto.miner_cli` inherit from
    this so :mod:`ditto.miner_cli.__main__` can catch one symbol and
    map subclasses to exit codes.
    """

    pass


# --- Pre-flight errors ---


class TarStructureError(MinerCliError):
    """Raised when the supplied tarball cannot be parsed as a valid archive.

    This can happen when:
    - The path supplied to ``ditto upload`` / ``ditto verify`` does not exist
      or is not a regular file.
    - The file exceeds the 200 MB upload limit enforced by the API (the CLI
      checks locally so miners do not pay for an upload the server will reject).
    - The gzip layer is invalid (truncated download, wrong format).
    - The inner tar cannot be opened by :mod:`tarfile`.
    """

    pass


class ManifestError(MinerCliError):
    """Raised when the harness manifest is missing or unparseable.

    This can happen when:
    - The expected manifest file is not present at the documented path
      inside the tar.
    - The manifest fails to parse (malformed format) or lacks required fields.

    NOTE: real manifest enforcement is currently logged-only pending the
    ``ditto-harness/interface/`` repo. Stubbed deferred checks log a
    warning rather than raising.
    """

    pass


class ImportAllowlistError(MinerCliError):
    """Raised when the harness imports Go packages outside the allowlist.

    This can happen when:
    - The harness imports a package that is not on the allowlist file.

    NOTE: real allowlist enforcement is currently logged-only pending the
    allowlist file shipping with the harness interface repo.
    """

    pass


class SchemaDriftError(MinerCliError):
    """Raised when the harness sqlite schema diverges from the reference schema.

    This can happen when:
    - The shipped tarball's schema file does not match
      ``schema/initial_harness.sql``.

    NOTE: real schema-diff enforcement is currently logged-only pending
    the reference schema file landing in-repo.
    """

    pass


# --- Wallet errors ---


class WalletNotFoundError(MinerCliError):
    """Raised when the named wallet cannot be located on disk.

    This can happen when:
    - The ``--coldkey-name`` / ``--hotkey-name`` (or env-var equivalents)
      refer to a wallet that does not exist under ``~/.bittensor/wallets/``.
    - The wallet path is overridden via ``BT_WALLET_PATH`` to a directory
      that does not contain the expected coldkey + hotkey keyfiles.
    """

    pass


class WalletDecryptError(MinerCliError):
    """Raised when an encrypted wallet keyfile cannot be unlocked.

    This can happen when:
    - The interactive keyfile password prompt fails (terminal closed,
      EOF on stdin) before the bittensor SDK can decrypt.
    - The user enters the wrong password and the SDK raises a decrypt
      exception that propagates to the CLI.
    """

    pass


# --- Chain errors ---


class PaymentSubmissionError(MinerCliError):
    """Raised when the upload-fee extrinsic cannot be submitted to chain.

    This can happen when:
    - The coldkey lacks enough free TAO to cover the upload fee plus the
      tip / network fee.
    - The substrate node rejects the extrinsic at submission time
      (invalid signature, wrong nonce, replay).
    - The configured subtensor network is unreachable.
    """

    pass


class PaymentFinalizationTimeoutError(MinerCliError):
    """Raised when an accepted extrinsic does not finalize in time.

    This can happen when:
    - The extrinsic was accepted into a block but the chain did not
      reach finality on that block within the configured timeout.
    - Network conditions stalled finalization across the relay chain.
    """

    pass


# --- API errors ---


class ApiResponseError(MinerCliError):
    """Raised when the API returns a non-2xx response with a typed envelope.

    Concrete subclasses map to specific envelope codes. The base class
    is kept catchable for callers that just want to bail on any API
    failure (e.g. the ``--json`` status path).

    This can happen when:
    - Any API endpoint returns a non-success status with an
      ``error_code`` envelope body the CLI did not expect.
    """

    pass


class PreCheckRejectedError(ApiResponseError):
    """Raised when ``/upload/check`` returns a definitive rejection.

    This can happen when:
    - The signature does not verify against the supplied hotkey.
    - The hotkey is not registered on the netuid the API is bound to.
    - The hotkey is on the banned list (when that table lands; not in MVP).
    - The tar manifest validation rejects the file (when manifest
      enforcement lands).
    """

    pass


class UploadAgentRejectedError(ApiResponseError):
    """Raised when ``/upload/agent`` returns non-2xx after payment was made.

    This can happen when:
    - The payment proof refers to an extrinsic that is not on the chain
      the API is bound to (wrong-network upload attempt).
    - The payment proof has already been consumed by an earlier upload
      (replay protection: ``(block_hash, extrinsic_index)`` PK on
      ``evaluation_payments``).
    - The signed sha256 does not match the uploaded tar's sha256.

    Recovery hint: the payment is already on chain. CLI surfaces the
    ``(block_hash, extrinsic_index)`` so support can investigate.
    """

    pass


class AgentNotFoundError(ApiResponseError):
    """Raised when ``/retrieval/agent/{id}/status`` returns 404.

    This can happen when:
    - The supplied agent UUID is well-formed but has never been seen
      by this deployment (mistyped id, copied across deployments).
    - An admin action deleted the row between the prior upload response
      and this lookup.
    """

    pass


class HotkeyAgentNotFoundError(ApiResponseError):
    """Raised when ``/retrieval/agent-by-hotkey`` returns 404.

    This can happen when:
    - The hotkey has never submitted an upload (fresh miner).
    - All prior submissions from the hotkey were deleted by admin action.
    """

    pass


# --- User-action errors ---


class PaymentCancelledError(MinerCliError):
    """Raised when the miner declines the interactive payment confirmation.

    This can happen when:
    - The interactive ``Confirm payment? [y/N]`` prompt receives anything
      other than ``y`` (including empty input).
    - The user sends EOF / Ctrl-D at the prompt.
    """

    pass
