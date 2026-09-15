"""The Ansible role renders exactly what the native runtime parser accepts.

The role writes the three worker-owned credential files. This test renders each
one the way the role's set_fact does -- the fixed non-secret settings verbatim
from the role, plus stand-in secrets -- and feeds the bytes to the real runtime
parsers, so a drift between the role and the loader fails here rather than on the
host. It reads no production secret; every credential is an obvious stand-in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from ditto.api_server.coding_hippius_evidence import (
    parse_hippius_sealed_evidence_config,
)
from ditto.api_server.coding_hippius_retrieval import (
    parse_hippius_private_input_retrieval_config,
)
from ditto.api_server.coding_hosted_runtime_config import (
    HIPPIUS_KEYS,
    HostedRuntimeImageStorage,
)
from ditto.api_server.coding_hosted_runtime_io import read_json, read_private

ROOT = Path(__file__).resolve().parents[5]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials"
MATERIALIZE = yaml.safe_load((ROLE / "tasks/materialize.yml").read_text())

# Obvious stand-ins with the shapes the runtime requires; never a real value.
STANDINS = {
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": "hip_reader_access_standin",  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": "reader-secret-standin",  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": "hip_curator_access_standin",  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_evidence_access_standin",  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": "evidence-secret-standin",  # noqa: E501
    "DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY": "GOOG_IMAGE_ACCESS_STANDIN",
    "DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY": "image-secret-standin",
    "DITTO_CODING_WORKER_PROVIDER_KEY": "sk-or-v1-provider-standin",
}


def _render_task() -> dict:
    (task,) = [
        task
        for task in MATERIALIZE
        if task.get("name", "").startswith("Render the three")
    ]
    return task["ansible.builtin.set_fact"][
        "coding_hosted_worker_credentials_documents"
    ]


def _fixed_settings() -> tuple[dict[str, str], dict[str, str]]:
    """The non-secret literals the role bakes into the two JSON documents."""
    documents = _render_task()
    hippius: dict[str, str] = {}
    image: dict[str, str] = {}
    # The set_fact bodies are Jinja dict literals; pull out each 'key': 'value'
    # pair whose value is a plain quoted literal (not a secret variable).
    import re

    pairs = (
        (documents["hippius-environment.json"], hippius),
        (documents["image-storage.json"], image),
    )
    for body, sink in pairs:
        for key, value in re.findall(r"'([^']+)':\s*'([^']*)'", body):
            sink[key] = value
    return hippius, image


def _documents() -> dict[str, str]:
    """Render the three documents exactly as the role's set_fact does."""
    hippius_fixed, image_fixed = _fixed_settings()
    hippius = {
        **hippius_fixed,
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": STANDINS[
            "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY"
        ],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": STANDINS[
            "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY"
        ],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": STANDINS[
            "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY"
        ],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": STANDINS[
            "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY"
        ],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": STANDINS[
            "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY"
        ],
    }
    image = {
        **image_fixed,
        "access_key": STANDINS["DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY"],
        "secret_key": STANDINS["DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY"],
    }
    # Ansible's to_json(sort_keys=True) is json.dumps with sorted keys.
    return {
        "hippius": json.dumps(hippius, sort_keys=True),
        "image": json.dumps(image, sort_keys=True),
        "provider": STANDINS["DITTO_CODING_WORKER_PROVIDER_KEY"],
    }


def _write_private(path: Path, body: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)


def test_fixed_hippius_settings_match_the_probe_and_runtime() -> None:
    hippius_fixed, _ = _fixed_settings()
    assert hippius_fixed == {
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
        "DITTO_CODING_HIPPIUS_REGION": "decentralized",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": "ditto-subnet-coding-private-input",  # noqa: E501
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": "ditto-subnet-coding-sealed-evidence",  # noqa: E501
        "DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS": "20",
    }
    # The curator secret key is never a key in the document, only the access id.
    assert (
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY"
        not in _documents()["hippius"]
    )


def test_hippius_document_parses_as_the_runtime_loads_it() -> None:
    document = json.loads(_documents()["hippius"])
    # The loader's own object-shape gate.
    assert isinstance(document, dict)
    assert not set(document) - HIPPIUS_KEYS
    assert all(type(value) is str and value for value in document.values())
    reader = parse_hippius_private_input_retrieval_config(document)
    evidence = parse_hippius_sealed_evidence_config(document)
    # The loader requires distinct identities and buckets.
    assert reader.bucket != evidence.bucket
    assert reader.reader.access_key != evidence.mediator.access_key
    assert reader.curator_access_key_id != evidence.mediator.access_key
    assert reader.curator_access_key_id != reader.reader.access_key
    assert reader.region == "decentralized"
    assert reader.endpoint_url == "https://s3.hippius.com"


def test_image_storage_document_parses_and_is_https() -> None:
    from urllib.parse import urlparse

    document = json.loads(_documents()["image"])
    assert set(document) == {
        "endpoint_url",
        "bucket",
        "access_key",
        "secret_key",
        "region",
    }
    image = HostedRuntimeImageStorage.model_validate(document)
    endpoint = urlparse(image.endpoint_url)
    assert endpoint.scheme == "https"
    assert endpoint.hostname and endpoint.port in {None, 443}
    assert not (
        endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
    )
    assert image.bucket == "ditto-platform-agents-prod"


def test_provider_key_is_printable_ascii_without_a_trailing_newline() -> None:
    provider = _documents()["provider"]
    assert provider and all(33 <= ord(character) <= 126 for character in provider)
    assert "\n" not in provider


def test_rendered_files_load_through_the_private_reader(tmp_path) -> None:
    tmp_path.chmod(0o700)
    documents = _documents()
    hippius_path = tmp_path / "hippius-environment.json"
    image_path = tmp_path / "image-storage.json"
    provider_path = tmp_path / "provider-key"
    _write_private(hippius_path, documents["hippius"])
    _write_private(image_path, documents["image"])
    _write_private(provider_path, documents["provider"])
    # The runtime opens each file through read_private/read_json, which require a
    # regular single-link mode-0600 file below a mode-0700 directory.
    hippius = read_json(hippius_path, 65536)
    parse_hippius_private_input_retrieval_config(hippius)
    parse_hippius_sealed_evidence_config(hippius)
    HostedRuntimeImageStorage.model_validate(read_json(image_path, 65536))
    provider = read_private(provider_path, 4096).decode("ascii")
    assert provider == documents["provider"]


def test_role_never_looks_up_the_curator_secret_key() -> None:
    tasks = (ROLE / "tasks/materialize.yml").read_text()
    assert "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY" in tasks
    # It appears only in the refusal, never in a document or a captured value.
    render = json.dumps(_render_task())
    assert "CURATOR_SECRET" not in render
    assert "CURATOR_ACCESS_KEY" in render


@pytest.mark.parametrize(
    "env_name",
    sorted(STANDINS),
)
def test_every_credential_is_a_controller_environment_variable(env_name: str) -> None:
    # Each secret enters only through its DITTO_CODING_WORKER_* environment name.
    tasks = (ROLE / "tasks/materialize.yml").read_text()
    assert f"lookup('env', '{env_name}')" in tasks
