---
name: field-level-eda
description: Compare field boundaries, CDL/cropland history, and weather across fields and growers, producing static maps, statistical figures, and reusable tables.
version: 1.0.0
author: Boreal Bytes
tags: [eda, field-boundaries, cdl, cropland, weather, comparison, static-plots]
---

# Workflow: field-level-eda

## Description

This workflow explores a multi-grower data-pipeline runtime and produces a small,
reviewable set of **static** outputs: maps, statistical figures, CSV tables, and a
provenance manifest. It compares three layers for every field:

- **Field boundaries** — size, shape compactness, and layout.
- **CDL / cropland** — dominant crop, crop composition, rotation, and edge share.
- **Weather** — Apr–Oct growing degree days, precipitation, and heat extremes.

It is a reusable analysis step, not a report. The one-time report is assembled
later from the tables and figures this workflow writes.

## When to use it

Use it when a grower set has canonical boundaries, at least five years of CDL
tables, and daily weather, and the question is some version of:

- How do fields differ in size and shape within and across growers?
- What crop history and rotation does CDL show, and does it agree with the
  boundary crop label?
- How do heat and rainfall differ across growers and across years?
- Which field attributes move together (for example, shape and CDL edge noise)?

Do not use it for soil analysis, yield modeling, imagery/NDVI, or interactive
dashboards. It deliberately does not touch soil.

## Runtime inputs

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/
└── <grower_slug>/
    ├── grower.json
    └── farms/
        └── <farm_slug>/
            ├── farm.json
            ├── boundary/field_boundaries.geojson
            └── derived/tables/
                ├── <farm>_weather_<start>_<end>.csv
                ├── <farm>_<year>_cdl.csv          # one per CDL year
                └── <farm>_crop_rotation.csv
```

Per-field `fields/*/boundary/field_boundary.geojson` is used as a fallback when
the farm-level boundary file is missing. The workflow never writes inside
`growers/`.

## Quick start

Use the pipeline virtualenv so `geopandas`, `scipy`, and `matplotlib` resolve:

```bash
export DATA_PIPELINE_DATA_ROOT=/home/coder/my-farm-advisor-runtime
PY="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"

# Validate the input contract first (fast, no plots)
"${PY}" my-farm-advisor/eda/field-level-eda/src/field_eda.py \
  --all-growers --check-inputs

# Build the Phase 1a artifacts
"${PY}" my-farm-advisor/eda/field-level-eda/src/field_eda.py \
  --all-growers \
  --weather-years 2021-2025 \
  --cdl-years 2019-2023 \
  --season 4-10 \
  --dpi 150
```

Default output:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/derived/eda/field-comparison/latest/
```

Override with `--out`, restrict with `--grower <slug>` (repeatable), or clean
previous plots with `--clean`.

## What is computed

- **Geometry** in EPSG:5070: area, perimeter, and Polsby–Popper compactness
  (`4πA / P²`; 1.0 = circle).
- **Weather** for the requested season: GDD with a base of 10 °C and a cap of
  30 °C, precipitation totals, days ≥ 35 °C, wet days, and the longest dry spell.
- **CDL** per field-year: dominant crop and share, non-dominant ("edge") share,
  number of classes, and the rotation sequence from the pipeline table.
- **Agreement** between the boundary `crop` property and the latest CDL dominant
  crop.

## Artifacts and the story each tells

| Artifact | Scope | Question | Story |
|---|---|---|---|
| `maps/boundaries_by_grower.png` | Across growers | How are fields laid out and how varied are their shapes? | True-scale overview shaded by compactness, plus an 8 km detail inset on each grower's densest cluster. |
| `maps/cdl_2023_by_grower.png` | Across growers | Where are corn and soy in 2023? | Overview plus detail inset showing the 2023 CDL dominant class per field. |
| `maps/weather_gdd_by_grower.png` | Across growers | How does seasonal heat differ by region? | Shared GDD scale; within-grower variation is visually small. Overview plus detail inset. |
| `figures/b01_area_compactness_by_grower.png` | Within grower | Do growers manage different field sizes and shapes? | Two panels: area and compactness distributions, with a Kruskal–Wallis annotation. |
| `figures/c01_crop_composition_2019_2023.png` | Within field + within grower | What is each field growing, and how has the mix changed? | Per-field 2023 composition plus grower corn share by year. |
| `figures/w01_seasonal_distributions.png` | Within field + across growers | How do heat and rainfall vary by field and year? | GDD distributions by grower; per-field precipitation trajectories. |
| `figures/x01_compactness_vs_cdl_edge.png` | Within grower | Does field shape explain CDL edge noise? | Compactness vs edge share, raw and grower-centred. |

Tables:

| Table | Contents |
|---|---|
| `tables/grower_summary.csv` | One row per grower: fields, acres, size, compactness, crop split, rotation, season metrics, agreement. |
| `tables/boundary_comparison.csv` | One row per field: geometry, crop, CDL edge share, weather means. |
| `tables/cdl_comparison.csv` | One row per field: crop and dominant share per year, rotation, agreement. |
| `tables/weather_comparison.csv` | One row per field-year: GDD, precipitation, heat days, wet days, dry spell. |
| `tables/statistical_tests.csv` | Every test with n, statistic, p, BH-adjusted p, and effect size. |

`manifest.json` records parameters, input hashes, counts, key metrics, and the
output list.

## Statistical tests

- **Kruskal–Wallis** compares the three growers on field area, compactness, GDD,
  precipitation, and heat days. Effect size is ε².
- **Pairwise Mann–Whitney** with Benjamini–Hochberg correction follows a
  significant boundary comparison. Effect size is rank-biserial. Correction is
  applied within each metric family (three grower pairs per metric), not across
  metrics.
- **Within-grower corn vs soy** tests whether local weather separates crop
  choice, using field-level five-year means; BH is again applied per metric.
- **Spearman ρ** links compactness and CDL edge share, both overall and after
  removing grower means.

Weather tests use field-level five-year means (n = 10 per grower), not the 50
field-years, because fields on one farm share nearly identical gridded weather.

## Reading caveats

- **CDL is a 30 m classified raster.** "Edge share" is the fraction of pixels that
  are not the field's dominant class; it usually reflects grass, trees, roads, or
  mixed edges, not a second crop.
- **NASA POWER weather is gridded.** Fields within a farm share nearly the same
  values. Within-grower weather tests therefore have low power; absence of a
  relationship is expected.
- **Compactness is geometry, not soil compaction.** It describes the field
  outline, not bulk density or traffic compaction.
- **CDL years and weather years differ.** The runtime currently has CDL
  2019–2023 and weather 2021–2025; overlapping years support within-field time
  comparisons.
- **Maps use a detail inset.** Fields are 1–3 km across but a grower's parcels
  can span 30–65 km, so at true scale the parcels are small. Each map panel
  shows the full extent plus an 8 km inset on the densest cluster, where shapes
  and field IDs are readable. Inset label positions are nudged apart; the full
  field list is always in the tables.

## Output boundary

This workflow writes only under `--out` (by default a `derived/eda/` runtime
folder). It does not generate a narrative report, HTML, or an interactive
dashboard, and it does not modify `growers/`.

## Validation

```bash
"${PY}" my-farm-advisor/eda/field-level-eda/src/field_eda.py --all-growers --check-inputs
bash scripts/validate.sh
```

`--check-inputs` fails loudly when a grower, farm, boundary, weather year, CDL
year, or required column is missing. There is no silent fallback to another
farm or state.
