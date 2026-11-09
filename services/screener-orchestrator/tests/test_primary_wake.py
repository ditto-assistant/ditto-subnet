"""The controller wakes a stopped on-demand AWS primary instead of using GCE."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from screener_capacity.aws import AwsError, InstanceState, InstanceStatus
from screener_capacity.controller import (
    ControllerError,
    Demand,
    OverflowPolicy,
    ProviderCounts,
    ProviderRouting,
    Settings,
    build_parser,
    gce_overflow_target,
    reconcile,
)
from screener_capacity.controller import _settings as controller_settings

_INSTANCE = "i-0123456789abcdef0"
_PRIMARY = "subnet-screener-3"


def _aws_routing() -> ProviderRouting:
    return ProviderRouting(
        revision=7,
        runtime_provider_priority=("aws", "gcp"),
        source_review_provider_priority=("aws", "gcp"),
        build_provider_priority=("aws", "gcp"),
        overflow=OverflowPolicy(True, _PRIMARY, 3, 12, 6),
    )


def _hetzner_routing() -> ProviderRouting:
    return ProviderRouting(
        revision=7,
        runtime_provider_priority=("hetzner", "gcp"),
        source_review_provider_priority=("hetzner", "gcp"),
        build_provider_priority=("hetzner", "gcp"),
        overflow=OverflowPolicy(True, _PRIMARY, 3, 12, 6),
    )


def _primary(*, ready: bool = False, **overrides: object) -> dict[str, object]:
    return {
        "node_id": _PRIMARY,
        "provider": "aws",
        "provider_resource_id": _INSTANCE,
        "status": "active",
        "ready": ready,
        "admission_open": True,
        "screening_concurrency": 1,
        **overrides,
    }


def _settings(root: Path) -> Settings:
    token_file = root / "controller-token"
    token_file.write_text("x" * 48)
    return Settings(
        platform_url="https://platform.invalid",
        platform_token_file=token_file,
        environment="test",
        epoch="test:epoch",
        source_sha="a" * 40,
        global_cap=6,
        jobs_per_slot=2,
        interval_seconds=30,
        state_file=root / "state.json",
        gce_project="test-project",
        gce_region="test-region",
        gce_mig="test-mig",
        gce_impersonate_service_account=None,
        lock_file=root / "lock",
        dry_run=False,
        aws_primary_instance_id=_INSTANCE,
        aws_primary_role_arn="arn:aws:iam::339713011188:role/ditto-screener-wake",
        primary_wake_grace_seconds=600,
    )


class _Platform:
    def __init__(
        self,
        *,
        runnable: int,
        primary: dict[str, object] | None,
        routing: ProviderRouting | None = None,
        fence_error: bool = False,
        operations: list[str] | None = None,
    ) -> None:
        self._demand = Demand(runnable=runnable, active=0, desired=min(6, runnable))
        self._nodes = {_PRIMARY: primary} if primary is not None else {}
        self._routing = routing or _aws_routing()
        self._fence_error = fence_error
        self.operations = operations if operations is not None else []
        self.renewed: list[dict[str, Any]] = []

    def demand(self, **_kwargs: object) -> Demand:
        return self._demand

    def provider_routing(self) -> ProviderRouting:
        return self._routing

    def node_states(self) -> dict[str, dict[str, object]]:
        return self._nodes

    def fence(self, **_kwargs: object) -> None:
        if self._fence_error and not self.renewed:
            # A fresh controller holds no lease until its first renew.
            raise ControllerError("controller epoch is not current")
        self.operations.append("fence")

    def renew(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.renewed.append(snapshot)
        return snapshot


class _GCE:
    def __init__(self, operations: list[str]) -> None:
        self._target = 0
        self.operations = operations

    def target(self) -> int:
        return self._target

    def counts(self) -> ProviderCounts:
        return ProviderCounts(healthy=self._target)

    def ensure_watchdog(self) -> None:
        pass

    def resize(self, target: int) -> None:
        self.operations.append(f"gce:{target}")
        self._target = target

    def running_instances(self) -> set[str]:
        return set()


class _EC2:
    def __init__(
        self,
        state: InstanceState,
        operations: list[str],
        *,
        launched_at: datetime | None = None,
        start_error: bool = False,
        status_error: bool = False,
    ) -> None:
        self.instance_id = _INSTANCE
        self.state = state
        self.launched_at = launched_at
        self.start_error = start_error
        self.status_error = status_error
        self.operations = operations
        self.starts = 0

    def status(self) -> InstanceStatus:
        if self.status_error:
            raise AwsError("EC2 DescribeInstances failed with HTTP 403")
        return InstanceStatus(self.state, self.launched_at)

    def start(self) -> None:
        if self.start_error:
            raise AwsError("EC2 StartInstances failed with HTTP 403")
        self.starts += 1
        self.operations.append("ec2:start")
        self.state = "pending"


def _run(
    settings: Settings, platform: _Platform, ec2: _EC2, gce: _GCE | None = None
) -> dict[str, Any]:
    with (
        patch("screener_capacity.controller.PlatformControl", return_value=platform),
        patch(
            "screener_capacity.controller.GCEFleet",
            return_value=gce or _GCE(platform.operations),
        ),
        patch("screener_capacity.controller.EC2Primary", return_value=ec2),
    ):
        return reconcile(settings)


class WakeDecisionTests(unittest.TestCase):
    def test_waking_primary_keeps_base_load_off_gce(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=4, active=0, desired=2),
            routing=_aws_routing(),
            primary_node=_primary(),
            jobs_per_slot=2,
            global_cap=6,
            primary_sleep="waking",
        )
        self.assertEqual((target, reason), (0, "AWS_PRIMARY_WAKING"))

    def test_waking_primary_still_overflows_excess_backlog(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=20, active=0, desired=6),
            routing=_aws_routing(),
            primary_node=_primary(),
            jobs_per_slot=2,
            global_cap=6,
            primary_sleep="waking",
        )
        self.assertEqual((target, reason), (4, "AWS_BACKLOG_OVERFLOW"))

    def test_asleep_primary_with_no_work_is_not_an_outage(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=0, active=0, desired=0),
            routing=_aws_routing(),
            primary_node=_primary(),
            jobs_per_slot=2,
            global_cap=6,
            primary_sleep="asleep",
        )
        self.assertEqual((target, reason), (0, "AWS_PRIMARY_ASLEEP"))

    def test_closed_admission_wins_over_waking(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=4, active=0, desired=2),
            routing=_aws_routing(),
            primary_node=_primary(admission_open=False, screening_concurrency=0),
            jobs_per_slot=2,
            global_cap=6,
            primary_sleep="waking",
        )
        self.assertEqual((target, reason), (0, "AWS_PRIMARY_ADMISSION_CLOSED"))


class ReconcileWakeTests(unittest.TestCase):
    def test_stopped_primary_with_work_is_started_under_the_fence(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(platform.operations[:2], ["fence", "ec2:start"])
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_WAKING")
            self.assertEqual(snapshot["gce_target"], 0)
            self.assertNotIn("gce:", " ".join(platform.operations))
            events = platform.renewed[0]["events"]
            self.assertEqual(events[0]["event_type"], "primary_wake_requested")
            self.assertEqual(events[0]["provider"], "aws")
            state = json.loads(settings.state_file.read_text())
            self.assertIsNotNone(state["primary_wake_started_at"])

    def test_waking_primary_is_not_started_twice_within_its_grace(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)
            _run(settings, platform, ec2)

            second = _run(settings, _Platform(runnable=3, primary=_primary()), ec2)

            self.assertEqual(ec2.starts, 1)
            self.assertEqual(second["fallback_reason"], "AWS_PRIMARY_WAKING")

    def test_primary_still_unready_after_its_grace_overflows_to_gce(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            settings.state_file.write_text(
                json.dumps(
                    {
                        "primary_wake_started_at": (
                            datetime.now(UTC) - timedelta(minutes=11)
                        ).isoformat()
                    }
                )
            )
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("running", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            self.assertEqual(snapshot["gce_target"], 3)

    def test_ready_primary_clears_the_wake_and_is_never_described(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            settings.state_file.write_text(
                json.dumps({"primary_wake_started_at": datetime.now(UTC).isoformat()})
            )
            platform = _Platform(runnable=3, primary=_primary(ready=True))
            ec2 = _EC2("running", platform.operations, status_error=True)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(
                snapshot["fallback_reason"], "AWS_PRIMARY_HANDLING_BASE_LOAD"
            )
            state = json.loads(settings.state_file.read_text())
            self.assertIsNone(state["primary_wake_started_at"])

    def test_idle_stopped_primary_stays_asleep(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=0, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_ASLEEP")
            self.assertEqual(snapshot["gce_target"], 0)

    def test_closed_or_unknown_or_draining_primary_is_never_woken(self) -> None:
        for primary in (
            _primary(admission_open=False, screening_concurrency=0),
            _primary(status="draining"),
            None,
        ):
            with self.subTest(primary=primary), TemporaryDirectory() as tmp:
                settings = _settings(Path(tmp))
                platform = _Platform(runnable=3, primary=primary)
                ec2 = _EC2("stopped", platform.operations)

                _run(settings, platform, ec2)

                self.assertEqual(ec2.starts, 0)

    def test_failed_start_reports_and_overflows_to_gce(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations, start_error=True)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            self.assertEqual(snapshot["gce_target"], 3)
            events = [event["event_type"] for event in platform.renewed[0]["events"]]
            self.assertIn("primary_wake_failed", events)
            self.assertNotIn("403 secret", json.dumps(platform.renewed))

    def test_failed_describe_reports_and_overflows_to_gce(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations, status_error=True)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            self.assertEqual(ec2.starts, 0)

    def test_no_unfenced_start_before_the_lease_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary(), fence_error=True)
            ec2 = _EC2("stopped", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_WAKING")

    def test_stopping_primary_with_work_waits_then_bounds_its_grace(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopping", platform.operations)

            first = _run(settings, platform, ec2)
            self.assertEqual(first["fallback_reason"], "AWS_PRIMARY_WAKING")
            self.assertEqual(ec2.starts, 0)

            ec2.state = "stopped"
            _run(settings, _Platform(runnable=3, primary=_primary()), ec2)
            self.assertEqual(ec2.starts, 1)

    def test_dry_run_never_starts_the_primary(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(_settings(Path(tmp)), dry_run=True)
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)

            _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)

    def test_hetzner_primary_never_consults_ec2(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(
                runnable=3,
                primary=_primary(provider="hetzner"),
                routing=_hetzner_routing(),
            )
            ec2 = _EC2("stopped", platform.operations, status_error=True)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "HETZNER_PRIMARY_UNAVAILABLE")

    def test_unconfigured_controller_stays_gce_only(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                _settings(Path(tmp)),
                aws_primary_instance_id=None,
                aws_primary_role_arn=None,
            )
            platform = _Platform(runnable=3, primary=_primary())
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch(
                    "screener_capacity.controller.GCEFleet",
                    return_value=_GCE(platform.operations),
                ),
                patch("screener_capacity.controller.EC2Primary") as ec2_class,
            ):
                snapshot = reconcile(settings)

            ec2_class.assert_not_called()
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")


class WakeReviewRegressionTests(unittest.TestCase):
    def _state(self, settings: Settings) -> dict[str, Any]:
        if not settings.state_file.exists():
            return {}
        loaded: dict[str, Any] = json.loads(settings.state_file.read_text())
        return loaded

    def test_dry_runs_never_write_wake_state(self) -> None:
        recorded = datetime.now(UTC).isoformat()
        for primary, state in (
            (_primary(ready=True), "running"),
            (_primary(), "stopping"),
            (_primary(), "stopped"),
        ):
            with self.subTest(state=state), TemporaryDirectory() as tmp:
                settings = replace(_settings(Path(tmp)), dry_run=True)
                settings.state_file.write_text(
                    json.dumps({"primary_wake_started_at": recorded})
                )
                before = settings.state_file.read_text()
                platform = _Platform(runnable=3, primary=primary)

                _run(settings, platform, _EC2(state, platform.operations))

                self.assertEqual(settings.state_file.read_text(), before)

    def test_a_repeated_failure_is_reported_once(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            ec2 = _EC2("stopped", [], status_error=True)
            reported = []
            for _ in range(3):
                platform = _Platform(runnable=3, primary=_primary())
                _run(settings, platform, ec2)
                reported.append(
                    [
                        event["event_type"]
                        for event in platform.renewed[0]["events"]
                        if event["event_type"] == "primary_wake_failed"
                    ]
                )

            self.assertEqual(reported, [["primary_wake_failed"], [], []])
            # Recovery clears it, so a later failure is reported again.
            ec2.status_error = False
            _run(settings, _Platform(runnable=3, primary=_primary()), ec2)
            self.assertIsNone(self._state(settings)["primary_wake_failure"])

    def test_start_failures_are_reported_once(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            ec2 = _EC2("stopped", [], start_error=True)
            first = _Platform(runnable=3, primary=_primary())
            second = _Platform(runnable=3, primary=_primary())

            _run(settings, first, ec2)
            _run(settings, second, ec2)

            def failures(platform: _Platform) -> int:
                return sum(
                    event["event_type"] == "primary_wake_failed"
                    for event in platform.renewed[0]["events"]
                )

            self.assertEqual((failures(first), failures(second)), (1, 0))

    def test_a_primary_on_another_resource_is_never_started(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            platform = _Platform(
                runnable=3, primary=_primary(provider_resource_id="i-0fffffffffffffff0")
            )
            ec2 = _EC2("stopped", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            details = [event["detail"] for event in platform.renewed[0]["events"]]
            self.assertTrue(any("not configured" in detail for detail in details))

    def test_a_wake_gets_one_grace_period_across_restarts(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            first_start = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
            settings.state_file.write_text(
                json.dumps({"primary_wake_started_at": first_start})
            )
            # It stopped again before it ever reported ready.
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)

            snapshot = _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 0)
            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            self.assertEqual(
                self._state(settings)["primary_wake_started_at"], first_start
            )

    def test_a_restart_within_grace_keeps_the_first_wake_time(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            first_start = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
            settings.state_file.write_text(
                json.dumps({"primary_wake_started_at": first_start})
            )
            platform = _Platform(runnable=3, primary=_primary())
            ec2 = _EC2("stopped", platform.operations)

            _run(settings, platform, ec2)

            self.assertEqual(ec2.starts, 1)
            self.assertEqual(
                self._state(settings)["primary_wake_started_at"], first_start
            )

    def test_invalid_aws_settings_never_break_a_hetzner_route(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(_settings(Path(tmp)), aws_primary_instance_id="web-1")
            platform = _Platform(
                runnable=3,
                primary=_primary(provider="hetzner", provider_resource_id="robot-1"),
                routing=_hetzner_routing(),
            )
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch(
                    "screener_capacity.controller.GCEFleet",
                    return_value=_GCE(platform.operations),
                ),
            ):
                snapshot = reconcile(settings)

            self.assertEqual(snapshot["fallback_reason"], "HETZNER_PRIMARY_UNAVAILABLE")

    def test_invalid_aws_settings_on_the_aws_route_fail_over_to_gce(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(_settings(Path(tmp)), aws_region="nowhere")
            platform = _Platform(runnable=3, primary=_primary())
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch(
                    "screener_capacity.controller.GCEFleet",
                    return_value=_GCE(platform.operations),
                ),
            ):
                snapshot = reconcile(settings)

            self.assertEqual(snapshot["fallback_reason"], "AWS_PRIMARY_UNAVAILABLE")
            details = [event["detail"] for event in platform.renewed[0]["events"]]
            self.assertTrue(any(d.startswith("configuration:") for d in details))

    def test_one_ec2_client_serves_every_pass_of_a_process(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            ec2 = _EC2("running", [], launched_at=datetime.now(UTC))
            aws_clients: dict[str, Any] = {}
            with patch(
                "screener_capacity.controller.EC2Primary", return_value=ec2
            ) as ec2_class:
                for _ in range(3):
                    platform = _Platform(runnable=3, primary=_primary())
                    with (
                        patch(
                            "screener_capacity.controller.PlatformControl",
                            return_value=platform,
                        ),
                        patch(
                            "screener_capacity.controller.GCEFleet",
                            return_value=_GCE(platform.operations),
                        ),
                    ):
                        reconcile(settings, aws_clients=aws_clients)

            self.assertEqual(ec2_class.call_count, 1)


class WakeFlagTests(unittest.TestCase):
    def test_wake_flags_reach_settings(self) -> None:
        args = build_parser().parse_args(
            [
                "--platform-url",
                "https://platform.invalid",
                "--platform-token-file",
                "/tmp/token",
                "--gce-project",
                "p",
                "--gce-region",
                "r",
                "--gce-mig",
                "m",
                "--aws-primary-instance-id",
                _INSTANCE,
                "--aws-primary-role-arn",
                "arn:aws:iam::339713011188:role/ditto-screener-wake",
                "--primary-wake-grace-seconds",
                "300",
            ]
        )
        with patch("screener_capacity.controller._source_sha", return_value="a" * 40):
            settings = controller_settings(args)

        self.assertEqual(settings.aws_primary_instance_id, _INSTANCE)
        self.assertEqual(settings.aws_region, "us-east-1")
        self.assertEqual(settings.aws_web_identity_audience, "ditto-screener-capacity")
        self.assertEqual(settings.primary_wake_grace_seconds, 300)
