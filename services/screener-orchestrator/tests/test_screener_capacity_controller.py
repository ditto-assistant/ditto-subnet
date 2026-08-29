from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

from screener_capacity.controller import (
    ControllerError,
    Demand,
    GCEFleet,
    GCPBootstrapTokenMinter,
    ProviderCounts,
    ProviderRouting,
    Settings,
    build_parser,
    desired_slots,
    gce_capacity_target,
    reconcile,
)


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
        screening_priority: tuple[Literal["targon", "gcp"], ...] = (
            "targon",
            "gcp",
        ),
        build_priority: tuple[Literal["targon", "gcp"], ...] = (
            "targon",
            "gcp",
        ),
    ) -> None:
        self._demand = demand
        self._nodes = nodes or {}
        self.renewed: list[dict[str, object]] = []
        self.drained: list[str] = []
        self.fences = 0
        self._screening_priority = screening_priority
        self._build_priority = build_priority

    def demand(self, **_kwargs: object) -> Demand:
        return self._demand

    def provider_routing(self) -> ProviderRouting:
        return ProviderRouting(
            revision=0,
            runtime_provider_priority=self._screening_priority,
            source_review_provider_priority=self._screening_priority,
            build_provider_priority=self._build_priority,
        )

    def renew(self, snapshot: dict[str, object]) -> dict[str, object]:
        self.renewed.append(snapshot)
        return snapshot

    def fence(self, **_kwargs: object) -> None:
        self.fences += 1

    def node_states(self) -> dict[str, dict[str, object]]:
        return self._nodes

    def drain_node(
        self, *, node_id: str, epoch: str, reason: str = "capacity scale-down"
    ) -> None:
        del epoch, reason
        self.drained.append(node_id)


class _GCE:
    def __init__(self, target: int = 0, operations: list[str] | None = None) -> None:
        self._target = target
        self.resized: list[int] = []
        self.operations = operations

    def target(self) -> int:
        return self._target

    def counts(self) -> ProviderCounts:
        return ProviderCounts(healthy=self._target)

    def ensure_watchdog(self) -> None:
        return None

    def resize(self, target: int) -> None:
        self.resized.append(target)
        if self.operations is not None:
            self.operations.append(f"gce:{target}")
        self._target = target


class CapacityDecisionTests(unittest.TestCase):
    @patch("screener_capacity.controller.subprocess.run")
    def test_gce_resize_pauses_and_restores_scale_out_watchdog(
        self, run: object
    ) -> None:
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
            fleet.resize(1)

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

    def test_targon_first_lanes_still_scale_gce_workers(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = SimpleNamespace(
                demand=lambda **_kwargs: Demand(runnable=5, active=0, desired=3),
                provider_routing=lambda: ProviderRouting(
                    revision=0,
                    runtime_provider_priority=("targon", "gcp"),
                    source_review_provider_priority=("targon", "gcp"),
                    build_provider_priority=("targon", "gcp"),
                ),
                renew=lambda snapshot: snapshot,
                fence=lambda **_kwargs: None,
            )
            resized: list[int] = []
            gce = SimpleNamespace(
                target=lambda: 0,
                counts=lambda: ProviderCounts(),
                ensure_watchdog=lambda: None,
                resize=resized.append,
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
            self.assertEqual(resized, [3, 0])

    def test_gcp_first_policy_scales_gce_workers(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(
                Demand(runnable=4, active=0, desired=2),
                screening_priority=("gcp", "targon"),
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
            self.assertEqual(
                snapshot["fallback_reason"], "GCP_SCREENERS_PRIORITIZED_BY_POLICY"
            )

    def test_unavailable_provider_revision_fails_closed_to_gcp(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(Demand(runnable=3, active=0, desired=2))
            gce = _GCE()
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
            self.assertEqual(gce.resized, [2])
            self.assertEqual(
                snapshot["fallback_reason"], "PROVIDER_ROUTING_UNAVAILABLE"
            )
            self.assertEqual(
                snapshot["last_provider_error_code"],
                "PROVIDER_ROUTING_UNAVAILABLE",
            )

    def test_targon_first_decomposed_lanes_use_gce_workers(self) -> None:
        with TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            platform = _Platform(Demand(runnable=4, active=0, desired=2))
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
            self.assertEqual(snapshot["targon_capability"], "nogo")
            self.assertEqual(snapshot["targon_available"], 0)
            self.assertEqual(
                snapshot["fallback_reason"], "TARGON_NESTED_DOCKER_WORKER_LANE_RETIRED"
            )

class RetiredUnitFlagTests(unittest.TestCase):
    def test_parser_accepts_retired_nested_docker_unit_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "--platform-url",
                "https://platform.invalid",
                "--targon-platform-url",
                "https://platform.invalid",
                "--platform-token-file",
                "/token",
                "--targon-org-slug",
                "ditto",
                "--gce-project",
                "test-project",
                "--gce-region",
                "test-region",
                "--gce-mig",
                "test-mig",
                "--targon-capability-file",
                "/capability.json",
                "--targon-resource",
                "cpu-medium",
                "--targon-worker-env-file",
                "/worker-env.json",
                "--gcp-bootstrap-service-account",
                "bootstrap@example.iam.gserviceaccount.com",
                "--source-review-secret-resource",
                "projects/test/secrets/key",
                "--targon-provisioning-timeout-seconds",
                "600",
            ]
        )
        self.assertEqual(args.targon_org_slug, "ditto")
        self.assertEqual(args.targon_resource, "cpu-medium")


if __name__ == "__main__":
    unittest.main()
