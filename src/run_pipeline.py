import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from consumption_pipeline import (
    load_period_consumption,
    calculate_period_bills,
    predict_period_profiles,
    build_monitoring_results,
)


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate saved tariffs and profile models."
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--project",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()

    root = args.project.resolve()
    processed = root / "data" / "processed"
    models = root / "models"

    config_file = processed / "locked_optional_switch_tariff_2013.json"
    assignments_file = (
        processed / "locked_customer_tariff_assignments_2013.csv"
    )
    config = json.loads(config_file.read_text(encoding="utf-8"))

    consumption = load_period_consumption(
        processed / "electricity_analysis.duckdb",
        assignments_file,
        args.start,
        args.end,
    )
    bands, bills = calculate_period_bills(
        consumption, assignments_file, config
    )
    features, groups = predict_period_profiles(
        consumption,
        models / "design_profile_kmeans_2013.joblib",
        models / "design_profile_kmeans_2013.json",
    )
    monitoring = build_monitoring_results(
        bills,
        groups,
        processed / "customer_design_cluster_assignments_2013.csv",
        config["validation_bill_increase_limit_pct"],
    )

    revenue = float(bills["assigned_charge_gbp"].sum())
    cost = float(bills["variable_cost_gbp"].sum())
    baseline = float(bills["baseline_charge_gbp"].sum())

    if revenue <= 0 or baseline <= 0:
        raise ValueError("Portfolio revenue and baseline must be positive.")

    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_" + uuid4().hex[:8]
    )
    output = root / "reports" / "pipeline_runs" / run_id
    output.mkdir(parents=True, exist_ok=False)

    summary = {
        "run_id": run_id,
        "start_utc": consumption["interval_start_utc"].min().isoformat(),
        "end_exclusive_utc": (
            consumption["interval_start_utc"].max()
            + timedelta(minutes=30)
        ).isoformat(),
        "households": int(len(bills)),
        "intervals": int(len(consumption)),
        "energy_kwh": float(bills["energy_kwh"].sum()),
        "customer_saving_gbp": float(bills["saving_gbp"].sum()),
        "aggregate_saving_pct": float(
            100 * bills["saving_gbp"].sum() / baseline
        ),
        "contribution_margin_pct": 100 * (revenue - cost) / revenue,
        "minimum_margin_pct": config["minimum_portfolio_margin_pct"],
        "margin_constraint_pass": bool(
            100 * (revenue - cost) / revenue
            >= config["minimum_portfolio_margin_pct"]
        ),
        "review_increase_threshold_pct": config[
            "validation_bill_increase_limit_pct"
        ],
        "paying_more": int(monitoring["paying_more"].sum()),
        "above_review_threshold": int(
            monitoring["above_review_threshold"].sum()
        ),
        "profile_changed": int(monitoring["profile_changed"].sum()),
        "profile_unavailable": int(
            monitoring["profile_unavailable"].sum()
        ),
        "profile_comparable_households": int(
            monitoring["profile_changed"].notna().sum()
        ),
        "zero_baseline_households": int(
            bills["baseline_charge_gbp"].eq(0).sum()
        ),
        "review_required": int(monitoring["review_required"].sum()),
        "rebates_applied": False,
        "model_retrained": False,
        "scope": "Historical evaluation from prepared database records",
        "limitations": [
            "Review thresholds apply to the evaluated period.",
            "The 2% threshold is not a monthly bill guarantee.",
            "Profile changes do not establish tariff unsuitability.",
            "Variable costs are illustrative; contribution is not net profit.",
        ],
    }

    bands.to_csv(output / "band_totals.csv")
    bills.to_csv(output / "customer_bills.csv")
    features.to_csv(output / "normalised_profiles.csv")
    monitoring.to_csv(output / "customer_monitoring.csv")
    monitoring.loc[monitoring["review_required"]].to_csv(
        output / "review_queue.csv"
    )
    for filename, content in [
        ("run_summary.json", summary),
        ("tariff_config_snapshot.json", config),
    ]:
        (output / filename).write_text(
            json.dumps(content, indent=2, allow_nan=False),
            encoding="utf-8",
        )

    print("Pipeline completed.")
    print(f"Households: {summary['households']}")
    print(f"Review required: {summary['review_required']}")
    print(f"Results saved: {output}")


if __name__ == "__main__":
    main()
