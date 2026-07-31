"""Ask the wind and the rain whether two verdict classes are the same thing.

`qc/outliers.py` sorts excursions by extent — did the neighbours share it — and
says nothing about mechanism. Whether the sort corresponds to anything physical
is a question the module cannot answer about itself, so this asks data the
module never used, by the method `qc/sentinels.py` used to settle the 888/999
dispute: read the separately valid meteorology recorded at the same station and
the same hour.

    uv run python scripts/check_verdict_meteorology.py

The reason it exists is that a class here was once called
`suspected_instrument`, and the name asserted a cause. An instrument artefact
has no reason to prefer stagnant, dry hours; locally confined real pollution has
every reason to. Measured over 7,193,172 PM2.5 station-hours, 2015-2025:

    class                          P(WS<1)  P(WS<0.5)  med WS  P(rain>0)
    background                      0.194     0.049      1.8     0.082
    regional_episode                0.264     0.067      1.5     0.032
    uncorroborated_short_rise       0.238     0.065      1.6     0.075
    uncorroborated_sustained_rise   0.239     0.059      1.6     0.072

On wind the two classes barely separate — the gap between them is 13-38% of the
gap each has from ordinary hours. On rain they separate clearly, and in the
direction that matters: an episode hour is less than half as likely to be
raining as an ordinary one, while an uncorroborated hour rains at nearly the
background rate. An episode is stagnant *and* dry, which is what accumulation
looks like. The other class is mildly stagnant and not dry, and nothing in any
column says instrument. The verdict is now named for what was measured.

Requires the store and a completed `twair qc outliers` run.
"""

import io
import sys

import polars as pl

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "src")
from twair.paths import outputs_dir  # noqa: E402
from twair.qc.rainfall import usable  # noqa: E402
from twair.store.stations import normalise_name_expr  # noqa: E402
from twair.store.writer import scan_observations  # noqa: E402

POLLUTANT = "PM2.5"
FIRST, LAST = 2015, 2025

runs = pl.read_parquet(outputs_dir("qc_outliers") / "runs.parquet").filter(
    (pl.col("pollutant") == POLLUTANT) & pl.col("obs_year").is_between(FIRST, LAST)
)
print(f"{POLLUTANT} {FIRST}-{LAST}: {runs.height:,} runs")

# Every hour inside every run, labelled with its verdict.
hours = (
    runs.select(
        "station_name",
        "verdict",
        pl.datetime_ranges(pl.col("t_start"), pl.col("t_end"), interval="1h").alias("ts_local"),
    )
    .explode("ts_local", empty_as_null=False)
    .unique(subset=["station_name", "ts_local"])
)
print(f"labelled station-hours: {hours.height:,}")

# The measurand's own usable hours, so "background" means the same stations.
own = (
    scan_observations()
    .filter(pl.col("year").is_between(FIRST, LAST))
    .filter((pl.col("pollutant") == POLLUTANT) & usable())
    .select(normalise_name_expr(), "ts_local")
    .collect()
)
print(f"usable {POLLUTANT} hours: {own.height:,}")

met = (
    scan_observations()
    .filter(pl.col("year").is_between(FIRST, LAST))
    .filter(pl.col("pollutant").is_in(["WIND_SPEED", "RAINFALL"]) & usable())
    .select(normalise_name_expr(), "ts_local", "pollutant", pl.col("value").cast(pl.Float64))
    .collect()
    .pivot(on="pollutant", index=["station_name", "ts_local"], values="value")
)
print(f"station-hours with usable meteorology: {met.height:,}")

labelled = (
    own.join(hours, on=["station_name", "ts_local"], how="left")
    .with_columns(pl.col("verdict").fill_null("_background"))
    .join(met, on=["station_name", "ts_local"], how="inner")
)
print(f"joined: {labelled.height:,}")
print()

summary = (
    labelled.group_by("verdict")
    .agg(
        pl.len().alias("n"),
        (pl.col("WIND_SPEED") < 1.0).mean().alias("P_ws_lt_1"),
        (pl.col("WIND_SPEED") < 0.5).mean().alias("P_ws_lt_0p5"),
        pl.col("WIND_SPEED").median().alias("med_ws"),
        (pl.col("RAINFALL") > 0).mean().alias("P_rain"),
    )
    .sort("n", descending=True)
)
with pl.Config(tbl_cols=-1, tbl_width_chars=150, set_ascii_tables=True):
    print(summary)

rows = {r["verdict"]: r for r in summary.iter_rows(named=True)}
bg = rows.get("_background")
ep = rows.get("regional_episode")
un = rows.get("uncorroborated_short_rise")
if bg and ep and un:
    print()
    print("Gap between the two classes, as a fraction of each one's gap from background:")
    for name, key in (
        ("P(wind < 1 m/s)", "P_ws_lt_1"),
        ("P(wind < 0.5 m/s)", "P_ws_lt_0p5"),
        ("median wind speed", "med_ws"),
        ("P(rain > 0)", "P_rain"),
    ):
        from_bg = abs(ep[key] - bg[key])
        between = abs(un[key] - ep[key])
        ratio = between / from_bg if from_bg else float("nan")
        direction = "uncorroborated is CALMER/DRIER" if un[key] > ep[key] else ""
        if key == "med_ws":
            direction = "uncorroborated is WINDIER" if un[key] > ep[key] else ""
        print(
            f"  {name:20s} background {bg[key]:7.4f}  episode {ep[key]:7.4f}  "
            f"uncorroborated {un[key]:7.4f}   ratio {ratio:5.3f}  {direction}"
        )
