# Consumption analysis pipeline

Run from the project root using the project's Python environment:

```bash
python src/run_pipeline.py --start 2013-07-01 --end 2014-01-01
```

## Inputs and period conventions

- Reuses the prepared DuckDB database; raw CSV ingestion is excluded.
- Dates use UTC interval starts: start inclusive, end exclusive.
- Period boundaries must align to half-hours.
- All assigned households require complete interval coverage.
- Profiles require all 96 features and positive mean consumption.
- Uses saved tariff prices, assignments and the profile model.
- Load joblib models only from trusted sources.

## Outputs

Each successful run creates a folder under reports/pipeline_runs/.
Outputs include band totals, bills, normalised profiles, monitoring,
a review queue, a run summary and a tariff configuration snapshot.

## Interpretation

- Charges are before rebates.
- The 2% threshold applies to the evaluated period.
- It is not a monthly bill guarantee.
- Profile changes do not establish tariff unsuitability.
- Review flags require human assessment.
- Variable costs are illustrative; contribution is not net profit.
- No training, optimisation or automatic switching occurs.
- Scheduling has not been configured.

## Checks

See notebooks/08_pipeline_development.ipynb for checks.
Check records are saved under reports/validation/.
