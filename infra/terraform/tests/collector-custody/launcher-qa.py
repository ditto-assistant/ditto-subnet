"""QA-only interception of rendered launcher; real SDK/public fixture derive.

Never installed by production bootstrap. Cloud is mocked; key generation is
replaced by a fixed public BIP39 vector. No custody secret or live cloud call.
"""

import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

assert sys.argv[1] == "-I"
script = Path(sys.argv[2])
assert script == Path("/opt/sn118-collector/scripts/collector_delegate_key.py")
args = sys.argv[3:]
role = os.environ["QA_ROLE"]
mode = args[args.index("--mode") + 1]
assert args == [
    "--project",
    "test-project",
    "--role",
    role,
    "--mode",
    mode,
    *[part for c in "BCDEF" for part in ("--forbidden-address", "5" + c * 47)],
    "--confirm",
    f"{mode.upper()} GCP COLLECTOR {role.upper()} DELEGATE",
]
spec = importlib.util.spec_from_file_location("custody_under_test", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_keypair = module.keypair_type()
assert importlib.metadata.version("bittensor-wallet") == "4.1.1"
FIXTURE = " ".join(["abandon"] * 23 + ["art"])


class PublicFixtureAdapter:
    @staticmethod
    def generate_mnemonic(*, n_words):
        assert n_words == 24
        return FIXTURE

    @staticmethod
    def create_from_mnemonic(mnemonic):
        assert mnemonic == FIXTURE
        return real_keypair.create_from_mnemonic(mnemonic)


class StubCloud:
    def __init__(self, project, selected_role, selected_mode):
        assert (project, selected_role, selected_mode) == ("test-project", role, mode)
        self.role = role
        self.parent = f"projects/123456/secrets/sn118-collector-{role}-delegate"

    def assert_empty(self):
        pass

    def add(self, mnemonic):
        assert mnemonic == FIXTURE

    def access(self):
        return FIXTURE


module.Cloud = StubCloud
module.keypair_type = lambda: PublicFixtureAdapter
sys.argv = [str(script), *args]
raise SystemExit(module.main())
