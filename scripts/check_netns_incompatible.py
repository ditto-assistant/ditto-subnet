#!/usr/bin/env python3
"""Reject compose services that carry network identity while joining another
container's network namespace.

``docker compose config`` only PARSES the model; it never asks the daemon to
create the containers, so it accepts options the daemon refuses at deploy time.
A service with ``network_mode: service:X`` / ``container:X`` shares X's network
stack and cannot carry its own network identity. Moby's
``validateNetContainerMode`` (v28 ``runconfig/hostconfig.go``) refuses EXACTLY:
``Hostname``, ``Links``, ``DNS``, ``ExtraHosts``, ``PortBindings``,
``PublishAllPorts``, and ``ExposedPorts``. In compose keys that is ``hostname``,
``links`` **and** ``external_links`` (both fold into ``HostConfig.Links``),
``dns``, ``extra_hosts``, ``ports``, and ``expose``. ``dns_search`` /
``dns_opt`` / ``mac_address`` / ``networks`` are NOT part of that daemon
check (verified on Docker 29.4 / Moby v28) and are deliberately excluded so the
gate never raises a false positive.

v0.19.1 shipped ``extra_hosts`` on such a service, passed ``config`` green, and
was un-deployable on the whole fleet. This gate makes a repeat fail the PR
instead of the validators. Run it against the fully-resolved model:

    docker compose config --format json >model.json
    python3 scripts/check_netns_incompatible.py model.json
"""

from __future__ import annotations

import json
import sys

# Compose keys that each map to a HostConfig field Moby refuses on a container
# that joins another's network namespace.
JOINER_INCOMPATIBLE: tuple[str, ...] = (
    "extra_hosts",  # HostConfig.ExtraHosts
    "ports",  # HostConfig.PortBindings
    "expose",  # HostConfig.ExposedPorts
    "hostname",  # HostConfig.Hostname
    "dns",  # HostConfig.DNS
    "links",  # HostConfig.Links
    "external_links",  # also HostConfig.Links (kept separate by compose)
)


def _joins_network_namespace(mode: object) -> bool:
    return isinstance(mode, str) and mode.split(":", 1)[0] in ("service", "container")


def find_incompatible(model: dict) -> list[str]:
    """Return one message per netns-joining service that carries a forbidden key."""
    problems: list[str] = []
    for name, svc in (model.get("services") or {}).items():
        if not isinstance(svc, dict) or not _joins_network_namespace(
            svc.get("network_mode")
        ):
            continue
        mode = svc.get("network_mode")
        for opt in JOINER_INCOMPATIBLE:
            # Truthiness (not ``is not None``) is deliberate: the daemon refuses
            # only NON-EMPTY values (Moby checks ``len(...) > 0``), so an
            # explicit-but-empty ``ports: []`` is accepted and must not be flagged.
            if svc.get(opt):
                problems.append(
                    f"service '{name}': '{opt}' is invalid with "
                    f"network_mode: {mode} (the daemon rejects it at container "
                    f"create; put it on the netns owner instead)"
                )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: check_netns_incompatible.py <resolved-compose.json>",
            file=sys.stderr,
        )
        return 2
    with open(argv[1]) as handle:
        model = json.load(handle)
    problems = find_incompatible(model)
    if problems:
        print(
            "network-namespace-incompatible compose options — this stack "
            "would fail to deploy:"
        )
        for problem in problems:
            print("  -", problem)
        return 1
    print("compose network-namespace options OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
