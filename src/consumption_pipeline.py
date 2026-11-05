from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


def load_period_consumption(
    database_file,
    assignments_file,
    start_utc,
    end_utc,
):
    """Load and validate a complete UTC half-hour period."""
    database_file = Path(database_file)
    assignments_file = Path(assignments_file)

    if not database_file.is_file():
        raise FileNotFoundError(database_file)
    if not assignments_file.is_file():
        raise FileNotFoundError(assignments_file)

    start = pd.Timestamp(start_utc)
    end = pd.Timestamp(end_utc)

    start = (
        start.tz_localize("UTC")
        if start.tzinfo is None
        else start.tz_convert("UTC")
    )
    end = (
        end.tz_localize("UTC")
        if end.tzinfo is None
        else end.tz_convert("UTC")
    )

    if pd.isna(start) or pd.isna(end) or start >= end:
        raise ValueError("A valid start before end is required.")

    for boundary in (start, end):
        if boundary != boundary.floor("30min"):
            raise ValueError("Period boundaries must align to half-hours.")

    assignments = pd.read_csv(
        assignments_file,
        dtype={"household_id": "string"},
    )
    required_columns = {"household_id", "assigned_tariff"}
    if not required_columns.issubset(assignments.columns):
        raise ValueError("Required assignment columns are missing.")

    if assignments.empty or assignments["household_id"].isna().any():
        raise ValueError("Assignments must contain valid households.")
    if assignments["household_id"].duplicated().any():
        raise ValueError("Duplicate household assignments found.")
    if not assignments["assigned_tariff"].isin(
        ["Flat", "Time-of-use"]
    ).all():
        raise ValueError("Unknown or missing tariff assignment.")

    with duckdb.connect(
        str(database_file), read_only=True
    ) as connection:
        consumption = connection.execute(
            """
            SELECT household_id, interval_start_utc, energy_kwh
            FROM analysis_consumption_2013
            WHERE interval_start_utc >= ?
              AND interval_start_utc < ?
            ORDER BY household_id, interval_start_utc
            """,
            [
                start.tz_localize(None).to_pydatetime(),
                end.tz_localize(None).to_pydatetime(),
            ],
        ).fetchdf()

    consumption["interval_start_utc"] = pd.to_datetime(
        consumption["interval_start_utc"], utc=True
    )

    if set(consumption["household_id"]) != set(
        assignments["household_id"]
    ):
        raise ValueError("Period households differ from saved assignments.")

    if consumption.duplicated(
        ["household_id", "interval_start_utc"]
    ).any():
        raise ValueError("Duplicate household intervals found.")

    energy = consumption["energy_kwh"].to_numpy(dtype=float)
    if not np.isfinite(energy).all() or (energy < 0).any():
        raise ValueError("Energy values must be finite and non-negative.")

    expected_times = pd.date_range(
        start=start,
        end=end,
        freq="30min",
        inclusive="left",
    )
    if not consumption["interval_start_utc"].isin(
        expected_times
    ).all():
        raise ValueError("Unexpected interval timestamps found.")

    counts = consumption.groupby("household_id").size()
    if not counts.eq(len(expected_times)).all():
        raise ValueError("Incomplete household interval coverage.")

    return consumption


def calculate_period_bills(consumption, assignments_file, config):
    """Calculate energy charges using saved rates and assignments."""
    bands = ["Peak", "Shoulder", "Off-peak"]

    local_start = consumption["interval_start_utc"].dt.tz_convert(
        "Europe/London"
    )
    hour = local_start.dt.hour
    weekday = local_start.dt.dayofweek < 5

    time_band = np.select(
        [
            hour < 7,
            weekday & (hour >= 16) & (hour < 20),
        ],
        ["Off-peak", "Peak"],
        default="Shoulder",
    )

    band_totals = (
        consumption.assign(time_band=time_band)
        .groupby(["household_id", "time_band"])["energy_kwh"]
        .sum()
        .unstack("time_band", fill_value=0)
        .reindex(columns=bands, fill_value=0)
    )

    rates = pd.Series(config["rates_gbp_per_kwh"]).reindex(bands)
    costs = pd.Series(
        config["variable_cost_rates_gbp_per_kwh"]
    ).reindex(bands)
    baseline_rate = float(config["baseline_rate_gbp_per_kwh"])

    rate_values = np.r_[
        rates.to_numpy(dtype=float),
        costs.to_numpy(dtype=float),
        baseline_rate,
    ]
    if not np.isfinite(rate_values).all() or (rate_values < 0).any():
        raise ValueError("Rates must be finite and non-negative.")

    assignments = pd.read_csv(
        assignments_file,
        index_col="household_id",
    )

    if assignments.index.has_duplicates:
        raise ValueError("Duplicate household assignments found.")
    if set(assignments.index) != set(band_totals.index):
        raise ValueError("Assignments and consumption households differ.")
    if not assignments["assigned_tariff"].isin(
        ["Flat", "Time-of-use"]
    ).all():
        raise ValueError("Unknown or missing tariff assignment.")

    results = assignments[["assigned_tariff"]].reindex(
        band_totals.index
    ).copy()

    results["energy_kwh"] = band_totals.sum(axis=1)
    results["baseline_charge_gbp"] = (
        results["energy_kwh"] * baseline_rate
    )
    results["tou_charge_gbp"] = band_totals.mul(
        rates, axis="columns"
    ).sum(axis=1)
    results["assigned_charge_gbp"] = np.where(
        results["assigned_tariff"].eq("Time-of-use"),
        results["tou_charge_gbp"],
        results["baseline_charge_gbp"],
    )
    results["saving_gbp"] = (
        results["baseline_charge_gbp"]
        - results["assigned_charge_gbp"]
    )

    positive_baseline = results["baseline_charge_gbp"].where(
        results["baseline_charge_gbp"] > 0
    )
    results["saving_pct"] = (
        100 * results["saving_gbp"] / positive_baseline
    )
    results["bill_increase_pct"] = -results["saving_pct"]
    results["variable_cost_gbp"] = band_totals.mul(
        costs, axis="columns"
    ).sum(axis=1)

    return band_totals, results


def predict_period_profiles(consumption, model_file, metadata_file):
    """Predict eligible profiles; retain zero-consumption households."""
    import json
    import joblib

    metadata = json.loads(
        Path(metadata_file).read_text(encoding="utf-8")
    )
    local_start = consumption["interval_start_utc"].dt.tz_convert(
        "Europe/London"
    )
    profile_data = consumption.assign(
        day_type=np.where(
            local_start.dt.dayofweek < 5, "Weekday", "Weekend"
        ),
        slot=local_start.dt.hour * 2 + local_start.dt.minute // 30,
    )
    expected_columns = pd.MultiIndex.from_product(
        [["Weekday", "Weekend"], range(48)],
        names=["day_type", "slot"],
    )
    profiles = (
        profile_data
        .groupby(["household_id", "day_type", "slot"])["energy_kwh"]
        .mean()
        .unstack(["day_type", "slot"])
        .reindex(columns=expected_columns)
        .sort_index()
    )

    values = profiles.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Incomplete or invalid consumption profiles.")

    profile_means = profiles.mean(axis=1)
    eligible = profile_means.gt(0)

    # Zero-consumption households retain missing normalised features.
    normalised = profiles.div(
        profile_means.where(eligible), axis=0
    )
    normalised.columns = [
        f"{day_type.lower()}_{slot:02d}"
        for day_type, slot in expected_columns
    ]
    if list(normalised.columns) != metadata["feature_columns"]:
        raise ValueError("Features differ from saved model metadata.")

    # Load only trusted model files created in this project.
    model = joblib.load(model_file)
    if model.n_features_in_ != normalised.shape[1]:
        raise ValueError("Model feature count does not match profiles.")

    results = pd.DataFrame(index=profiles.index)
    results["profile_cluster"] = pd.Series(
        pd.NA, index=profiles.index, dtype="Int64"
    )
    results["profile_group"] = "Unavailable: zero consumption"
    results["profile_status"] = "zero_consumption"

    labels = {
        int(key): value
        for key, value in metadata["profile_labels"].items()
    }

    if eligible.any():
        predictions = model.predict(
            normalised.loc[eligible].to_numpy()
        )
        if not set(predictions).issubset(labels):
            raise ValueError("Predicted cluster has no saved label.")

        results.loc[eligible, "profile_cluster"] = predictions
        results.loc[eligible, "profile_group"] = [
            labels[int(cluster)] for cluster in predictions
        ]
        results.loc[eligible, "profile_status"] = "available"

    return normalised, results


def build_monitoring_results(
    bills,
    profile_groups,
    design_groups_file,
    review_increase_pct,
):
    """Flag charge issues, profile changes and unavailable profiles."""
    threshold = float(review_increase_pct)
    if not np.isfinite(threshold) or threshold < 0:
        raise ValueError("Review threshold must be finite and non-negative.")

    design_groups = pd.read_csv(
        design_groups_file, index_col="household_id"
    )
    for frame in (bills, profile_groups, design_groups):
        if frame.index.has_duplicates:
            raise ValueError("Duplicate household keys found.")

    if not (
        set(bills.index)
        == set(profile_groups.index)
        == set(design_groups.index)
    ):
        raise ValueError("Monitoring household keys do not match.")

    groups = profile_groups.copy()
    if "profile_status" not in groups:
        groups["profile_status"] = "available"

    results = bills.join(
        groups[["profile_cluster", "profile_group", "profile_status"]],
        validate="one_to_one",
    ).join(
        design_groups[["design_cluster", "profile_group"]].rename(
            columns={"profile_group": "design_profile_group"}
        ),
        validate="one_to_one",
    )

    money_columns = [
        "assigned_charge_gbp", "baseline_charge_gbp", "saving_gbp"
    ]
    if not np.isfinite(
        results[money_columns].to_numpy(dtype=float)
    ).all():
        raise ValueError("Invalid monitoring charge values.")

    if results["design_cluster"].isna().any():
        raise ValueError("Missing design cluster.")

    if not results["profile_status"].isin(
        ["available", "zero_consumption"]
    ).all():
        raise ValueError("Unknown profile status.")

    available = results["profile_status"].eq("available")
    if results.loc[available, "profile_cluster"].isna().any():
        raise ValueError("Available profiles require cluster predictions.")
    if results.loc[~available, "profile_cluster"].notna().any():
        raise ValueError("Unavailable profiles must not have predictions.")

    tolerance = 1e-6
    results["paying_more"] = (
        results["assigned_charge_gbp"]
        - results["baseline_charge_gbp"]
    ) > tolerance
    results["above_review_threshold"] = (
        results["assigned_charge_gbp"]
        - results["baseline_charge_gbp"] * (1 + threshold / 100)
    ) > tolerance

    results["profile_unavailable"] = ~available
    results["profile_changed"] = pd.Series(
        pd.NA, index=results.index, dtype="boolean"
    )
    results.loc[available, "profile_changed"] = (
        results.loc[available, "profile_cluster"]
        != results.loc[available, "design_cluster"]
    ).astype(bool)

    results["review_required"] = (
        results["paying_more"]
        | results["above_review_threshold"]
        | results["profile_changed"].fillna(False)
        | results["profile_unavailable"]
    )

    reasons = []
    for _, row in results.iterrows():
        items = []
        if row["paying_more"]:
            items.append("Higher charge than flat baseline")
        if row["above_review_threshold"]:
            items.append("Increase exceeds review threshold")
        if row["profile_unavailable"]:
            items.append("Profile unavailable: zero recorded consumption")
        elif row["profile_changed"]:
            items.append("Profile differs from design period")
        reasons.append("; ".join(items))

    results["review_reasons"] = reasons
    return results
