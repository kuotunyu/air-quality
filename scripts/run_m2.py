"""Run the M2 comparison over the 2018 project's own window.

Same years as M1 so the two are compared on the same data rather than on
different subsets.
"""

import time

import polars as pl

from twair.analysis.drivers import baseline_scores, build_modelling_frame, run_drivers
from twair.paths import outputs_dir

PERIOD = (2010, 2017)
FEATURE_SETS_TO_RUN = (
    "full",
    "full_with_pm10",
    "chemistry_only",
    "weather_only",
    "full_raw_wind",
)
# Leave-one-station-out over all 77 stations would be 77 fits. A sample spread
# across station types and regions answers the same question at a fraction of
# the cost.
LOSO_SAMPLE = 8


def main() -> None:
    out = outputs_dir("m2_drivers")
    out.mkdir(parents=True, exist_ok=True)

    t = time.time()
    frame = build_modelling_frame(period=PERIOD)
    print(
        f"frame: {frame.height:,} rows, {frame['station_name'].n_unique()} stations "
        f"({time.time() - t:.0f}s)",
        flush=True,
    )

    tables = [baseline_scores(frame, n_splits=3)]
    print("baselines done", flush=True)

    for name in FEATURE_SETS_TO_RUN:
        t = time.time()
        result = run_drivers(frame, feature_set=name, split_kind="rolling", n_splits=3)
        tables.append(result.summary())
        if result.importance is not None:
            result.importance.write_parquet(out / f"importance_{name}.parquet")
        print(f"rolling/{name}: {time.time() - t:.0f}s", flush=True)

    # Spatial and temporal generalisation, for the honest specification only.
    all_stations = sorted(frame["station_name"].unique().to_list())
    step = max(len(all_stations) // LOSO_SAMPLE, 1)
    sampled = all_stations[::step][:LOSO_SAMPLE]
    print(f"leave-one-station-out over {len(sampled)}: {sampled}", flush=True)

    t = time.time()
    loso = run_drivers(frame, feature_set="full", split_kind="station", stations=sampled)
    tables.append(loso.summary())
    print(f"station/full: {time.time() - t:.0f}s", flush=True)

    t = time.time()
    loyo = run_drivers(frame, feature_set="full", split_kind="year")
    tables.append(loyo.summary())
    print(f"year/full: {time.time() - t:.0f}s", flush=True)

    scores = pl.concat(tables, how="vertical_relaxed")
    scores.write_parquet(out / "scores.parquet")

    summary = (
        scores.group_by("model", "feature_set", "split_kind")
        .agg(
            pl.col("rmse").mean().alias("rmse"),
            pl.col("mae").mean().alias("mae"),
            pl.col("r2").mean().alias("r2"),
            pl.col("exceedance_f1").mean().alias("f1"),
            pl.len().alias("splits"),
        )
        .sort("split_kind", "rmse")
    )
    summary.write_csv(out / "summary.csv")

    with pl.Config(tbl_rows=40, tbl_width_chars=120, float_precision=4):
        print("\n=== M2 summary ===", flush=True)
        print(summary, flush=True)


if __name__ == "__main__":
    main()
