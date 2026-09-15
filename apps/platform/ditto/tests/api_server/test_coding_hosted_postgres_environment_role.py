"""The Ansible role renders exactly what the native runtime parser accepts."""

import json
import os
from pathlib import Path

import pytest
import yaml

from ditto.api_server.coding_hosted_runtime_config import postgres_config
from ditto.api_server.coding_hosted_runtime_io import read_json

ROOT = Path(__file__).resolve().parents[5]
TASKS = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment/tasks/main.yml"


def _entries(host: str, password: str) -> list[str]:
    variables = yaml.safe_load(TASKS.read_text())[1]["vars"]
    document = " ".join(
        variables["coding_hosted_postgres_environment_document"].split()
    )
    # The document appends exactly one environment-sourced password entry.
    assert document == (
        "{{ (coding_hosted_postgres_environment_entries + "
        "['POSTGRES_PASSWORD=' ~ lookup('env', 'DITTO_CODING_PG_PASSWORD')]) "
        "| to_json }}"
    )
    rendered = [
        entry.replace("{{ coding_hosted_postgres_environment_host }}", host)
        for entry in variables["coding_hosted_postgres_environment_entries"]
    ]
    assert not any("{{" in entry for entry in rendered)
    return [*rendered, f"POSTGRES_PASSWORD={password}"]


def test_rendered_entries_parse_with_the_bounded_admitted_principal() -> None:
    config, entries = postgres_config(
        _entries("10.30.0.5", 's3cr3t-with=equals&json"quote')
    )
    assert (config.host, config.port, config.user, config.database) == (
        "10.30.0.5",
        5432,
        "ditto",
        "ditto_platform_prod",
    )
    assert config.password == 's3cr3t-with=equals&json"quote'
    assert (config.pool_min_size, config.pool_max_size, config.command_timeout) == (
        1,
        4,
        30.0,
    )
    assert len(entries) == 8


def test_parser_rejects_an_empty_password_the_role_also_refuses() -> None:
    with pytest.raises(ValueError):
        postgres_config(_entries("10.30.0.5", ""))


def test_ansible_to_json_bytes_load_through_the_private_reader(tmp_path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    path = directory / "postgres-environment.json"
    # Ansible's to_json is json.dumps with default separators and ASCII escapes;
    # the folded document carries no trailing newline.
    body = json.dumps(_entries("10.30.0.5", "p\u00e4ss/\\word"))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
    config, _ = postgres_config(read_json(path, 128 << 10))
    assert config.password == "p\u00e4ss/\\word"
