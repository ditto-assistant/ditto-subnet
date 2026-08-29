"""Create the deterministic //Alice wallet used only by isolated previews."""

from __future__ import annotations

import bittensor


def main() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    wallet = bittensor.Wallet(name="preview", hotkey="default")
    wallet.set_hotkey(keypair, encrypt=False, overwrite=True)
    wallet.set_coldkey(keypair, encrypt=False, overwrite=True)


if __name__ == "__main__":
    main()
