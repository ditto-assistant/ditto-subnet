"""Exercise the actual writer consumed by the protected workflow."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "phase_input", ROOT / "scripts/collector-custody-phase-input.py"
)
assert spec and spec.loader
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)


class TypedInput(unittest.TestCase):
    def test_writes_actual_boolean_not_literal_cli_string(self):
        with tempfile.TemporaryDirectory() as folder:
            for phase, rpc in (
                ("bootstrap", "false"),
                ("armed", "false"),
                ("locked", "false"),
                ("sealed", "false"),
                ("sealed", "true"),
            ):
                with self.subTest(phase=phase, rpc=rpc):
                    output = Path(folder) / (phase + rpc + ".json")
                    writer.write_phase(phase, rpc, output)
                    value = json.loads(output.read_text())
                    self.assertIs(value["collector_runtime_rpc_egress"], rpc == "true")
                    self.assertEqual(
                        value["collector_custody_phases"],
                        {"registration": phase, "transfer": phase},
                    )
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_refuses_unknown_rpc_unsealed_and_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "phase.json"
            for phase, rpc in (
                ("unknown", "false"),
                ("armed", "true"),
                ("sealed", "False"),
            ):
                with self.assertRaises(ValueError):
                    writer.write_phase(phase, rpc, output)
                self.assertFalse(output.exists())
            writer.write_phase("sealed", "false", output)
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                writer.write_phase("sealed", "true", output)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
