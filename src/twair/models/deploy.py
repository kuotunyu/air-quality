"""Bundle the forecast model for a HuggingFace Space.

The demo has to survive the finding it demonstrates. M9 measured that this
model beats persistence at every horizon and still decays to climatology by
48 hours, so a Space that prints one number and calls it a forecast would
contradict the chapter that produced it. Everything here is shaped so the app
can show **all three predictions and the observed value together**: the model,
the rule it has to beat, the long-run average it must not fall back to, and
what actually happened.

Three deliberate choices:

**LightGBM's own text format, not pickle.** A pickle couples the Space to the
exact library version that wrote it and executes arbitrary code on load. The
text format is portable, diffable, and inert.

**The bundle carries a sample, never the archive.** `docs/legal.md` settles
that derived products ship and a complete copy of the hourly record does not.
So the demo slice is a handful of stations over a single held-out year — a
fraction of a percent of the store, and enough to replay real forecasts.

**The held-out year is genuinely held out.** Models are fitted on everything
before `demo_year` and never see it. The demo is therefore a real out-of-sample
replay rather than a recital, and the numbers it shows can be compared against
the backtest without embarrassment.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.models.forecast import HORIZONS, MODEL_PARAMS, TARGET, build_feature_frame
from twair.paths import REPO_ROOT

log = logging.getLogger(__name__)

__all__ = [
    "DEMO_STATIONS",
    # Re-exported deliberately: the settings go into the bundle manifest, so
    # they are part of this module's surface even though `forecast` owns them.
    "MODEL_PARAMS",
    "BundleReport",
    "build_climatology",
    "build_space_bundle",
    "space_dir",
]

# Six stations chosen from M7's own result rather than by population: the CBPF
# analysis separated transport-dominated sites from locally-dominated ones
# without being told where any of them were, and these span that finding.
# 富貴角 and 馬公 are transport-dominated, 忠明 and 前金 are urban and local,
# 潮州 is rural, 埔里 sits in an inland basin where air stagnates.
#
# 陽明 was the original sixth and had to be dropped: it has PM2.5 for 2025 but
# no WS_HR or WD_HR, so every row died on the wind features. That is a real
# limit rather than a bad pick — **this model cannot serve a station without an
# anemometer**, and 3 of 77 stations are in that position. `build_space_bundle`
# now refuses to write a bundle that quietly lost a declared station.
DEMO_STATIONS: tuple[str, ...] = ("富貴角", "馬公", "忠明", "前金", "潮州", "埔里")

# Imported, not restated. The Space's whole claim is that it deploys the model
# whose skill M9 measured, and two dicts that agree today do not stay agreed.


def space_dir() -> Path:
    return REPO_ROOT / "spaces" / "forecast"


@dataclass(frozen=True, slots=True)
class BundleReport:
    horizons: tuple[int, ...]
    stations: tuple[str, ...]
    train_rows: dict[int, int]
    """Rows actually fitted, per horizon.

    Not the size of the hourly index, which is what this reported at first: the
    index counts every hour in the span including the ones with no usable
    observation, so it stayed at 507,480 when a station was swapped and told me
    nothing. The number worth printing is the one the model saw.
    """

    demo_rows: int
    features: int
    bytes: int

    def summary(self) -> str:
        # No square brackets around the list: this string is printed through
        # rich, which reads `[...]` as a style tag and silently drops anything
        # inside that is not one. It swallowed the row counts entirely.
        fitted = ", ".join(f"h{h}={n:,}" for h, n in sorted(self.train_rows.items()))
        return (
            f"{len(self.horizons)} model(s) fitted on {fitted} rows; "
            f"{self.demo_rows:,} demo rows, {self.bytes / 1_048_576:.1f} MB"
        )


def build_climatology(train: pl.DataFrame, *, column: str = TARGET) -> pl.DataFrame:
    """Mean by station, month and hour — the baseline that ignores today.

    Computed from the training rows only. Built from the *observed* column
    rather than from a shifted target so that one table serves every horizon:
    the climatological value for 3pm in March is the same number whether it is
    being predicted 1 hour or 48 hours ahead.
    """
    return (
        train.with_columns(
            pl.col("ts_local").dt.month().alias("month"),
            pl.col("ts_local").dt.hour().alias("hour"),
        )
        .group_by("station_name", "month", "hour")
        .agg(pl.col(column).mean().alias("climatology"))
        .sort("station_name", "month", "hour")
    )


def build_space_bundle(
    destination: Path | None = None,
    *,
    train_period: tuple[int, int] = (2015, 2024),
    demo_year: int = 2025,
    stations: tuple[str, ...] = DEMO_STATIONS,
    horizons: tuple[int, ...] = HORIZONS,
    seed: int = 20260729,
) -> BundleReport:
    """Fit one model per horizon and write everything the Space needs."""
    import lightgbm as lgb

    from twair.features.lags import add_target

    out = destination or space_dir()
    (out / "model").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)

    # One feature build covering training and demo years together, so the demo
    # rows carry lags reaching back into the training period rather than
    # starting cold on 1 January.
    featured, features = build_feature_frame(
        period=(train_period[0], demo_year), stations=list(stations)
    )
    log.info("feature frame: %d rows, %d features", featured.height, len(features))

    year = pl.col("ts_local").dt.year()
    climatology = build_climatology(featured.filter(year <= train_period[1]).drop_nulls([TARGET]))

    demo = featured.filter(year == demo_year)
    train_all = featured.filter(year <= train_period[1])

    fitted_rows: dict[int, int] = {}
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "target": TARGET,
        "features": features,
        "horizons": list(horizons),
        "stations": list(stations),
        "train_period": list(train_period),
        "demo_year": demo_year,
        "model_params": MODEL_PARAMS,
        "validation": (
            "Scored by rolling-origin backtest in twair.models.forecast, not by "
            "anything computed here. These models are fitted on the full training "
            "period and never see the demo year."
        ),
        "trained": {},
    }

    demo_columns = demo
    for horizon in horizons:
        train = add_target(train_all, column=TARGET, horizon=horizon).drop_nulls(
            ["target", *features]
        )
        model = lgb.LGBMRegressor(random_state=seed, **MODEL_PARAMS)
        model.fit(train.select(features).to_numpy(), train["target"].to_numpy())

        path = out / "model" / f"pm25_h{horizon}.txt"
        # Written through Python rather than `save_model`, which hands the path
        # to LightGBM's C library. On Windows that goes through the ANSI code
        # page and fails with "not available for writes" for any non-ASCII
        # path — and this repository's own directory name is non-ASCII.
        path.write_text(model.booster_.model_to_string(), encoding="utf-8")
        fitted_rows[horizon] = train.height
        manifest["trained"][str(horizon)] = {
            "rows": train.height,
            "file": f"model/pm25_h{horizon}.txt",
            "bytes": path.stat().st_size,
        }
        log.info("h%d: fitted on %d rows -> %s", horizon, train.height, path.name)

        # The observed value h hours later, so the app can show what actually
        # happened next to what each method said would happen.
        truth = add_target(demo, column=TARGET, horizon=horizon, name=f"truth_h{horizon}")
        demo_columns = demo_columns.with_columns(truth[f"truth_h{horizon}"])

    complete = demo_columns.drop_nulls(features).sort("station_name", "ts_local")

    # A station can vanish here without any error: it may report PM2.5 all year
    # and still lack an anemometer, in which case the wind features are null and
    # `drop_nulls` removes every one of its rows. That happened to 陽明 and the
    # bundle shipped five stations while the README described six. The counts
    # only ever get checked if something refuses to continue.
    survived = set(complete["station_name"].unique().to_list())
    lost = [s for s in stations if s not in survived]
    if lost:
        raise ValueError(
            f"{lost} produced no complete rows for {demo_year} — a required feature is "
            f"missing there (a station without WS_HR/WD_HR cannot be forecast by this "
            f"model). Pick different stations or drop the feature."
        )
    manifest["demo_skill"] = _demo_skill(complete, features, out, manifest, horizons)

    keep = [
        "station_name",
        "ts_local",
        TARGET,
        *features,
        *[f"truth_h{h}" for h in horizons],
    ]
    slim = complete.select([c for c in dict.fromkeys(keep) if c in complete.columns])
    slim.write_parquet(out / "data" / "demo.parquet", compression="zstd")
    climatology.write_parquet(out / "data" / "climatology.parquet", compression="zstd")

    manifest["demo_rows"] = slim.height
    manifest["demo_span"] = (
        [str(slim["ts_local"].min()), str(slim["ts_local"].max())] if slim.height else []
    )
    (out / "data" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return BundleReport(
        horizons=tuple(horizons),
        stations=tuple(stations),
        train_rows=fitted_rows,
        demo_rows=int(slim.height),
        features=len(features),
        bytes=total,
    )


def _demo_skill(
    complete: pl.DataFrame,
    features: list[str],
    out: Path,
    manifest: dict[str, Any],
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    """Skill on the bundled sample itself, overall and per station.

    Measured because it disagrees with the backtest, and the disagreement is
    the interesting part. On six stations over one year the model **loses** to
    persistence at six hours (-0.04) where the full backtest has it winning
    (+0.19), and 24-hour skill ranges from +0.03 at 埔里 to +0.29 at 富貴角.

    A demo that quoted only the headline number would have a reader watching
    the model lose while the page insisted it wins. Both numbers ship, and the
    app says which is which: this one describes 6 stations and one year, the
    other 74 stations and eleven.
    """
    import lightgbm as lgb

    from twair.models.forecast import skill_score

    overall: dict[str, Any] = {}
    by_station: dict[str, dict[str, float]] = {}

    for horizon in horizons:
        booster = lgb.Booster(
            model_str=(out / str(manifest["trained"][str(horizon)]["file"])).read_text(
                encoding="utf-8"
            )
        )
        rows = complete.drop_nulls([f"truth_h{horizon}"])
        if rows.is_empty():
            continue

        predicted = np.asarray(booster.predict(rows.select(features).to_numpy()))
        truth = rows[f"truth_h{horizon}"].to_numpy()
        persistence = rows[f"{TARGET}_lag1"].to_numpy()
        overall[str(horizon)] = {
            "n": int(rows.height),
            "rmse": round(float(np.sqrt(np.mean((predicted - truth) ** 2))), 3),
            "skill_persistence": round(skill_score(truth - predicted, truth - persistence), 3),
        }

        for (name,), part in rows.group_by("station_name", maintain_order=True):
            station = str(name)
            p = np.asarray(booster.predict(part.select(features).to_numpy()))
            t = part[f"truth_h{horizon}"].to_numpy()
            q = part[f"{TARGET}_lag1"].to_numpy()
            by_station.setdefault(station, {})[str(horizon)] = round(skill_score(t - p, t - q), 3)

    return {"overall": overall, "by_station": by_station}


def predict_row(booster: Any, row: pl.DataFrame, features: list[str]) -> float:
    """One prediction, with the feature order taken from the manifest.

    Column order is the whole risk in a deployed tree model: LightGBM takes a
    bare array and cannot tell that column 12 is now humidity rather than wind
    speed. The manifest is the single source of that order, and both the
    training code and the app read it from there.
    """
    matrix = row.select(features).to_numpy()
    return float(np.asarray(booster.predict(matrix))[0])
