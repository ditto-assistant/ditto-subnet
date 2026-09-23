from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _module(monkeypatch: pytest.MonkeyPatch) -> Any:
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "attach_single_sol_handoff", scripts / "attach_single_sol_handoff.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity() -> dict[str, object]:
    return {
        "agent_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "artifact_sha256": "a" * 64,
        "policy_version": 13,
        "manifest_digest": "b" * 64,
        "review_settings_revision": 119,
    }


def test_attach_requires_exact_one_to_one_attempt_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module(monkeypatch)
    identity = _identity()
    investigator = {**identity, "model": "openai/gpt-5.6-sol"}
    case = {**identity, "archive": "artifact.tar.gz"}
    l4 = {"policy_version": 13, "cases": [case]}
    handoff = {
        "schema_version": 1,
        "policy_version": 13,
        "items": [{**identity, "sol_investigator": investigator}],
    }
    attached = module._attach(l4, handoff)
    assert attached["cases"][0]["sol_investigator"] == investigator
    assert "sol_investigator" not in case

    for field, bad in (
        ("artifact_sha256", "c" * 64),
        ("manifest_digest", "d" * 64),
        ("review_settings_revision", 120),
    ):
        wrong = {**case, field: bad}
        with pytest.raises(ValueError, match=field):
            module._attach({**l4, "cases": [wrong]}, handoff)
    with pytest.raises(ValueError, match="lacks an exact Sol handoff"):
        module._attach({**l4, "cases": [{**case, "attempt_id": "different"}]}, handoff)
    with pytest.raises(ValueError, match="already has"):
        module._attach({**l4, "cases": [{**case, "sol_investigator": {}}]}, handoff)
    with pytest.raises(ValueError, match="duplicate exact Sol"):
        module._attach({**l4}, {**handoff, "items": handoff["items"] * 2})
    with pytest.raises(ValueError, match="unmatched exact attempt"):
        module._attach(
            {**l4, "cases": [case]},
            {
                **handoff,
                "items": [
                    *handoff["items"],
                    {
                        **identity,
                        "attempt_id": "33333333-3333-4333-8333-333333333333",
                        "sol_investigator": investigator,
                    },
                ],
            },
        )


def test_attach_reads_only_private_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"policy_version": 13}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        module._private_json(path)
    path.chmod(0o600)
    assert module._private_json(path) == {"policy_version": 13}
