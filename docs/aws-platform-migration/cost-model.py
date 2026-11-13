#!/usr/bin/env python3
"""Recompute planning scenarios from captured rates; performs no cloud calls."""

import json
from pathlib import Path

INPUT = json.loads(Path(__file__).with_name("cost-inputs.json").read_text())
R = {key: value["usd"] for key, value in INPUT["rates"].items()}
H = INPUT["hours"]
D = INPUT["db"]


def common(traffic):
    tasks = INPUT["tasks"]
    return {
        "application_tasks": H
        * sum(
            t["count"]
            * (t["vcpu"] * R["fargate_vcpu_hour"] + t["gib"] * R["fargate_gib_hour"])
            for t in tasks
        ),
        "alb": H * (R["alb_hour"] + traffic["lcu"] * R["alb_lcu_hour"]),
        "two_nat_gateways": 2 * H * R["nat_hour"],
        "four_public_ipv4": 4 * H * R["public_ipv4_hour"],
        "nat_data": traffic["nat_gb"] * R["nat_gb"],
        "internet_data_out": traffic["out_gb"] * R["internet_egress_gb"],
        "cross_az_allowance": traffic["cross_az_gb"] * R["cross_az_side_gb"],
        **INPUT["allowances"],
    }


def rds(ha):
    prefix = "rds_multi" if ha else "rds_single"
    return (
        H * R[prefix + "_hour"]
        + D["storage_gib"] * R[prefix + "_gp3_gib_month"]
        + H * D["rds_di_monitored_vcpu"] * R["di_vcpu_hour"]
        + D["excess_backup_gib"] * R["rds_backup_gib_month"]
    )


def aurora(average_acu_each, ha, optimized=False):
    total_acu = average_acu_each * (2 if ha else 1)
    prefix = "aurora_io_optimized" if optimized else "aurora_standard"
    return (
        H * total_acu * (R[prefix + "_acu_hour"] + R["di_acu_hour"])
        + D["storage_gib"] * R[prefix + "_gib_month"]
        + D["excess_backup_gib"] * R["aurora_backup_gib_month"]
        + (0 if optimized else D["aurora_billable_io"] * R["aurora_io_operation"])
    )


def report():
    databases = {
        "rds_single": rds(False),
        "rds_multi": rds(True),
        "aurora_single_2acu": aurora(2, False),
        "aurora_ha_2acu_each": aurora(2, True),
        "aurora_ha_4acu_each": aurora(4, True),
        "aurora_ha_2acu_io_optimized": aurora(2, True, True),
    }
    scenarios = {}
    for name, traffic in INPUT["traffic_scenarios"].items():
        items = common(traffic)
        subtotal = sum(items.values())
        scenarios[name] = {
            "common_items": {k: round(v, 2) for k, v in items.items()},
            "common_total": round(subtotal, 2),
            "total_by_database": {
                k: round(subtotal + v, 2) for k, v in databases.items()
            },
        }
    premium = H * 4 * (
        R["aurora_io_optimized_acu_hour"] - R["aurora_standard_acu_hour"]
    ) + D["storage_gib"] * (
        R["aurora_io_optimized_gib_month"] - R["aurora_standard_gib_month"]
    )
    return {
        "db_monthly": {k: round(v, 2) for k, v in databases.items()},
        "scenarios": scenarios,
        "ha_2acu_each_io_optimized_break_even_million_io": round(
            premium / R["aurora_io_operation"] / 1e6, 2
        ),
        "planning_with_25_percent_contingency": {
            k: round(
                (sum(common(INPUT["traffic_scenarios"]["planning"]).values()) + v)
                * 1.25,
                2,
            )
            for k, v in databases.items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=2))
