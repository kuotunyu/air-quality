# air-quality｜Taiwan Air Quality Reanalysis

[![CI](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml/badge.svg)](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml)

> Every hourly observation published in MoENV's 1982–2025 annual archives:
> 340 million measurements, quality-flagged rather than quietly repaired,
> with an open pipeline and an interactive site on top.

### Taiwan's PM2.5 fell 60% between 2008 and 2025; meteorological normalisation assigns 43% of that fall to weather the model can see.

Measured by normalising out meteorological conditions — 61 stations, one set of rows, two lines.
Asked again by a completely different aggregation (the median of per-station slope ratios), the answer is 42.2%.
This is a model decomposition, not causal attribution to policy or emissions; unobserved BLH and long-range transport remain limitations.
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
        C[CWA / ERA5 / Satellite] -.->|CWA/ERA5 deferred; satellite Stage A only| X[Future covariates]
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
| [Copernicus Climate Change Service](https://cds.climate.copernicus.eu/) | **ERA5 Boundary Layer Height (BLH)** | 10m wind, 2m temp, surface pressure | 1982–Present | ⬜ **not yet acquired** |
| [Sentinel-5P TROPOMI](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2) | TROPOMI L3 via Google Earth Engine | Station-month tropospheric NO₂ and vertical SO₂ columns | 2018–Present | 🟡 2025 source acquisition implemented; M8 analysis and fusion remain incomplete |
| [MODIS MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) | MAIAC via Google Earth Engine | Aerosol optical depth | 2000–Present | 🟡 2025 station-month batch acquisition completed; AOD/PM2.5 analysis and fusion remain incomplete |

Every meteorological variable in use today is measured by the air-quality stations' own instruments — temperature, humidity, rainfall, wind speed and bearing. Nothing comes from CWA or from a reanalysis yet.

> **Boundary Layer Height (BLH)** is the single most critical variable the original method omitted — and it is missing from this project too. Pollutant concentrations are roughly inversely proportional to the mixing boundary layer volume, so without BLH any attempt to model meteorological impacts on PM2.5 carries omitted-variable bias. The size of the gap is measurable: M4's meteorological normalisation has a median holdout R² of **0.445**, meaning more than half of hourly variance is not explained by local weather as currently observed. Adding BLH is one of the most valuable things left to do, and it has not been done, so it is written here as such.

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
The `probe sources` utility parses the live airtw annual catalogue, resolves current Google Drive identifiers, downloads one real archive sample, and populates `conf/sources.yaml` and `docs/data-sources.md`. Credentialed value-add sources remain explicitly unprobed when no credential is configured. Since government links change periodically, the catalogue is rediscovered rather than treated as a permanent hardcoded URL list.

---

## Project Status

For the measured state of the store and outputs, run `uv run twair status`.
The roadmap is in [PLAN.md](PLAN.md); durable evidence and decisions live in
the relevant public [technical docs](docs/).

| Phase | Current delivery | Disposition |
|---|---|---|
| **Phase 0** | Project skeleton, live airtw probe, real cross-generation samples, source documentation | ✅ Core complete; GEE satellite Stage A delivered; CWA/ERA5 deferred |
| **Phase 1** | 1982–2025 canonical Parquet, QA/QC, coverage-aware aggregates | ✅ Complete; L0/L1 Dataset bundle is locally reproducible; remote publication needs owner confirmation |
| **Phase 2** | M1 replication, M2 hourly rebuild, M3 method comparisons, core report | ✅ Complete |
| **Phase 3** | Homepage, ten routed chapters, build-time SVG, DuckDB-WASM | ✅ Complete |
| **Phase 4** | M4 meteorological normalisation, M5 counterfactual + placebo detection limit | ✅ Bounded delivery; no policy-causal claim |
| **Phase 5** | M6 spatial structure and M7 CBPF observed high-value wind-speed/direction patterns (not source identity, position, transport distance, or contribution) | ✅ Bounded delivery; HYSPLIT and a 1 km field deferred |
| **Phase 6** | Satellite and low-cost sensor fusion | 🟡 S5P and MAIAC source-acquisition Stage A delivered; analysis and fusion remain deferred and do not block this release |
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
