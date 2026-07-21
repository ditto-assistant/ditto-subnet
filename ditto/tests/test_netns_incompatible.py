"""Unit fixtures for the deploy-time network-namespace compose gate.

Guards the exact Moby ``validateNetContainerMode`` conflict set so a future edit
cannot silently re-open the #191-class hole (an option the daemon refuses on a
netns joiner slipping past `docker compose config`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_netns_incompatible.py"
_spec = importlib.util.spec_from_file_location("check_netns_incompatible", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# A non-empty representative value for each forbidden compose key.
_FORBIDDEN_VALUE = {
    "extra_hosts": ["host.docker.internal:127.0.0.1"],
    "ports": ["8080:8080"],
    "expose": ["8080"],
    "hostname": "joiner",
    "dns": ["1.1.1.1"],
    "links": ["base:base"],
    "external_links": ["base:base"],
}


def _joiner(**opts: object) -> dict:
    return {
        "services": {"base": {}, "joiner": {"network_mode": "service:base", **opts}}
    }


def test_forbidden_set_matches_the_daemon_exactly() -> None:
    assert set(gate.JOINER_INCOMPATIBLE) == set(_FORBIDDEN_VALUE)


@pytest.mark.parametrize("opt", sorted(_FORBIDDEN_VALUE))
def test_each_forbidden_option_on_a_joiner_is_flagged(opt: str) -> None:
    problems = gate.find_incompatible(_joiner(**{opt: _FORBIDDEN_VALUE[opt]}))
    assert any(f"'{opt}'" in p for p in problems), problems


def test_expose_and_external_links_specifically() -> None:
    # The two options this gate previously missed (P0/P1 review findings).
    assert gate.find_incompatible(_joiner(expose=["8080"]))
    assert gate.find_incompatible(_joiner(external_links=["base:base"]))


@pytest.mark.parametrize("opt", ["dns_search", "dns_opt", "mac_address"])
def test_daemon_accepted_options_are_not_flagged(opt: str) -> None:
    # Not part of validateNetContainerMode — must never be a false positive.
    assert gate.find_incompatible(_joiner(**{opt: ["x"]})) == []


def test_empty_value_is_not_flagged() -> None:
    # The daemon refuses only non-empty values (len > 0).
    assert gate.find_incompatible(_joiner(ports=[], expose=[])) == []


def test_container_mode_is_also_covered() -> None:
    model = {
        "services": {"joiner": {"network_mode": "container:abc", "expose": ["80"]}}
    }
    assert gate.find_incompatible(model)


def test_forbidden_option_on_the_netns_owner_is_fine() -> None:
    # extra_hosts on the OWNER (no network_mode:service) is valid — that is the
    # #192 fix that moved the mapping onto sandbox-docker.
    model = {
        "services": {
            "base": {"extra_hosts": ["host.docker.internal:127.0.0.1"]},
            "joiner": {"network_mode": "service:base"},
        }
    }
    assert gate.find_incompatible(model) == []


def test_production_compose_passes_the_gate() -> None:
    compose = yaml.safe_load((_REPO / "docker-compose.yml").read_text())
    assert gate.find_incompatible(compose) == []
