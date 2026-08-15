"""Repository guard for additive JSON compatibility across rolling upgrades."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "apps" / "platform" / "ditto",
    REPO_ROOT / "ditto",
    REPO_ROOT / "packages",
    REPO_ROOT / "workers",
)


def _strict_extra_config_locations() -> list[str]:
    locations: list[str] = []
    for source_root in SOURCE_ROOTS:
        for path in source_root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "extra":
                        continue
                    if isinstance(keyword.value, ast.Constant) and (
                        keyword.value.value == "forbid"
                    ):
                        relative = path.relative_to(REPO_ROOT)
                        locations.append(f"{relative}:{node.lineno}")
    return locations


def test_wire_models_do_not_forbid_additive_json_fields() -> None:
    assert _strict_extra_config_locations() == []
