#!/usr/bin/env python3
"""Offline monthly estimates for one app server and private managed PostgreSQL."""

import json
from pathlib import Path

INPUT = json.loads(Path(__file__).with_name("cost-inputs.json").read_text())
R = {key: value["usd"] for key, value in INPUT["rates"].items()}
H = INPUT["hours"]
D = INPUT["db"]


def common(traffic, app_rate=None):
    app = INPUT["app"]
    return {
        "app_compute": H * R[app_rate or app["default_rate"]],
        "app_gp3": app["gp3_gib"] * R["app_gp3_gib_month"],
        "app_snapshots": app["incremental_snapshot_gib"] * R["app_snapshot_gib_month"],
        "app_public_ipv4": H * app["public_ipv4_count"] * R["public_ipv4_hour"],
        "internet_out": traffic["internet_out_gb"] * R["internet_egress_gb"],
        **INPUT["allowances"],
    }


def rds(ha=False, advanced=True):
    prefix = "rds_multi" if ha else "rds_single"
    return {
        "compute": H * R[prefix + "_hour"],
        "storage": D["storage_gib"] * R[prefix + "_gp3_gib_month"],
        "chargeable_backup": D["excess_backup_gib"] * R["rds_backup_gib_month"],
        "advanced_insights": H * D["rds_di_monitored_vcpu"] * R["di_vcpu_hour"]
        if advanced
        else 0,
    }


def aurora(profile):
    return {
        "compute": H * profile["average_acu"] * R["aurora_standard_acu_hour"],
        "storage": D["storage_gib"] * R["aurora_standard_gib_month"],
        "chargeable_backup": D["excess_backup_gib"] * R["aurora_backup_gib_month"],
        "io": profile.get("io", D["aurora_billable_io"]) * R["aurora_io_operation"],
        "advanced_insights": H * profile["average_acu"] * R["di_acu_hour"]
        if profile["advanced"]
        else 0,
    }


def rounded(items):
    return {key: round(value, 2) for key, value in items.items()}


def report():
    databases = {
        "rds_single_advanced": rds(),
        "rds_single_log_monitoring": rds(advanced=False),
        "rds_multi_advanced": rds(ha=True),
    }
    databases.update(
        {"aurora_" + name: aurora(p) for name, p in INPUT["aurora_profiles"].items()}
    )
    totals = {name: sum(items.values()) for name, items in databases.items()}
    scenarios = {}
    for name, traffic in INPUT["traffic_scenarios"].items():
        items = common(traffic)
        base = sum(items.values())
        scenarios[name] = {
            "common_items": rounded(items),
            "common_total": round(base, 2),
            "total_by_database": rounded(
                {key: base + value for key, value in totals.items()}
            ),
            "with_15_percent_contingency": rounded(
                {key: (base + value) * 1.15 for key, value in totals.items()}
            ),
        }
    saving = H * (R[INPUT["app"]["default_rate"]] - R["ec2_t3a_xlarge_hour"])
    break_even = (
        totals["rds_single_advanced"]
        - (
            D["storage_gib"] * R["aurora_standard_gib_month"]
            + D["excess_backup_gib"] * R["aurora_backup_gib_month"]
            + D["aurora_billable_io"] * R["aurora_io_operation"]
        )
    ) / (H * (R["aurora_standard_acu_hour"] + R["di_acu_hour"]))
    return {
        "database_items": {name: rounded(items) for name, items in databases.items()},
        "database_totals": rounded(totals),
        "scenarios": scenarios,
        "t3a_monthly_saving_before_surplus_credits": round(saving, 2),
        "t3a_planning_totals_before_surplus_credits": rounded(
            {
                key: value - saving
                for key, value in scenarios["planning"]["total_by_database"].items()
            }
        ),
        "native_insights_aurora_break_even_average_acu_vs_rds_single": round(
            break_even, 3
        ),
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=2))
