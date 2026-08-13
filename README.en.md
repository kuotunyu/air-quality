# air-quality｜Taiwan Air Quality Reanalysis

[![CI](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml/badge.svg)](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml)

> Every hourly observation published in MoENV's 1982–2025 annual archives:
> 340 million measurements, quality-flagged rather than quietly repaired,
> with an open pipeline and an interactive site on top.

### Taiwan's PM2.5 fell 60% between 2008 and 2025; meteorological normalisation assigns 43% of that fall to weather the model can see.

Measured by normalising out meteorological conditions — 61 stations, one set of rows, two lines.
Asked again by a completely different aggregation (the median of per-station slope ratios), the answer is 42.2%.
This is a model decomposition, not causal attribution to policy or emissions; the published M4 does not yet use ERA5 BLH, and long-range transport remains a limitation.
[See the chart →](https://kuotunyu.github.io/air-quality/trend/)

[繁體中文](README.md) ·
[Interactive site](https://kuotunyu.github.io/air-quality/) ·
Dataset — *not yet uploaded* ·
[Forecast demo](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) ·
[Methodology](docs/methodology.md)

---

## What is this?

An open, quality-controlled reanalysis of **44 years** of Taiwanese air-quality
monitoring — 1982 to 2025, 340,371,384 hourly observations across 82 stations.
Three things come out of it:

1. **A dataset that did not previously exist.** Taiwan's ministry publishes the
   raw annual archives, but in four different container formats, two date
   orders, and with a quality-flag convention that changes from year to year.
   This project parses all of it into one canonical, per-row-provenanced store.
2. **A methodological comparison.** A 2018 undergraduate project analysed a
   subset of this data using monthly means ($N = 7{,}286$), OLS with stepwise
   elimination, and a mixed model with AR(1). Re-running that method beside a
   corrected one, on the same data, prices each choice.
3. **A site that lets you check the work**, including SQL over the whole record
   in your own browser.

### The governing principle

**Measure and publish data quality; never silently repair it.** Invalid
readings are flagged, not deleted. Out-of-range values keep their number so
they stay inspectable. Aggregates below a coverage threshold return **null**,
never a biased mean. Every gap on every chart is a real gap.

### On the 2018 project

It is the starting point, not the subject. This repository names no author or
supervisor, and reproduces none of its figures — only its *method choices*
(matters of methodological fact) and its *published numbers* (independently
verifiable). The original PDF is gitignored and is not redistributed.

Two of those choices turn out to matter a great deal:

1. **Using PM10 to predict PM2.5.** PM2.5 is physically a subset of PM10, so
   this is definitional overlap rather than an empirical finding. Measured
   cost: **32.1% of the leaking model's $R^2$** (0.524 → 0.772).
2. **Treating wind direction ($0^\circ$–$360^\circ$) as a linear variable.**
   $0^\circ$ and $359^\circ$ are adjacent but numerically $359$ apart. Under
   OLS, sin/cos encoding gives **2.55×** the $R^2$ of the raw bearing.

And two claimed defects were **overturned by the full data** and are documented
as such — see [docs/working-rules.md](docs/working-rules.md).

---

## System Architecture

```mermaid
flowchart TD
    subgraph Data Sources [Measured Inputs]
        A[MoENV Annual Archive Catalogue] -->|Probe file IDs / gdown| D[Raw ZIP / 7z Archives]
        B[MoENV Station Register] -->|Metadata only| S[Station Metadata]
        C[CWA / ERA5 / Satellite] -.->|ERA5 2024–2025 acquisition and multi-year/held-out-station robustness delivered; CWA deferred| X[Future covariates]
    end

    subgraph Ingestion [Processing & Pipeline]
        D -->|twair build| G[Cross-generation CSV / XLS / ODS Parsing]
    end

    subgraph QC [Quality Control Suite]
        G -->|Flags parsing| H[Flag Classification]
        H_flag["#, *, x, A, NR, ND, -"] --> H
        G -->|Sentinel & Consistency Check| I[Physical Consistency]
        I_check["PM2.5 <= PM10, NO+NO2 ~ NOx"] --> I
        G -->|Sentinel Handling| J[Circular Sentinels]
        J_set["888 (variable dir), 999 (calm)"] --> J
    end

    subgraph Storage [Canonical Parquet Store]
        H & I & J --> K[observations/ year=YYYY/month=MM/]
        K -->|zstd Hive Partition| L[(Canonical Parquet Store)]
        L -->|coverage-gated aggregation| M[(Daily / Monthly / Wide Datasets)]
    end

    subgraph Analytics [Analysis Modules]
        L -->|twair qc outliers| N_out[Isolated Excursion vs Network Episode]
        M -->|M1| N[2018 Method Replication]
        M -->|M2-M3| O[Hourly Drivers & Pitfall Analysis]
        M -->|M4-M5| P[Weather Normalisation & Detection Limits]
        M -->|M6-M12| T[Spatial / Forecast / Health / Sensitivity Analyses]
    end

    subgraph Export [Web Packaging]
        O & P & T -->|twair export web| Q_json[L0 JSON / L1 Parquet Web Layer]
        Q_json -->|Embedded SVG Charts| R_astro[Astro Static Engine]
        Q_json -->|DuckDB WebAssembly| S_wasm[In-Browser SQL Query Engine]
    end

    classDef tech fill:#f9f8f6,stroke:#333,stroke-width:1.5px;
    class K,L,M,Q_json,S_wasm tech;
```

---

## Five Main Deliverables

| | Description |
|---|---|
| 📦 **Open-Source Dataset** | Public L0 station-month and L1 station-day aggregates for 1982–2025, downloadable from [chapter 10](https://kuotunyu.github.io/air-quality/data/) and packageable as a Hugging Face Dataset. The complete hourly L2 copy is not redistributed; the open pipeline and upstream archives rebuild it. |
| 📊 **Reproducible Science** | Step-by-step replication of the 2018 method, followed by rigorous corrections and quantitative comparisons. |
| 🌐 **Interactive Dashboard** | Routed evidence chapters covering trends, station summaries, observed high-value wind-speed/direction patterns (not source identity, position, transport distance, or contribution), event-detection limits, forecasting, health assumptions, and method comparisons. |
| 🔮 **Forecast Demo** | [Hugging Face Space](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) — PM2.5 one to 48 hours ahead, against two baselines. |
| 🔧 **Python Toolchain** | `twair` — An extensible, high-performance data pipeline with built-in QC, database management, and analysis. |

---

## Data Sources

MoENV and CWA data use Taiwan's *Government Data Open License, Version 1.0*.
Copernicus satellite inputs are governed separately by the
[Sentinel Data Legal Notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice).
See [docs/legal.md](docs/legal.md) for source-specific terms and redistribution boundaries.

| Source | Content | Spatial/Temporal | Span | Status |
|---|---|---|---|---|
| [Ministry of Environment (MoENV)](https://airtw.moenv.gov.tw/) | Historical Hourly Archives | All 82 monitoring stations | 1982–2025 | ✅ **all 44 years held**; every result here comes from this |
| [MoENV Open Data Platform](https://data.moenv.gov.tw/) | Station Meta & Live AQI API | GPS Coordinates & Daily Updates | Real-time | ✅ in use (station register, freshness check) |
| [Central Weather Administration (CWA)](https://opendata.cwa.gov.tw/) | Meteorological Stations | Barometric pressure, solar radiation, visibility | Historical | ⬜ **not yet acquired** |
| [Copernicus Climate Change Service](https://cds.climate.copernicus.eu/) | **ERA5 Boundary Layer Height (BLH)** | 10m wind, 2m temperature/dewpoint, surface pressure | 1940–Present | ✅ 2024–2025 acquisition plus multi-year and held-out-station robustness complete; calibration not delivered |
| [Sentinel-5P TROPOMI](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2) | TROPOMI L3 via Google Earth Engine | Station-month tropospheric NO₂ and vertical SO₂ columns | 2018–Present | 🟡 2024–2025 source acquisition, M8 association, and multi-year predictive robustness delivered; calibration/fusion not done |
| [MODIS MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) | MAIAC via Google Earth Engine | Aerosol optical depth | 2000–Present | 🟡 2024–2025 source acquisition, M8 association, and multi-year predictive robustness delivered; AOD calibration/fusion not done |

The 2025 M8 association and held-out predictive-value diagnostics delivered use
851 common complete station-months, 76 stations, and 12 months. Relative to a baseline containing
calendar seasonality and station geography, the combined all-satellite feature set improved both RMSE and R² in
3/4, 9/10, and 37/40 folds under held-quarter, held-station, and joint transfer, respectively.
Across the three designs, all satellite, AOD, NO₂, and SO₂ improved both metrics in 49/54,
44/54, 48/54, and 25/54 folds; SO₂ worsened both in 29/54. For the combined all-satellite feature set, the
overall median ΔRMSE was −0.588 µg/m³ and median ΔR² was +0.147. These 54 fold-evaluations
reuse the same 851 rows under three designs; they are not 54 independent years or stations, and
this is not future-year transfer. The evidence supports held-out predictive value within 2025 only,
not causality, calibration, fusion, or replacement of M4; calibration and fusion remain deferred.

The follow-up satellite multi-year robustness analysis used 76 common stations and
848 / 851 common complete station-months in 2024/2025; every baseline/candidate comparison
paired identical test rows. In all/AOD/NO₂/SO₂ order, within-year quarter replication improved
both RMSE and R² in 3/4, 4/4, 3/4, 3/4 folds in 2024 and 3/4, 3/4, 2/4, 1/4 in 2025;
the remaining folds worsened both metrics. The genuine future-year `2024_to_2025` test was
forward: improve / improve / improve / improve, with all-satellite at
−0.378 µg/m³ / +0.057 R². `2025_to_2024` is reverse-direction replication, not prediction of the past:
reverse: improve / improve / worsen / worsen, with all-satellite at −0.179 µg/m³ / +0.024 R².
When station and year were both held out, the forward direction improved in
10/10, 10/10, 9/10, 6/10 folds and the reverse in 10/10, 10/10, 9/10, 7/10.
This is predictive robustness only: not causal, calibration, fusion, satellite-estimated PM2.5,
and not a spatial-resolution claim or an M4 replacement.

ERA5 2025 source acquisition delivered 674,520 station-hour rows for 77 stations and
8,760 hours, with zero source nulls across all six variables. A separate value-add experiment
then compared temporal-only, station-weather, ERA5-weather, and combined information on the
same 632,760 complete station-hour rows from 74 stations and three forward local-time folds.
Combined versus station weather improved both RMSE and R² in 205 of 222 station-folds;
the median RMSE delta was −0.758 µg/m³ and the median R² delta was +0.249.

The follow-up robustness analysis used the same 74 stations and measured 636,244 paired rows in 2024 and 632,760 in 2025.
Combined versus station weather improved both metrics for 63/74, 66/74, and 70/74 stations in
same-station temporal, contemporaneous held-out-station spatial, and held-out-station-plus-year
transfer, respectively. Within-year replication improved 177/222 and 205/222 station-time-folds
in 2024 and 2025. ERA5 weather alone also improved both metrics for 59/74, 64/74, and 72/74
transfer stations, so the main increment is not merely the result of adding more features.

Sanchong, Tamsui, and Yangming had PM2.5 targets but no complete rows shared by all four
information sets in either year, so no value was filled and those stations were excluded.
Contemporaneous spatial transfer measures predictive generalisation, not future-year transfer.
These results are not causal attribution, calibration, or fusion. ERA5 has not been added to the published M4 model;
the released meteorological normalisation still uses station measurements.

---

## Quick Start

### 1. Synchronize the Environment
This project uses the modern, high-speed Python package manager `uv`:
```bash
uv sync
```

### 2. Configure Environment Variables
```bash
cp .env.example .env
```

### 3. Check System Integrity
```bash
uv run twair doctor
```

### 4. Locate & Cache Live Links
```bash
uv run twair probe sources
```
In a source checkout, the repository root is the workspace. When running an
installed wheel elsewhere, set `TWAIR_WORKSPACE_DIR` in the process environment
before startup; otherwise the current working directory is used. Relative
`data/`, `.env`, `conf/`, report, and probe paths stay in that external
workspace. If a `conf/*.yaml` override is absent, `twair` reads the reviewed,
read-only defaults packaged in the wheel. Refresh and probe commands create
workspace overrides and never rewrite the installed package.

The `probe sources` utility parses the live airtw annual catalogue, resolves current Google Drive identifiers, downloads one real archive sample, and populates `conf/sources.yaml` and `docs/data-sources.md`. Credentialed value-add sources remain explicitly unprobed when no credential is configured. Since government links change periodically, the catalogue is rediscovered rather than treated as a permanent hardcoded URL list.

---

## Project Status

For the measured state of the store and outputs, run `uv run twair status`.
The roadmap is in [PLAN.md](PLAN.md); durable evidence and decisions live in
the relevant public [technical docs](docs/).

| Phase | Current delivery | Disposition |
|---|---|---|
| **Phase 0** | Project skeleton, live airtw probe, real cross-generation samples, source documentation | ✅ Core complete; GEE satellite Stage A delivered; ERA5 2024–2025 acquisition and multi-year/held-out-station robustness delivered; CWA deferred |
| **Phase 1** | 1982–2025 canonical Parquet, QA/QC, coverage-aware aggregates | ✅ Complete; L0/L1 Dataset bundle is locally reproducible; remote publication needs owner confirmation |
| **Phase 2** | M1 replication, M2 hourly rebuild, M3 method comparisons, core report | ✅ Complete |
| **Phase 3** | Homepage, ten routed chapters, build-time SVG, DuckDB-WASM | ✅ Complete |
| **Phase 4** | M4 meteorological normalisation, M5 counterfactual + placebo detection limit | ✅ Bounded delivery; no policy-causal claim |
| **Phase 5** | M6 spatial structure and M7 CBPF observed high-value wind-speed/direction patterns (not source identity, position, transport distance, or contribution) | ✅ Bounded delivery; HYSPLIT and a 1 km field deferred |
| **Phase 6** | Satellite, ERA5, and low-cost sensor value-adds | 🟡 S5P and MAIAC source-acquisition Stage A delivered; the 2025 M8 association and held-out predictive-value diagnostics delivered; ERA5 2024–2025 robustness delivered; January 2025 micro-sensor observations, readiness, and grouped predictive benchmark delivered; January 2025 reference-station satellite-context predictive-value limit delivered; 2025 annual micro-sensor readiness audit delivered, while validated calibration and fusion remain deferred; not causal, not calibration, and not satellite-estimated PM2.5 |
| **Phase 7** | M9 four-horizon forecast, M12 SARIMA, public HF Space | ✅ Complete; DL/GNN stretch goals excluded |
| **Phase 8** | M10 health assumptions, CI, weekly freshness, full website narrative | 🔄 Closeout: HF Dataset and an external-reader trial remain; PyPI is optional |

The original blueprint and every superseded/deferred disposition remain in [PLAN.md](PLAN.md).

---

## Web Dashboard

To run the interactive web interface locally:
```bash
# Export analytics results from Parquet into static front-end assets
uv run twair export web                 

# Set up and preview the Astro server
cd web
npm install
npm run dev    # Dashboard launches on http://localhost:4321
```

### Dashboard Core Philosophy
1. **Charts are HTML native**: Charts are compiled into lightweight SVGs inside Astro frontmatter. The website loads immediately and is completely interactive even with JavaScript disabled in the browser.
2. **Null Values are Sacred**: Missing data points represent periods of station inactivity or insufficient data coverage. Instead of smoothly interpolating across gaps, charts render distinct breaks.
3. **No External Query Servers**: The chapter 9 explorer runs **DuckDB-WASM** compiled to WebAssembly. When a user explores daily records, DuckDB fetches only the required bytes from remote Parquet files via *HTTP Range Requests*—bypassing the need for database API keys or external server upkeep.

---

## Licensing

- **Software Source Code**: [MIT License](LICENSE)
- **Data Derivatives**: [Creative Commons Attribution 4.0 International (CC BY 4.0)](LICENSE-DATA). Source attribution belongs to the Ministry of Environment (Taiwan). County boundaries come from the National Land Surveying and Mapping Center under the Open Government Data License v1.0.

---

## Citation

A personal side project — no DOI, no formal citation format. Link to the repository
if you need to reference it, and credit the data to Taiwan's Ministry of Environment.
