from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

from screener_capacity.controller import (
    ControllerError,
    Demand,
    GCEFleet,
    GCPBootstrapTokenMinter,
    NodeInventory,
    OverflowPolicy,
    ProviderCounts,
    ProviderRouting,
    Settings,
    build_parser,
    desired_slots,
    gce_capacity_target,
    gce_overflow_target,
    reconcile,
)
from screener_capacity.controller import _settings as controller_settings


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
    )


class _Platform:
    def __init__(
        self,
        demand: Demand,
        nodes: dict[str, dict[str, object]] | None = None,
        screening_priority: tuple[Literal["hetzner", "targon", "gcp"], ...] = (
            "hetzner",
            "gcp",
        ),
        build_priority: tuple[Literal["hetzner", "targon", "gcp"], ...] = (
            "hetzner",
            "gcp",
        ),
        primary_node_id: str | None = None,
    ) -> None:
        self._demand = demand
        self._nodes = nodes or {}
        self.renewed: list[dict[str, object]] = []
        self.fences = 0
        self._screening_priority = screening_priority
        self._build_priority = build_priority
        self._primary_node_id = primary_node_id

    def demand(self, **_kwargs: object) -> Demand:
        return self._demand

    def provider_routing(self) -> ProviderRouting:
        return ProviderRouting(
            revision=0,
            runtime_provider_priority=self._screening_priority,
            source_review_provider_priority=self._screening_priority,
            build_provider_priority=self._build_priority,
            overflow=OverflowPolicy(False, self._primary_node_id, 3, 12, 6),
        )

    def renew(self, snapshot: dict[str, object]) -> dict[str, object]:
        self.renewed.append(snapshot)
        return snapshot

    def fence(self, **_kwargs: object) -> None:
        self.fences += 1

    def node_states(self) -> dict[str, dict[str, object]]:
        return self._nodes


class _GCE:
    def __init__(self, target: int = 0, operations: list[str] | None = None) -> None:
        self._target = target
        self.resized: list[int] = []
        self.deleted_instances: list[list[str]] = []
        self.watchdogs: list[bool] = []
        self.operations = operations
        self.instances: set[str] = set()

    def target(self) -> int:
        return self._target

    def counts(self) -> ProviderCounts:
        return ProviderCounts(healthy=self._target)

    def ensure_watchdog(self) -> None:
        self.watchdogs.append(True)

    def resize(self, target: int) -> None:
        self.resized.append(target)
        if self.operations is not None:
            self.operations.append(f"gce:{target}")
        self._target = target

    def running_instances(self) -> set[str]:
        return self.instances

    def delete_instances(self, names: list[str]) -> None:
        self.deleted_instances.append(names)
        if self.operations is not None:
            self.operations.append(f"delete:{','.join(names)}")
        self._target -= len(names)


def _targon_routing() -> ProviderRouting:
    """A stale revision still naming the retired Targon provider first."""
    return ProviderRouting(
        revision=0,
        runtime_provider_priority=("targon", "gcp"),
        source_review_provider_priority=("targon", "gcp"),
        build_provider_priority=("targon", "gcp"),
        overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
    )


def _overflow_routing(*, enabled: bool = True) -> ProviderRouting:
    return ProviderRouting(
        revision=1,
        runtime_provider_priority=("hetzner", "gcp"),
        source_review_provider_priority=("hetzner", "gcp"),
        build_provider_priority=("hetzner", "gcp"),
        overflow=OverflowPolicy(enabled, "subnet-screener-1", 3, 12, 6),
    )


class CapacityDecisionTests(unittest.TestCase):
    def test_healthy_hetzner_handles_normal_backlog_without_gce(self) -> None:
        routing = ProviderRouting(
            revision=1,
            runtime_provider_priority=("hetzner", "gcp"),
            source_review_provider_priority=("hetzner", "gcp"),
            build_provider_priority=("hetzner", "gcp"),
            overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
        )

        target, reason = gce_overflow_target(
            demand=Demand(runnable=24, active=8, desired=6),
            routing=routing,
            primary_node={
                "status": "active",
                "ready": True,
                "screening_concurrency": 8,
            },
            jobs_per_slot=6,
            global_cap=6,
        )

        self.assertEqual(target, 0)
        self.assertEqual(reason, "HETZNER_PRIMARY_HANDLING_BASE_LOAD")

    def test_hetzner_backlog_multiple_starts_only_residual_gce(self) -> None:
        routing = ProviderRouting(
            revision=1,
            runtime_provider_priority=("hetzner", "gcp"),
            source_review_provider_priority=("hetzner", "gcp"),
            build_provider_priority=("hetzner", "gcp"),
            overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
        )

        target, reason = gce_overflow_target(
            demand=Demand(runnable=37, active=8, desired=6),
            routing=routing,
            primary_node={
                "status": "active",
                "ready": True,
                "screening_concurrency": 8,
            },
            jobs_per_slot=6,
            global_cap=6,
        )

        self.assertEqual(target, 3)
        self.assertEqual(reason, "HETZNER_BACKLOG_OVERFLOW")

    def test_hetzner_outage_activates_gce_for_waiting_work(self) -> None:
        routing = ProviderRouting(
            revision=1,
            runtime_provider_priority=("hetzner", "gcp"),
            source_review_provider_priority=("hetzner", "gcp"),
            build_provider_priority=("hetzner", "gcp"),
            overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
        )

        target, reason = gce_overflow_target(
            demand=Demand(runnable=7, active=0, desired=2),
            routing=routing,
            primary_node={
                "status": "active",
                "ready": False,
                "admission_open": True,
                "screening_concurrency": 4,
            },
            jobs_per_slot=6,
            global_cap=6,
        )

        self.assertEqual(target, 2)
        self.assertEqual(reason, "HETZNER_PRIMARY_UNAVAILABLE")

    def test_admission_closed_primary_is_a_global_full_stop(self) -> None:
        for enabled in (True, False):
            with self.subTest(gce_overflow_enabled=enabled):
                target, reason = gce_overflow_target(
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(enabled=enabled),
                    primary_node={
                        "status": "active",
                        "ready": True,
                        "admission_open": False,
                        "screening_concurrency": 0,
                    },
                    jobs_per_slot=6,
                    global_cap=6,
                )

                self.assertEqual(target, 0)
                self.assertEqual(reason, "HETZNER_PRIMARY_ADMISSION_CLOSED")

    def test_one_slot_activation_restores_backlog_threshold(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=24, active=0, desired=4),
            routing=_overflow_routing(),
            primary_node={
                "status": "active",
                "ready": True,
                "admission_open": True,
                "screening_concurrency": 1,
            },
            jobs_per_slot=6,
            global_cap=6,
        )

        # threshold = max(min_backlog=12, 1 * 3); (24 - 12) / 6 slots.
        self.assertEqual(target, 2)
        self.assertEqual(reason, "HETZNER_BACKLOG_OVERFLOW")

    def test_known_closure_survives_a_host_health_failure(self) -> None:
        # Recovery: the operator's zero admission must persist when the primary
        # stops heartbeating; only a one-slot activation can reopen screening.
        for primary in (
            {
                "status": "active",
                "ready": False,
                "admission_open": False,
                "screening_concurrency": 0,
            },
            {"status": "offline", "ready": False, "screening_concurrency": 0},
        ):
            with self.subTest(primary=primary):
                target, reason = gce_overflow_target(
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(),
                    primary_node=primary,
                    jobs_per_slot=6,
                    global_cap=6,
                )

                self.assertEqual(target, 0)
                self.assertEqual(reason, "HETZNER_PRIMARY_ADMISSION_CLOSED")

    def test_unready_open_primary_is_a_host_failure(self) -> None:
        target, reason = gce_overflow_target(
            demand=Demand(runnable=24, active=0, desired=4),
            routing=_overflow_routing(),
            primary_node={
                "status": "active",
                "ready": False,
                "admission_open": True,
                "screening_concurrency": 4,
            },
            jobs_per_slot=6,
            global_cap=6,
        )

        self.assertEqual(target, 4)
        self.assertEqual(reason, "HETZNER_PRIMARY_UNAVAILABLE")

    def test_unknown_primary_fails_closed(self) -> None:
        # An omitted primary row (or a failed inventory read) and a row without
        # its admission setting cannot rule out the operator stop.
        for primary in (None, {"status": "active", "ready": False}):
            with self.subTest(primary=primary):
                target, reason = gce_overflow_target(
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(),
                    primary_node=primary,
                    jobs_per_slot=6,
                    global_cap=6,
                )

                self.assertEqual(target, 0)
                self.assertEqual(reason, "HETZNER_PRIMARY_UNKNOWN")

    def test_reconcile_never_overflows_without_the_primary_inventory(self) -> None:
        def failed_read() -> dict[str, Any]:
            raise ControllerError("Platform node readiness response is invalid")

        for label, node_states in (
            ("inventory-read-failure", failed_read),
            ("omitted-primary-row", lambda: {"other-node": {"status": "active"}}),
        ):
            with self.subTest(label), TemporaryDirectory() as directory:
                platform = SimpleNamespace(
                    demand=lambda **_kwargs: Demand(runnable=24, active=0, desired=4),
                    provider_routing=_overflow_routing,
                    node_states=node_states,
                    renew=lambda snapshot: snapshot,
                    fence=lambda **_kwargs: None,
                )
                gce = _GCE()
                with (
                    patch(
                        "screener_capacity.controller.PlatformControl",
                        return_value=platform,
                    ),
                    patch("screener_capacity.controller.GCEFleet", return_value=gce),
                ):
                    snapshot = reconcile(
                        replace(
                            _settings(Path(directory)),
                            inventory_failure_hold_passes=0,
                        )
                    )

                self.assertEqual(snapshot["gce_target"], 0)
                self.assertEqual(snapshot["fallback_reason"], "HETZNER_PRIMARY_UNKNOWN")
                self.assertEqual(gce.resized, [])

    def test_admission_closed_falls_back_to_concurrency_when_field_missing(
        self,
    ) -> None:
        for concurrency, expected in (
            (0, (0, "HETZNER_PRIMARY_ADMISSION_CLOSED")),
            (1, (2, "HETZNER_BACKLOG_OVERFLOW")),
        ):
            with self.subTest(screening_concurrency=concurrency):
                result = gce_overflow_target(
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(),
                    primary_node={
                        "status": "active",
                        "ready": True,
                        "screening_concurrency": concurrency,
                    },
                    jobs_per_slot=6,
                    global_cap=6,
                )

                self.assertEqual(result, expected)

    def test_explicit_gcp_routing_respects_primary_admission(self) -> None:
        routing = ProviderRouting(
            revision=1,
            runtime_provider_priority=("gcp", "hetzner"),
            source_review_provider_priority=("gcp", "hetzner"),
            build_provider_priority=("gcp", "hetzner"),
            overflow=OverflowPolicy(False, "subnet-screener-1", 3, 12, 6),
        )
        for primary, expected in (
            (
                {
                    "status": "active",
                    "ready": True,
                    "admission_open": False,
                    "screening_concurrency": 0,
                },
                (0, "HETZNER_PRIMARY_ADMISSION_CLOSED"),
            ),
            (None, (0, "HETZNER_PRIMARY_UNKNOWN")),
            (
                {
                    "status": "offline",
                    "ready": False,
                    "admission_open": True,
                    "screening_concurrency": 1,
                },
                (4, "GCP_SCREENERS_PRIORITIZED_BY_POLICY"),
            ),
        ):
            with self.subTest(primary=primary):
                self.assertEqual(
                    gce_overflow_target(
                        demand=Demand(runnable=24, active=0, desired=4),
                        routing=routing,
                        primary_node=primary,
                        jobs_per_slot=6,
                        global_cap=6,
                    ),
                    expected,
                )

    def test_reconcile_records_each_fallback_reason_transition_once(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            primary: dict[str, object] = {
                "status": "active",
                "ready": True,
                "admission_open": True,
                "screening_concurrency": 4,
            }
            renewed: list[dict[str, Any]] = []

            def renew(snapshot: dict[str, Any]) -> dict[str, Any]:
                renewed.append(snapshot)
                return snapshot

            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=2, active=0, desired=1),
                provider_routing=_overflow_routing,
                node_states=lambda: {"subnet-screener-1": primary},
                renew=renew,
                fence=lambda **_kwargs: None,
            )
            gce = _GCE()

            def reason_events() -> list[dict[str, Any]]:
                return [
                    event
                    for event in renewed[0]["events"]
                    if event["event_type"] == "fallback_reason_changed"
                ]

            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                reconcile(settings)
                self.assertEqual(reason_events(), [])

                primary.update(admission_open=False, screening_concurrency=0)
                renewed.clear()
                snapshot = reconcile(settings)
                self.assertEqual(
                    snapshot["fallback_reason"], "HETZNER_PRIMARY_ADMISSION_CLOSED"
                )
                self.assertEqual(snapshot["gce_target"], 0)
                self.assertEqual(gce.resized, [])
                self.assertEqual(
                    reason_events(),
                    [
                        {
                            "event_type": "fallback_reason_changed",
                            "provider": "hetzner",
                            "detail": (
                                "HETZNER_PRIMARY_HANDLING_BASE_LOAD -> "
                                "HETZNER_PRIMARY_ADMISSION_CLOSED"
                            ),
                        }
                    ],
                )

                renewed.clear()
                reconcile(settings)
                self.assertEqual(reason_events(), [])
                self.assertEqual(gce.resized, [])

                # The deliberate one-slot activation reopens the primary.
                primary.update(admission_open=True, screening_concurrency=1)
                renewed.clear()
                reconcile(settings)
                self.assertEqual(
                    [event["detail"] for event in reason_events()],
                    [
                        "HETZNER_PRIMARY_ADMISSION_CLOSED -> "
                        "HETZNER_PRIMARY_HANDLING_BASE_LOAD"
                    ],
                )

    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_resize_pauses_and_restores_watchdog_at_zero(self, run: object) -> None:
        run.return_value = SimpleNamespace(stdout="")  # type: ignore[attr-defined]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        fleet.resize(0)

        commands = [call.args[0] for call in run.call_args_list]  # type: ignore[attr-defined]
        self.assertIn("--mode", commands[0])
        self.assertIn("off", commands[0])
        self.assertIn("resize", commands[1])
        self.assertIn("--size", commands[1])
        self.assertIn("0", commands[1])
        self.assertIn("--mode", commands[2])
        self.assertIn("only-scale-out", commands[2])

    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_resize_restores_watchdog_after_resize_failure(
        self, run: object
    ) -> None:
        from subprocess import CalledProcessError

        run.side_effect = [
            SimpleNamespace(stdout=""),
            CalledProcessError(1, ["gcloud", "resize"]),
            SimpleNamespace(stdout=""),
        ]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        with self.assertRaisesRegex(ControllerError, "managed-group operation"):
            fleet.resize(0)

        restore = run.call_args_list[-1].args[0]  # type: ignore[attr-defined]
        self.assertIn("only-scale-out", restore)

    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_resize_fails_closed_when_watchdog_restore_fails(
        self, run: object
    ) -> None:
        from subprocess import CalledProcessError

        run.side_effect = [
            SimpleNamespace(stdout=""),
            SimpleNamespace(stdout=""),
            CalledProcessError(1, ["gcloud", "update-autoscaling"]),
        ]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        with self.assertRaisesRegex(ControllerError, "watchdog restore failed"):
            fleet.resize(0)

        self.assertEqual(run.call_count, 3)  # type: ignore[attr-defined]

    @patch("screener_capacity.controller.subprocess.run")
    def test_delete_instances_restores_watchdog_on_failure(self, run: object) -> None:
        from subprocess import CalledProcessError

        run.side_effect = [
            SimpleNamespace(stdout=""),
            CalledProcessError(1, ["gcloud", "delete-instances"]),
            SimpleNamespace(stdout=""),
        ]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        with self.assertRaisesRegex(ControllerError, "managed-group operation"):
            fleet.delete_instances(["vm-a", "vm-b"])

        commands = [call.args[0] for call in run.call_args_list]  # type: ignore[attr-defined]
        self.assertIn("off", commands[0])
        self.assertIn("delete-instances", commands[1])
        self.assertIn("--instances=vm-a,vm-b", commands[1])
        self.assertIn("only-scale-out", commands[2])

    @patch("screener_capacity.controller.subprocess.run")
    def test_running_instances_excludes_changing_members(self, run: object) -> None:
        run.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            stdout="""[
              {"instance": "https://compute/zones/z/instances/vm-idle",
               "instanceStatus": "RUNNING", "currentAction": "NONE"},
              {"instance": "https://compute/zones/z/instances/vm-going",
               "instanceStatus": "RUNNING", "currentAction": "DELETING"},
              {"instance": "https://compute/zones/z/instances/vm-booting",
               "instanceStatus": "STAGING", "currentAction": "CREATING"}
            ]"""
        )
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        self.assertEqual(fleet.running_instances(), {"vm-idle"})

    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_watchdog_recovery_is_idempotent(self, run: object) -> None:
        run.side_effect = [SimpleNamespace(stdout="OFF\n"), SimpleNamespace(stdout="")]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        fleet.ensure_watchdog()

        self.assertEqual(run.call_count, 2)  # type: ignore[attr-defined]
        self.assertIn("only-scale-out", run.call_args_list[-1].args[0])  # type: ignore[attr-defined]

    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_watchdog_ready_requires_no_mutation(self, run: object) -> None:
        run.return_value = SimpleNamespace(stdout="ONLY_SCALE_OUT\n")  # type: ignore[attr-defined]
        fleet = GCEFleet(project="test", region="region", mig="fleet")

        fleet.ensure_watchdog()

        self.assertEqual(run.call_count, 1)  # type: ignore[attr-defined]

    def test_reconcile_reports_watchdog_recovery_failure(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(Demand(runnable=0, active=0, desired=0))
            gce = _GCE()

            def fail_watchdog() -> None:
                raise ControllerError("test watchdog failure")

            gce.ensure_watchdog = fail_watchdog  # type: ignore[method-assign]
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
                self.assertRaisesRegex(ControllerError, "watchdog failure"),
            ):
                reconcile(settings)

            self.assertFalse(platform.renewed[-1]["provider_ready"])
            self.assertEqual(
                platform.renewed[-1]["last_provider_error_code"],
                "GCE_WATCHDOG_RESTORE_FAILED",
            )

    def test_hetzner_base_load_keeps_watchdog_only_scale_out(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            routing = ProviderRouting(
                revision=1,
                runtime_provider_priority=("hetzner", "gcp"),
                source_review_provider_priority=("hetzner", "gcp"),
                build_provider_priority=("hetzner", "gcp"),
                overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
            )
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=10, active=0, desired=5),
                provider_routing=lambda: routing,
                node_states=lambda: {
                    "subnet-screener-1": {
                        "status": "active",
                        "ready": True,
                        "screening_concurrency": 2,
                    }
                },
                renew=lambda snapshot: snapshot,
                fence=lambda **_kwargs: None,
            )
            gce = _GCE()

            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)

            self.assertEqual(snapshot["gce_target"], 0)
            self.assertEqual(
                snapshot["fallback_reason"], "HETZNER_PRIMARY_HANDLING_BASE_LOAD"
            )
            self.assertEqual(gce.watchdogs, [True])
            self.assertEqual(gce.resized, [])

    def test_scale_down_to_zero_defers_even_with_idle_members(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.state_file.write_text(json.dumps({"provider_ready": True}))
            routing = ProviderRouting(
                revision=1,
                runtime_provider_priority=("hetzner", "gcp"),
                source_review_provider_priority=("hetzner", "gcp"),
                build_provider_priority=("hetzner", "gcp"),
                overflow=OverflowPolicy(True, "subnet-screener-1", 3, 12, 6),
            )
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=10, active=0, desired=5),
                provider_routing=lambda: routing,
                node_states=lambda: {
                    "subnet-screener-1": {
                        "status": "active",
                        "ready": True,
                        "screening_concurrency": 2,
                    }
                },
                node_inventory=lambda: self._inventory(
                    self._gcp_row("vm-a", seen=1),
                    self._gcp_row("vm-b", seen=2),
                ),
                renew=lambda snapshot: snapshot,
                fence=lambda **_kwargs: None,
            )
            gce = _GCE(target=2)
            gce.instances = {"vm-a", "vm-b"}

            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                reconcile(settings)

            self.assertEqual(gce.resized, [])
            self.assertEqual(gce.target(), 2)

    def test_zero_idle_capacity_is_valid(self) -> None:
        self.assertEqual(desired_slots(runnable=0, active=0, jobs_per_slot=6, cap=6), 0)

    def test_active_leases_always_have_capacity(self) -> None:
        self.assertEqual(desired_slots(runnable=7, active=2, jobs_per_slot=6, cap=6), 4)

    def test_global_cap_is_authoritative(self) -> None:
        self.assertEqual(
            desired_slots(runnable=200, active=3, jobs_per_slot=1, cap=6), 6
        )

    def test_gce_worker_capacity_takes_all_demand(self) -> None:
        self.assertEqual(gce_capacity_target(demand=5), 5)

    @patch("screener_capacity.controller.subprocess.run")
    def test_worker_secret_bootstrap_uses_delegated_short_lived_token(
        self, run: object
    ) -> None:
        run.return_value = SimpleNamespace(stdout="x" * 120)  # type: ignore[attr-defined]
        token = GCPBootstrapTokenMinter(
            target="node@example.iam.gserviceaccount.com",
            delegate="controller@example.iam.gserviceaccount.com",
        ).mint()
        self.assertEqual(token, "x" * 120)
        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertIn("--lifetime=1800", command)
        self.assertIn(
            "--impersonate-service-account=controller@example.iam.gserviceaccount.com,"
            "node@example.iam.gserviceaccount.com",
            command,
        )

    def test_targon_first_lanes_scale_out_but_defer_unproven_scale_in(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=5, active=0, desired=3),
                provider_routing=_targon_routing,
                node_states=lambda: {
                    "subnet-screener-1": {
                        "status": "active",
                        "ready": True,
                        "admission_open": True,
                        "screening_concurrency": 4,
                    }
                },
                node_inventory=lambda: NodeInventory({}, 0),
                renew=lambda snapshot: snapshot,
                fence=lambda **_kwargs: None,
            )
            resized: list[int] = []
            gce = SimpleNamespace(
                target=lambda: 0,
                counts=lambda: ProviderCounts(),
                ensure_watchdog=lambda **_kwargs: None,
                resize=lambda target, **_kwargs: resized.append(target),
            )
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)
            self.assertEqual(snapshot["gce_target"], 3)
            self.assertEqual(resized, [3])

            platform.demand = lambda **_kwargs: Demand(runnable=0, active=0, desired=0)
            gce.target = lambda: 3
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)
            self.assertEqual(snapshot["gce_target"], 0)
            self.assertEqual(resized, [3])
            self.assertEqual(snapshot["fallback_reason"], "GCE_SCALE_IN_DEFERRED")

    def test_targon_first_lanes_never_bypass_the_primary_stop(self) -> None:
        # A stale routing revision that still names the retired provider must not
        # reopen screening through GCE while the operator's stop holds or the
        # primary's admission is unknown.
        for primary, reason in (
            (
                {
                    "status": "active",
                    "ready": True,
                    "admission_open": False,
                    "screening_concurrency": 0,
                },
                "HETZNER_PRIMARY_ADMISSION_CLOSED",
            ),
            (
                {"status": "offline", "ready": False, "screening_concurrency": 0},
                "HETZNER_PRIMARY_ADMISSION_CLOSED",
            ),
            (None, "HETZNER_PRIMARY_UNKNOWN"),
        ):
            with self.subTest(primary=primary):
                target, actual_reason = gce_overflow_target(
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_targon_routing(),
                    primary_node=primary,
                    jobs_per_slot=6,
                    global_cap=6,
                )

                self.assertEqual(target, 0)
                self.assertEqual(actual_reason, reason)

    def test_missing_node_inventory_blocks_gce_scale_in_with_live_work(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=0, active=1, desired=1),
                provider_routing=lambda: ProviderRouting(
                    revision=0,
                    runtime_provider_priority=("targon", "gcp"),
                    source_review_provider_priority=("targon", "gcp"),
                    build_provider_priority=("targon", "gcp"),
                ),
                renew=lambda snapshot: snapshot,
                fence=lambda **_kwargs: None,
            )
            gce = _GCE(target=2)

            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)

            self.assertEqual(snapshot["gce_target"], 2)
            self.assertEqual(gce.resized, [])

    @staticmethod
    def _gcp_row(
        name: str,
        *,
        seen: int,
        busy: bool = False,
        lease: bool = False,
        ready: bool = True,
    ) -> dict[str, object]:
        return {
            "node_id": name,
            "provider_resource_id": name,
            "provider": "gcp",
            "status": "active",
            "ready": ready,
            "active_lease": lease,
            "instance_busy": busy,
            "heartbeat_seen_at": f"2026-09-29T00:00:{seen:02d}Z",
        }

    @staticmethod
    def _inventory(*rows: dict[str, object], running: int | None = 0) -> NodeInventory:
        states: dict[str, dict[str, Any]] = {
            "subnet-screener-1": {
                "status": "active",
                "ready": True,
                "admission_open": True,
                "screening_concurrency": 4,
            }
        }
        states.update({str(row["node_id"]): row for row in rows})
        return NodeInventory(states, running)

    def _scale_in_pass(
        self,
        gce: _GCE,
        *,
        runnable: int,
        first: NodeInventory,
        second: NodeInventory | ControllerError,
        operations: list[str],
        routing: ProviderRouting | None = None,
        desired: int = 4,
        provider_ready: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run one pass whose post-renew inventory read returns ``second``."""
        renewed: list[dict[str, Any]] = []

        def node_inventory() -> NodeInventory:
            operations.append("inventory")
            if isinstance(second, ControllerError):
                raise second
            return second

        def renew(snapshot: dict[str, Any]) -> dict[str, Any]:
            operations.append("renew")
            renewed.append(snapshot)
            return snapshot

        platform = SimpleNamespace(
            demand=lambda **_kwargs: Demand(
                runnable=runnable, active=0, desired=desired
            ),
            provider_routing=(lambda: routing) if routing else _overflow_routing,
            node_states=lambda: first.states,
            node_inventory=node_inventory,
            renew=renew,
            fence=lambda **_kwargs: operations.append("fence"),
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "screener_capacity.controller.PlatformControl", return_value=platform
            ),
            patch("screener_capacity.controller.GCEFleet", return_value=gce),
        ):
            settings = _settings(Path(directory))
            settings.state_file.write_text(
                json.dumps({"provider_ready": provider_ready})
            )
            snapshot = reconcile(settings)
        return snapshot, renewed

    def test_scale_in_rereads_node_states_after_renew_and_defers_on_new_gce_lease(
        self,
    ) -> None:
        operations: list[str] = []
        gce = _GCE(target=2, operations=operations)
        snapshot, renewed = self._scale_in_pass(
            gce,
            runnable=2,
            first=self._inventory(
                self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2)
            ),
            # vm-a claimed between the first read and the fenced renew.
            second=self._inventory(
                self._gcp_row("vm-a", seen=3, busy=True, lease=True),
                self._gcp_row("vm-b", seen=2, lease=True),
                running=1,
            ),
            operations=operations,
        )

        self.assertEqual(operations, ["renew", "fence", "fence", "inventory", "renew"])
        self.assertEqual(gce.resized, [])
        self.assertEqual(gce.deleted_instances, [])
        # The fenced renew stopped new claims before the re-read, and the
        # deferral must not reopen them.
        self.assertEqual(renewed[0]["gce_target"], 0)
        self.assertEqual(snapshot["gce_target"], 0)
        self.assertEqual(snapshot["fallback_reason"], "GCE_SCALE_IN_DEFERRED")
        self.assertTrue(snapshot["provider_ready"])
        self.assertEqual(
            snapshot["events"],
            [
                {
                    "event_type": "gce_scale_in_deferred",
                    "provider": "gcp",
                    "detail": "GCE target 2 -> 0 deferred: gce_active_lease",
                }
            ],
        )

    def test_scale_in_defers_when_post_renew_inventory_unavailable(self) -> None:
        operations: list[str] = []
        gce = _GCE(target=2, operations=operations)
        snapshot, _ = self._scale_in_pass(
            gce,
            runnable=2,
            first=self._inventory(self._gcp_row("vm-a", seen=1)),
            second=ControllerError("Platform GET failed with HTTP 502"),
            operations=operations,
        )

        self.assertEqual(gce.resized, [])
        self.assertEqual(gce.target(), 2)
        self.assertEqual(snapshot["gce_target"], 0)
        self.assertTrue(snapshot["provider_ready"])
        self.assertIsNone(snapshot["last_provider_error_code"])
        self.assertEqual(
            [event["detail"] for event in snapshot["events"]],
            ["GCE target 2 -> 0 deferred: inventory_unavailable"],
        )

    def test_scale_in_to_zero_defers_after_clean_reread_without_durable_fence(
        self,
    ) -> None:
        operations: list[str] = []
        gce = _GCE(target=2, operations=operations)
        gce.instances = {"vm-a", "vm-b"}
        idle = self._inventory(
            self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2)
        )
        snapshot, _ = self._scale_in_pass(
            gce, runnable=2, first=idle, second=idle, operations=operations
        )

        self.assertEqual(operations, ["renew", "fence", "fence", "inventory", "renew"])
        self.assertEqual(gce.resized, [])
        self.assertEqual(gce.target(), 2)
        self.assertEqual(gce.watchdogs, [True])
        self.assertEqual(snapshot["gce_target"], 0)
        self.assertEqual(
            [event["detail"] for event in snapshot["events"]],
            ["GCE target 2 -> 0 deferred: durable_claim_fence_unavailable"],
        )

    def test_scale_in_to_zero_requires_every_managed_instance_to_be_idle(
        self,
    ) -> None:
        cases = (
            (self._inventory(self._gcp_row("vm-a", seen=1)), {"vm-a", "vm-b"}),
            (
                self._inventory(
                    self._gcp_row("vm-a", seen=1),
                    self._gcp_row("vm-b", seen=2, busy=True),
                ),
                {"vm-a", "vm-b"},
            ),
        )
        for inventory, members in cases:
            with self.subTest(inventory=inventory):
                operations: list[str] = []
                gce = _GCE(target=2, operations=operations)
                gce.instances = members
                snapshot, _ = self._scale_in_pass(
                    gce,
                    runnable=2,
                    first=inventory,
                    second=inventory,
                    operations=operations,
                )

                self.assertEqual(gce.resized, [])
                self.assertEqual(gce.target(), 2)
                self.assertEqual(
                    [event["detail"] for event in snapshot["events"]],
                    ["GCE target 2 -> 0 deferred: instance_inventory_incomplete"],
                )

    def test_scale_in_to_zero_defers_on_running_attempt_without_heartbeat(self) -> None:
        for running, reason in (
            (1, "gce_active_lease"),
            (None, "attribution_incomplete"),
        ):
            with self.subTest(running=running):
                operations: list[str] = []
                gce = _GCE(target=2, operations=operations)
                snapshot, _ = self._scale_in_pass(
                    gce,
                    runnable=2,
                    first=self._inventory(),
                    second=self._inventory(running=running),
                    operations=operations,
                )

                self.assertEqual(gce.resized, [])
                self.assertEqual(gce.target(), 2)
                self.assertEqual(snapshot["gce_target"], 0)
                self.assertEqual(
                    [event["detail"] for event in snapshot["events"]],
                    [f"GCE target 2 -> 0 deferred: {reason}"],
                )

    def test_scale_in_to_zero_defers_without_a_legacy_claim_fence(self) -> None:
        operations: list[str] = []
        gce = _GCE(target=2, operations=operations)
        routing = ProviderRouting(
            revision=1,
            runtime_provider_priority=("gcp", "hetzner"),
            source_review_provider_priority=("gcp", "hetzner"),
            build_provider_priority=("gcp", "hetzner"),
        )
        snapshot, _ = self._scale_in_pass(
            gce,
            runnable=0,
            desired=0,
            routing=routing,
            first=self._inventory(),
            second=self._inventory(),
            operations=operations,
        )

        self.assertEqual(gce.resized, [])
        self.assertEqual(snapshot["gce_target"], 0)
        self.assertEqual(
            [event["detail"] for event in snapshot["events"]],
            ["GCE target 2 -> 0 deferred: legacy_claims_not_fenced"],
        )

    def test_scale_in_to_zero_defers_while_controller_is_unready(self) -> None:
        operations: list[str] = []
        gce = _GCE(target=2, operations=operations)
        snapshot, renewed = self._scale_in_pass(
            gce,
            runnable=2,
            first=self._inventory(),
            second=self._inventory(),
            operations=operations,
            provider_ready=False,
        )

        self.assertEqual(gce.resized, [])
        self.assertFalse(renewed[0]["provider_ready"])
        self.assertEqual(snapshot["gce_target"], 0)
        self.assertEqual(
            [event["detail"] for event in snapshot["events"]],
            ["GCE target 2 -> 0 deferred: legacy_claims_not_fenced"],
        )

    def test_partial_scale_in_defers_even_when_instances_are_idle(self) -> None:
        operations: list[str] = []
        gce = _GCE(target=3, operations=operations)
        gce.instances = {"vm-busy", "vm-old", "vm-new", "vm-unready"}
        first = self._inventory(
            self._gcp_row("vm-busy", seen=1),
            self._gcp_row("vm-old", seen=2),
            self._gcp_row("vm-new", seen=5),
        )
        second = self._inventory(
            self._gcp_row("vm-busy", seen=1, busy=True, lease=True),
            self._gcp_row("vm-old", seen=2, lease=True),
            self._gcp_row("vm-new", seen=5, lease=True),
            # Already deleted: its heartbeat is still fresh for minutes.
            self._gcp_row("vm-gone", seen=0, lease=True),
            self._gcp_row("vm-unready", seen=0, lease=True, ready=False),
            running=1,
        )
        # threshold = max(12, 4 * 3); (14 - 12) / 2 jobs per slot -> 1 slot.
        snapshot, _ = self._scale_in_pass(
            gce, runnable=14, first=first, second=second, operations=operations
        )

        self.assertEqual(gce.deleted_instances, [])
        self.assertEqual(gce.resized, [])
        self.assertEqual(snapshot["gce_target"], 1)
        self.assertEqual(
            operations,
            ["renew", "fence", "fence", "inventory", "renew"],
        )
        self.assertEqual(
            [event["detail"] for event in snapshot["events"]],
            ["GCE target 3 -> 1 deferred: per_instance_claim_fence_unavailable"],
        )

    def test_partial_scale_in_defers_on_unattributed_running_attempt(self) -> None:
        idle = (
            self._gcp_row("vm-a", seen=1),
            self._gcp_row("vm-b", seen=2),
            self._gcp_row("vm-c", seen=3),
        )
        for label, second, members in (
            # A worker claimed but has not heartbeated "screening" yet.
            ("attribution_incomplete", self._inventory(*idle, running=1), None),
            # A Platform without the attribution count cannot vouch for idleness.
            ("attribution_incomplete", self._inventory(*idle, running=None), None),
            ("insufficient_idle_instances", self._inventory(*idle), {"vm-a"}),
        ):
            with self.subTest(label=label, second=second):
                operations: list[str] = []
                gce = _GCE(target=3, operations=operations)
                gce.instances = members or {"vm-a", "vm-b", "vm-c"}
                snapshot, _ = self._scale_in_pass(
                    gce,
                    runnable=14,
                    first=self._inventory(*idle),
                    second=second,
                    operations=operations,
                )

                self.assertEqual(gce.deleted_instances, [])
                self.assertEqual(gce.resized, [])
                self.assertEqual(gce.target(), 3)
                self.assertEqual(snapshot["fallback_reason"], "GCE_SCALE_IN_DEFERRED")
                self.assertEqual(
                    [event["detail"] for event in snapshot["events"]],
                    [f"GCE target 3 -> 1 deferred: {label}"],
                )

    def _deferred_scale_in_passes(
        self,
        gce: _GCE,
        inventories: list[NodeInventory],
        *,
        settings: Settings,
        runnable: int = 2,
    ) -> list[list[dict[str, Any]]]:
        """Run one pass per inventory against one persistent state file."""
        per_pass: list[list[dict[str, Any]]] = []
        for inventory in inventories:
            renewed: list[dict[str, Any]] = []
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=runnable, active=0, desired=4),
                provider_routing=_overflow_routing,
                node_states=lambda inventory=inventory: inventory.states,
                node_inventory=lambda inventory=inventory: inventory,
                renew=lambda snapshot, renewed=renewed: (
                    renewed.append(snapshot) or snapshot
                ),
                fence=lambda **_kwargs: None,
            )
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                reconcile(settings)
            per_pass.append(renewed)
        return per_pass

    @staticmethod
    def _event_details(renewed: list[dict[str, Any]]) -> list[str]:
        return [event["detail"] for payload in renewed for event in payload["events"]]

    def test_deferred_scale_in_records_each_transition_once(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.state_file.write_text(json.dumps({"provider_ready": True}))
            gce = _GCE(target=2)
            gce.instances = {"vm-a", "vm-b"}
            idle = self._inventory(
                self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2)
            )
            busy = self._inventory(
                self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2), running=1
            )
            passes = self._deferred_scale_in_passes(
                gce, [idle, idle, idle, busy, busy], settings=settings
            )

            self.assertEqual(gce.resized, [])
            self.assertEqual(gce.deleted_instances, [])
            for renewed in passes:
                # Every pass keeps publishing the lower target and the deferral.
                self.assertEqual(renewed[0]["gce_target"], 0)
                self.assertEqual(renewed[-1]["gce_target"], 0)
                self.assertEqual(
                    renewed[-1]["fallback_reason"], "GCE_SCALE_IN_DEFERRED"
                )
            self.assertEqual(
                [self._event_details(renewed) for renewed in passes],
                [
                    [
                        "GCE target 2 -> 0",
                        "GCE target 2 -> 0 deferred: durable_claim_fence_unavailable",
                    ],
                    [],
                    [],
                    # A new deferral reason is a new event, not a new target.
                    ["GCE target 2 -> 0 deferred: gce_active_lease"],
                    [],
                ],
            )

    def test_deferred_scale_in_records_a_new_target_or_a_resumed_deferral(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.state_file.write_text(json.dumps({"provider_ready": True}))
            gce = _GCE(target=3)
            idle = self._inventory(
                self._gcp_row("vm-a", seen=1),
                self._gcp_row("vm-b", seen=2),
                self._gcp_row("vm-c", seen=3),
            )
            gce.instances = {"vm-a", "vm-b", "vm-c"}
            first = self._deferred_scale_in_passes(gce, [idle, idle], settings=settings)
            self.assertEqual(
                [self._event_details(renewed) for renewed in first],
                [
                    [
                        "GCE target 3 -> 0",
                        "GCE target 3 -> 0 deferred: durable_claim_fence_unavailable",
                    ],
                    [],
                ],
            )

            # An operator drain changes the physical target: a new transition.
            gce._target = 2
            gce.instances = {"vm-a", "vm-b"}
            drained = self._deferred_scale_in_passes(gce, [idle], settings=settings)
            self.assertEqual(
                self._event_details(drained[0]),
                [
                    "GCE target 2 -> 0",
                    "GCE target 2 -> 0 deferred: durable_claim_fence_unavailable",
                ],
            )

            # A pass that no longer scales in ends the deferral, so a later
            # scale-in is recorded again instead of being mistaken for it.
            leased = self._inventory(
                self._gcp_row("vm-a", seen=1, busy=True, lease=True),
                self._gcp_row("vm-b", seen=2),
                running=1,
            )
            held = self._deferred_scale_in_passes(gce, [leased], settings=settings)
            self.assertEqual(self._event_details(held[0]), [])
            self.assertEqual(held[0][-1]["gce_target"], 2)
            resumed = self._deferred_scale_in_passes(gce, [idle], settings=settings)
            self.assertEqual(
                self._event_details(resumed[0]),
                [
                    "GCE target 2 -> 0",
                    "GCE target 2 -> 0 deferred: durable_claim_fence_unavailable",
                ],
            )

    def test_deferred_scale_in_event_retries_after_completed_renew_fails(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.state_file.write_text(json.dumps({"provider_ready": True}))
            gce = _GCE(target=2)
            gce.instances = {"vm-a", "vm-b"}
            idle = self._inventory(
                self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2)
            )
            renewed: list[dict[str, Any]] = []

            def renew(snapshot: dict[str, Any]) -> dict[str, Any]:
                renewed.append(snapshot)
                if len(renewed) == 2:
                    raise ControllerError("completed renew failed")
                return snapshot

            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=2, active=0, desired=4),
                provider_routing=_overflow_routing,
                node_states=lambda: idle.states,
                node_inventory=lambda: idle,
                renew=renew,
                fence=lambda **_kwargs: None,
            )
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                with self.assertRaisesRegex(ControllerError, "completed renew"):
                    reconcile(settings)
                reconcile(settings)
                reconcile(settings)

            # The fenced renew delivered the target change, so only the
            # undelivered deferral is sent on the next pass, then neither again.
            self.assertEqual(
                [
                    [event["detail"] for event in payload["events"]]
                    for payload in renewed
                ],
                [
                    ["GCE target 2 -> 0"],
                    ["GCE target 2 -> 0 deferred: durable_claim_fence_unavailable"],
                    [],
                    ["GCE target 2 -> 0 deferred: durable_claim_fence_unavailable"],
                    [],
                    [],
                ],
            )

    def test_scale_in_after_a_deferral_still_records_the_target_change(
        self,
    ) -> None:
        # Once a claim fence lets a deferred scale-in proceed, the pass that
        # deletes capacity must still record the change it suppressed.
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.state_file.write_text(
                json.dumps(
                    {
                        "provider_ready": True,
                        "gce_scale_in_deferral": {
                            "from": 3,
                            "to": 1,
                            "reason": "per_instance_claim_fence_unavailable",
                        },
                    }
                )
            )
            gce = _GCE(target=3)
            inventory = self._inventory(
                self._gcp_row("vm-a", seen=1),
                self._gcp_row("vm-b", seen=2),
                self._gcp_row("vm-c", seen=3),
            )
            with patch(
                "screener_capacity.controller._plan_gce_scale_in",
                return_value=(["vm-a", "vm-b"], None),
            ):
                # threshold = max(12, 4 * 3); (14 - 12) / 2 jobs per slot -> 1.
                passes = self._deferred_scale_in_passes(
                    gce, [inventory], settings=settings, runnable=14
                )

            self.assertEqual(gce.deleted_instances, [["vm-a", "vm-b"]])
            renewed = passes[0]
            self.assertEqual(renewed[0]["events"], [])
            self.assertEqual(
                renewed[-1]["events"],
                [
                    {
                        "event_type": "gce_target_changed",
                        "provider": "gcp",
                        "detail": "GCE target 3 -> 1",
                    }
                ],
            )
            self.assertIsNone(
                json.loads(settings.state_file.read_text())["gce_scale_in_deferral"]
            )

    def test_corrupt_scale_in_deferral_state_is_ignored(self) -> None:
        for cached in (
            "2->0",
            {"from": 2},
            {"from": True, "to": 0, "reason": "durable_claim_fence_unavailable"},
            {"from": 2, "to": "0", "reason": "durable_claim_fence_unavailable"},
            {"from": 2, "to": 0, "reason": 7},
        ):
            with self.subTest(cached=cached), TemporaryDirectory() as directory:
                settings = _settings(Path(directory))
                settings.state_file.write_text(
                    json.dumps(
                        {"provider_ready": True, "gce_scale_in_deferral": cached}
                    )
                )
                gce = _GCE(target=2)
                gce.instances = {"vm-a", "vm-b"}
                idle = self._inventory(
                    self._gcp_row("vm-a", seen=1), self._gcp_row("vm-b", seen=2)
                )
                passes = self._deferred_scale_in_passes(gce, [idle], settings=settings)

                self.assertEqual(
                    self._event_details(passes[0]),
                    [
                        "GCE target 2 -> 0",
                        "GCE target 2 -> 0 deferred: durable_claim_fence_unavailable",
                    ],
                )

    def test_gcp_first_policy_scales_gce_workers(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(
                Demand(runnable=4, active=0, desired=2),
                nodes={
                    "subnet-screener-1": {
                        "admission_open": True,
                        "screening_concurrency": 1,
                    }
                },
                screening_priority=("gcp", "hetzner"),
                primary_node_id="subnet-screener-1",
            )
            gce = _GCE()
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)
            self.assertEqual(gce.resized, [2])
            self.assertEqual(snapshot["gce_target"], 2)
            self.assertEqual(
                snapshot["fallback_reason"], "GCP_SCREENERS_PRIORITIZED_BY_POLICY"
            )

    def test_gcp_first_policy_cannot_reopen_zero_admission(self) -> None:
        with TemporaryDirectory() as directory:
            platform = _Platform(
                Demand(runnable=4, active=0, desired=2),
                nodes={
                    "subnet-screener-1": {
                        "admission_open": False,
                        "screening_concurrency": 0,
                    }
                },
                screening_priority=("gcp", "hetzner"),
                primary_node_id="subnet-screener-1",
            )
            gce = _GCE()
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(_settings(Path(directory)))
            self.assertEqual(snapshot["gce_target"], 0)
            self.assertEqual(
                snapshot["fallback_reason"], "HETZNER_PRIMARY_ADMISSION_CLOSED"
            )
            self.assertEqual(gce.resized, [])

    def test_unavailable_provider_revision_fails_closed_to_gcp(self) -> None:
        for current_target in (0, 2):
            with (
                self.subTest(current_target=current_target),
                TemporaryDirectory() as directory,
            ):
                # No cached revision and no hold: neither a scale-out nor a blind
                # scale-in is safe while the provider policy cannot be read.
                settings = replace(
                    _settings(Path(directory)), inventory_failure_hold_passes=0
                )
                platform = _Platform(Demand(runnable=3, active=0, desired=2))
                gce = _GCE(target=current_target)
                with (
                    patch.object(
                        platform,
                        "provider_routing",
                        side_effect=ControllerError("provider settings unavailable"),
                    ),
                    patch(
                        "screener_capacity.controller.PlatformControl",
                        return_value=platform,
                    ),
                    patch("screener_capacity.controller.GCEFleet", return_value=gce),
                ):
                    snapshot = reconcile(settings)
                self.assertEqual(gce.resized, [])
                self.assertEqual(snapshot["gce_target"], current_target)
                self.assertFalse(snapshot["provider_ready"])
                self.assertEqual(
                    snapshot["fallback_reason"], "PROVIDER_ROUTING_UNAVAILABLE"
                )
                self.assertEqual(
                    snapshot["last_provider_error_code"],
                    "PROVIDER_ROUTING_UNAVAILABLE",
                )

    def test_unavailable_provider_revision_preserves_existing_capacity(self) -> None:
        with TemporaryDirectory() as directory:
            platform = _Platform(Demand(runnable=3, active=1, desired=3))
            gce = _GCE(target=2)
            with (
                patch.object(
                    platform,
                    "provider_routing",
                    side_effect=ControllerError("provider settings unavailable"),
                ),
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(_settings(Path(directory)))
            self.assertEqual(snapshot["gce_target"], 2)
            self.assertEqual(
                snapshot["last_provider_error_code"], "PROVIDER_ROUTING_UNAVAILABLE"
            )
            self.assertEqual(gce.resized, [])

    def test_gce_read_success_advances_success_timestamp_when_routing_fails(
        self,
    ) -> None:
        # last_provider_success_at means "last successful GCE fleet read". It
        # must advance on a good GCE read even if the routing read fails.
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(Demand(runnable=3, active=0, desired=2))
            gce = _GCE()
            before = datetime.now(UTC)
            with (
                patch.object(
                    platform,
                    "provider_routing",
                    side_effect=ControllerError("provider settings unavailable"),
                ),
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                snapshot = reconcile(settings)
            self.assertEqual(
                snapshot["last_provider_error_code"],
                "PROVIDER_ROUTING_UNAVAILABLE",
            )
            success_at = snapshot["last_provider_success_at"]
            self.assertIsNotNone(success_at)
            self.assertGreaterEqual(datetime.fromisoformat(success_at), before)
            self.assertEqual(
                platform.renewed[0]["last_provider_success_at"], success_at
            )

    _OPEN_PRIMARY: dict[str, dict[str, object]] = {
        "subnet-screener-1": {
            "status": "active",
            "ready": True,
            "admission_open": True,
            "screening_concurrency": 4,
        }
    }

    def _inventory_pass(
        self,
        settings: Settings,
        gce: _GCE,
        *,
        demand: Demand,
        routing: ProviderRouting | None,
        nodes: dict[str, dict[str, object]] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run one pass; a None routing or node inventory read fails."""
        renewed: list[dict[str, Any]] = []

        def failed_read() -> Any:
            raise ControllerError("Platform GET failed with HTTP 502")

        def renew(snapshot: dict[str, Any]) -> dict[str, Any]:
            renewed.append(snapshot)
            return snapshot

        platform = SimpleNamespace(
            demand=lambda **_kwargs: demand,
            provider_routing=failed_read if routing is None else lambda: routing,
            node_states=failed_read if nodes is None else lambda: nodes,
            renew=renew,
            fence=lambda **_kwargs: None,
        )
        with (
            patch(
                "screener_capacity.controller.PlatformControl", return_value=platform
            ),
            patch("screener_capacity.controller.GCEFleet", return_value=gce),
        ):
            snapshot = reconcile(settings)
        return snapshot, renewed

    def test_transient_node_inventory_failure_holds_gce_target(self) -> None:
        for current_target in (0, 2):
            with (
                self.subTest(current_target=current_target),
                TemporaryDirectory() as directory,
            ):
                gce = _GCE(target=current_target)
                snapshot, renewed = self._inventory_pass(
                    _settings(Path(directory)),
                    gce,
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(),
                    nodes=None,
                )

                self.assertEqual(gce.resized, [])
                self.assertEqual(snapshot["gce_target"], current_target)
                self.assertEqual(
                    snapshot["fallback_reason"], "PLATFORM_INVENTORY_UNAVAILABLE"
                )
                self.assertIn(
                    {
                        "event_type": "platform_inventory_unavailable",
                        "provider": "gcp",
                        "detail": (
                            f"nodes read failed; holding GCE target {current_target}"
                        ),
                    },
                    renewed[0]["events"],
                )

    def test_transient_routing_failure_holds_current_target_with_cached_revision(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            gce = _GCE()
            self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=2, active=0, desired=1),
                routing=replace(_overflow_routing(), revision=7),
                nodes=self._OPEN_PRIMARY,
            )

            snapshot, _ = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )

            self.assertEqual(gce.resized, [])
            self.assertEqual(snapshot["gce_target"], 0)
            self.assertEqual(snapshot["provider_settings_revision"], 7)
            self.assertTrue(snapshot["provider_ready"])
            self.assertIsNone(snapshot["last_provider_error_code"])

            self.assertEqual(
                snapshot["fallback_reason"], "PLATFORM_INVENTORY_UNAVAILABLE"
            )

    def test_persistent_node_inventory_failure_fails_closed_after_threshold(
        self,
    ) -> None:
        # After the hold, an unknown primary is still an operator stop that
        # cannot be ruled out, so GCE never scales up to the backlog.
        with TemporaryDirectory() as directory:
            settings = replace(
                _settings(Path(directory)), inventory_failure_hold_passes=2
            )
            gce = _GCE()
            for _ in range(2):
                snapshot, renewed = self._inventory_pass(
                    settings,
                    gce,
                    demand=Demand(runnable=24, active=0, desired=4),
                    routing=_overflow_routing(),
                    nodes=None,
                )
                self.assertEqual(
                    snapshot["fallback_reason"], "PLATFORM_INVENTORY_UNAVAILABLE"
                )

            snapshot, renewed = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=_overflow_routing(),
                nodes=None,
            )

            self.assertEqual(gce.resized, [])
            self.assertEqual(renewed[0]["gce_target"], 0)
            self.assertEqual(renewed[0]["fallback_reason"], "HETZNER_PRIMARY_UNKNOWN")
            self.assertFalse(snapshot["provider_ready"])
            self.assertEqual(
                snapshot["last_provider_error_code"],
                "PLATFORM_INVENTORY_UNAVAILABLE",
            )
            self.assertIn(
                {
                    "event_type": "platform_inventory_hold_expired",
                    "provider": "gcp",
                    "detail": "nodes read still failing after 2 held passes",
                },
                renewed[0]["events"],
            )

    def test_persistent_routing_failure_holds_target_and_wakes_watchdog(  # noqa: E501
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            settings = replace(
                _settings(Path(directory)), inventory_failure_hold_passes=1
            )
            gce = _GCE()
            self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=2, active=0, desired=1),
                routing=replace(_overflow_routing(), revision=7),
                nodes=self._OPEN_PRIMARY,
            )
            self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )
            self.assertEqual(gce.resized, [])

            snapshot, _ = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )

            self.assertEqual(gce.resized, [])
            self.assertEqual(
                snapshot["fallback_reason"], "PROVIDER_ROUTING_UNAVAILABLE"
            )
            self.assertEqual(snapshot["provider_settings_revision"], 7)
            self.assertFalse(snapshot["provider_ready"])
            self.assertEqual(
                snapshot["last_provider_error_code"],
                "PROVIDER_ROUTING_UNAVAILABLE",
            )

            # A preexisting positive target is held after the read hold
            # expires; a stale route cannot add or delete physical capacity.
            gce._target = 2
            snapshot, _ = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )
            self.assertEqual(gce.resized, [])
            self.assertEqual(snapshot["gce_target"], 2)

    def test_routing_failure_without_cache_fails_closed_after_threshold(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            settings = replace(
                _settings(Path(directory)), inventory_failure_hold_passes=1
            )
            gce = _GCE()
            snapshot, _ = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=3, active=0, desired=2),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )
            self.assertEqual(gce.resized, [])
            self.assertEqual(
                snapshot["fallback_reason"], "PLATFORM_INVENTORY_UNAVAILABLE"
            )
            self.assertFalse(snapshot["provider_ready"])

            snapshot, _ = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=3, active=0, desired=2),
                routing=None,
                nodes=self._OPEN_PRIMARY,
            )

            self.assertEqual(gce.resized, [])
            self.assertEqual(snapshot["gce_target"], 0)
            self.assertEqual(snapshot["provider_settings_revision"], 0)
            self.assertFalse(snapshot["provider_ready"])
            self.assertEqual(
                snapshot["fallback_reason"], "PROVIDER_ROUTING_UNAVAILABLE"
            )
            self.assertEqual(
                snapshot["last_provider_error_code"], "PROVIDER_ROUTING_UNAVAILABLE"
            )

    def test_inventory_failure_counter_resets_on_success(self) -> None:
        with TemporaryDirectory() as directory:
            settings = replace(
                _settings(Path(directory)), inventory_failure_hold_passes=1
            )
            gce = _GCE(target=2)
            failing = {
                "demand": Demand(runnable=24, active=0, desired=4),
                "routing": _overflow_routing(),
                "nodes": None,
            }
            self._inventory_pass(settings, gce, **failing)  # type: ignore[arg-type]
            self.assertEqual(
                json.loads(settings.state_file.read_text())["inventory_failures"], 1
            )

            self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=_overflow_routing(),
                nodes={
                    "subnet-screener-1": {
                        **self._OPEN_PRIMARY["subnet-screener-1"],
                        "ready": False,
                    }
                },
            )
            self.assertEqual(
                json.loads(settings.state_file.read_text())["inventory_failures"], 0
            )

            # A new outage starts a fresh hold instead of scaling in.
            snapshot, renewed = self._inventory_pass(settings, gce, **failing)  # type: ignore[arg-type]
            self.assertEqual(gce.resized, [4])
            self.assertEqual(snapshot["gce_target"], 4)
            self.assertIn(
                "platform_inventory_unavailable",
                [event["event_type"] for event in renewed[0]["events"]],
            )

    def test_corrupt_cached_routing_is_ignored(self) -> None:
        valid = {
            "revision": 7,
            "settings": {
                "runtime_provider_priority": ["hetzner", "gcp"],
                "source_review_provider_priority": ["hetzner", "gcp"],
                "build_provider_priority": ["hetzner", "gcp"],
                "gce_overflow_enabled": True,
                "primary_node_id": "subnet-screener-1",
                "gce_overflow_backlog_multiplier": 3,
                "gce_overflow_min_backlog": 12,
                "gce_overflow_max_instances": 6,
            },
        }
        for cached in (
            "not-a-routing",
            {"revision": 7},
            {**valid, "revision": -1},
            {
                **valid,
                "settings": {**valid["settings"], "build_provider_priority": [[1]]},
            },  # type: ignore[dict-item]
            {
                **valid,
                "settings": {**valid["settings"], "gce_overflow_max_instances": "x"},
            },  # type: ignore[dict-item]
        ):
            with self.subTest(cached=cached), TemporaryDirectory() as directory:
                settings = replace(
                    _settings(Path(directory)), inventory_failure_hold_passes=0
                )
                settings.state_file.write_text(
                    json.dumps({"last_good_provider_routing": cached})
                )
                gce = _GCE()
                snapshot, _ = self._inventory_pass(
                    settings,
                    gce,
                    demand=Demand(runnable=3, active=0, desired=2),
                    routing=None,
                    nodes=self._OPEN_PRIMARY,
                )

                self.assertEqual(snapshot["provider_settings_revision"], 0)
                self.assertEqual(
                    snapshot["last_provider_error_code"],
                    "PROVIDER_ROUTING_UNAVAILABLE",
                )

    def test_boolean_inventory_failure_count_starts_a_new_hold(self) -> None:
        with TemporaryDirectory() as directory:
            settings = replace(
                _settings(Path(directory)), inventory_failure_hold_passes=1
            )
            settings.state_file.write_text(json.dumps({"inventory_failures": True}))
            gce = _GCE(target=2)
            snapshot, renewed = self._inventory_pass(
                settings,
                gce,
                demand=Demand(runnable=24, active=0, desired=4),
                routing=_overflow_routing(),
                nodes=None,
            )

            self.assertEqual(snapshot["gce_target"], 2)
            self.assertEqual(gce.resized, [])
            self.assertEqual(
                json.loads(settings.state_file.read_text())["inventory_failures"], 1
            )
            self.assertIn(
                "platform_inventory_unavailable",
                [event["event_type"] for event in renewed[0]["events"]],
            )

    def test_inventory_transition_event_retries_after_first_renew_fails(self) -> None:
        for prior, event_type in (
            (0, "platform_inventory_unavailable"),
            (1, "platform_inventory_hold_expired"),
        ):
            with self.subTest(prior=prior), TemporaryDirectory() as directory:
                settings = replace(
                    _settings(Path(directory)), inventory_failure_hold_passes=1
                )
                settings.state_file.write_text(
                    json.dumps({"inventory_failures": prior})
                )
                renewed: list[dict[str, Any]] = []

                def failed_read() -> Any:
                    raise ControllerError("node inventory unavailable")

                def renew(
                    snapshot: dict[str, Any],
                    received: list[dict[str, Any]] = renewed,
                ) -> dict[str, Any]:
                    received.append(snapshot)
                    if len(received) == 1:
                        raise ControllerError("fenced renew failed")
                    return snapshot

                platform = SimpleNamespace(
                    demand=lambda **_kwargs: Demand(runnable=0, active=0, desired=0),
                    provider_routing=_overflow_routing,
                    node_states=failed_read,
                    renew=renew,
                    fence=lambda **_kwargs: None,
                )
                with (
                    patch(
                        "screener_capacity.controller.PlatformControl",
                        return_value=platform,
                    ),
                    patch(
                        "screener_capacity.controller.GCEFleet",
                        return_value=_GCE(),
                    ),
                ):
                    with self.assertRaisesRegex(ControllerError, "fenced renew failed"):
                        reconcile(settings)
                    self.assertEqual(
                        json.loads(settings.state_file.read_text())[
                            "inventory_failures"
                        ],
                        prior,
                    )
                    reconcile(settings)

                self.assertIn(
                    event_type,
                    [event["event_type"] for event in renewed[1]["events"]],
                )
                self.assertEqual(
                    json.loads(settings.state_file.read_text())["inventory_failures"],
                    prior + 1,
                )

    def test_inventory_event_retries_after_gce_read_fails(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            renewed: list[dict[str, Any]] = []
            reads = 0

            def failed_nodes() -> Any:
                raise ControllerError("node inventory unavailable")

            def target() -> int:
                nonlocal reads
                reads += 1
                if reads == 1:
                    raise ControllerError("GCE target unavailable")
                return 0

            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=0, active=0, desired=0),
                provider_routing=_overflow_routing,
                node_states=failed_nodes,
                renew=lambda snapshot: renewed.append(snapshot) or snapshot,
                fence=lambda **_kwargs: None,
            )
            gce = _GCE()
            gce.target = target  # type: ignore[method-assign]
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                with self.assertRaisesRegex(ControllerError, "GCE target unavailable"):
                    reconcile(settings)
                state = json.loads(settings.state_file.read_text())
                self.assertNotIn("inventory_failures", state)
                reconcile(settings)

            self.assertIn(
                "platform_inventory_unavailable",
                [event["event_type"] for event in renewed[0]["events"]],
            )
            self.assertEqual(
                json.loads(settings.state_file.read_text())["inventory_failures"], 1
            )

    def test_capacity_events_are_sent_once(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(
                Demand(runnable=4, active=0, desired=2),
                screening_priority=("gcp", "hetzner"),
                primary_node_id="subnet-screener-1",
                nodes={
                    "subnet-screener-1": {
                        "admission_open": True,
                        "screening_concurrency": 1,
                    }
                },
            )
            gce = _GCE()
            with (
                patch(
                    "screener_capacity.controller.PlatformControl",
                    return_value=platform,
                ),
                patch("screener_capacity.controller.GCEFleet", return_value=gce),
            ):
                reconcile(settings)

            self.assertEqual(gce.resized, [2])
            self.assertEqual(
                [event["event_type"] for event in platform.renewed[0]["events"]],  # type: ignore[attr-defined]
                ["gce_target_changed"],
            )
            self.assertEqual(platform.renewed[-1]["events"], [])

    def test_inventory_failure_hold_passes_flag(self) -> None:
        argv = [
            "--platform-url",
            "https://platform.invalid",
            "--platform-token-file",
            "/tmp/token",
            "--gce-project",
            "project",
            "--gce-region",
            "region",
            "--gce-mig",
            "mig",
        ]
        parser = build_parser()
        self.assertEqual(parser.parse_args(argv).inventory_failure_hold_passes, 4)
        args = parser.parse_args([*argv, "--inventory-failure-hold-passes", "0"])
        with patch("screener_capacity.controller._source_sha", return_value="a" * 40):
            self.assertEqual(controller_settings(args).inventory_failure_hold_passes, 0)
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args([*argv, "--inventory-failure-hold-passes", "-1"])

    def test_failed_gce_read_does_not_publish_success_timestamp(self) -> None:
        for failing in ("target", "counts"):
            with self.subTest(failing=failing), TemporaryDirectory() as directory:
                settings = _settings(Path(directory))
                platform = _Platform(Demand(runnable=3, active=0, desired=2))
                gce = _GCE()
                with (
                    patch.object(
                        gce, failing, side_effect=ControllerError("gce read failed")
                    ),
                    patch(
                        "screener_capacity.controller.PlatformControl",
                        return_value=platform,
                    ),
                    patch("screener_capacity.controller.GCEFleet", return_value=gce),
                    self.assertRaises(ControllerError),
                ):
                    reconcile(settings)
                # No snapshot (and so no fresh success timestamp) is sent.
                self.assertEqual(platform.renewed, [])


def test_retired_installed_unit_flags_are_inert() -> None:
    retired = {
        "targon-api-key-file": "/old/key",
        "targon-org-slug": "old",
        "targon-prefix": "old",
        "targon-platform-url": "https://old.invalid",
        "targon-capability-file": "/old/capability",
        "targon-resource": "old",
        "targon-worker-env-file": "/old/env",
        "gcp-bootstrap-service-account": "old@invalid",
        "gcp-bootstrap-delegate-service-account": "old@invalid",
        "source-review-secret-resource": "old",
        "targon-provisioning-timeout-seconds": "60",
    }
    argv = [
        "--platform-url",
        "https://platform.invalid",
        "--platform-token-file",
        "/tmp/token",
        "--gce-project",
        "project",
        "--gce-region",
        "region",
        "--gce-mig",
        "mig",
    ]
    for flag, value in retired.items():
        argv.extend((f"--{flag}", value))

    args = build_parser().parse_args(argv)
    with patch("screener_capacity.controller._source_sha", return_value="a" * 40):
        settings = controller_settings(args)
    assert settings.gce_mig == "mig"
    for flag in retired:
        assert not hasattr(settings, flag.replace("-", "_"))


if __name__ == "__main__":
    unittest.main()
