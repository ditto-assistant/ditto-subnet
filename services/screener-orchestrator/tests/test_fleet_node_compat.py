"""The first rolling upgrade must keep the installed old unit healthy."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from screener_capacity.fleet_node import build_parser


def test_old_installed_unit_argv_remains_accepted() -> None:
    args = build_parser().parse_args(
        [
            "--platform-url",
            "https://platform.example",
            "--credential-file",
            "/not-read/node.json",
            "--base-image",
            "/not-read/base.qcow2",
            "--builder-image",
            "registry.example/builder@sha256:" + "a" * 64,
            "--source-review-api-key-file",
            "/not-read/key",
            "--jobs-root",
            "/not-created/jobs",
            "--interval-seconds",
            "2",
            "--build-timeout-seconds",
            "2400",
            "--runtime-timeout-seconds",
            "300",
            "--source-review-timeout-seconds",
            "1800",
            "--max-workers",
            "12",
            "--local-sandbox-slots",
            "2",
            "--local-source-review-slots",
            "1",
            "--resource-slice",
            "dittoscreener.slice",
            "--vm-memory-mib",
            "10240",
            "--vm-vcpus",
            "8",
            "--vm-disk-gib",
            "80",
        ]
    )
    assert args.local_sandbox_slots == 2
    assert args.local_source_review_slots == 1
    assert args.resource_slice == "dittoscreener.slice"


def test_compat_entrypoint_stays_active_until_sigterm() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "screener_capacity.fleet_node",
            "--platform-url",
            "https://platform.example",
            "--credential-file",
            "/not-read/node.json",
            "--base-image",
            "/not-read/base.qcow2",
            "--builder-image",
            "registry.example/builder@sha256:" + "a" * 64,
            "--source-review-api-key-file",
            "/not-read/key",
            "--jobs-root",
            "/not-created/jobs",
        ],
        env=os.environ.copy(),
    )
    try:
        time.sleep(0.1)
        assert process.poll() is None
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
