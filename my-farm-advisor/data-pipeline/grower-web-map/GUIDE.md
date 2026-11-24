---
name: grower-web-map
description: Generate lightweight grower-level interactive HTML maps from canonical data-pipeline field boundaries, with an Esri World Imagery basemap, clickable field metadata, and zoom-to-field controls.
license: Apache-2.0
compatibility: Python 3.11+ (standard library only); a modern browser with internet access for Leaflet and basemap tiles.
metadata:
  author: Superior Byte Works, LLC / borealBytes
  version: "1.0.0"
  category: geospatial
  tags: [web-map, leaflet, grower, field-boundaries, data-pipeline, interactive]
---

# Grower Web Map

Generate one lightweight interactive HTML map per grower from the boundaries
already produced by the data pipeline. The map is a single HTML file that
embeds only field vector geometry and a small metadata payload. It does not
embed imagery, rasters, or base64 data.

## What this workflow does

- Discovers every grower under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers`.
- Reads each farm's canonical `boundary/field_boundaries.geojson`.
- Falls back to per-field `fields/<field_slug>/boundary/field_boundary.geojson`
  files when a farm-level boundary file is missing.
- Joins `grower.json`, `farm.json`, `field.json`, and boundary properties into
  a compact map payload.
- Writes one standalone HTML map per grower.
- Writes a provenance manifest with input hashes and output metadata.

## When to use it

Use this workflow when a grower needs a shareable, browser-based view of all
their fields without shipping the full runtime data archive. It is a delivery
layer over the canonical pipeline output, not a replacement for the pipeline.

For richer offline dashboards or map embedding patterns, see the umbrella
[interactive web map guide](../../admin/interactive-web-map/GUIDE.md).

## Runtime inputs

The generator expects the normal data-pipeline runtime tree:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/
├── growers/
│   └── <grower_slug>/
│       ├── grower.json
│       └── farms/
│           └── <farm_slug>/
│               ├── farm.json
│               ├── boundary/field_boundaries.geojson
│               ├── manifests/field-inventory.csv
│               └── fields/
│                   └── <field_slug>/
│                       ├── field.json
│                       └── boundary/field_boundary.geojson
└── src/scripts/reporting/generate_grower_web_maps.py
```

Boundary files must be EPSG:4326 longitude/latitude GeoJSON. The pipeline's
canonical boundaries already satisfy that contract.

## Install or refresh the runtime copy

Run the data-pipeline installer from the skill checkout. It copies the
committed `src/` tree, including this generator, into the runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime

cd my-farm-advisor/data-pipeline
./scripts/install.sh \
  --data-root "$DATA_PIPELINE_DATA_ROOT" \
  --non-interactive \
  --force-refresh
```

The installed script is:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_web_maps.py
```

## Generate maps

Run from the runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"

"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_maps.py \
  --all-growers \
  --check
```

Render only selected growers:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_maps.py \
  --grower il-grower-01 \
  --grower ia-grower-01 \
  --check
```

Preview the planned output without writing files:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_maps.py \
  --all-growers \
  --dry-run
```

## Outputs

By default, each grower gets:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/derived/web-maps/<grower_slug>_grower_map.html
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/manifests/grower_web_map.json
```

Use `--output-dir /absolute/path` to write the HTML files somewhere else. The
manifest always stays with the grower under `manifests/`.

Generated HTML is runtime output and must stay outside the skill checkout.

## Map behavior

- **Basemap:** Esri World Imagery satellite tiles, with optional Esri place
  labels and an OpenStreetMap streets layer.
- **Fields:** every field polygon for the grower, colored by crop.
- **Zoom and pan:** standard Leaflet controls; basemap layer control; scale
  control.
- **Click metadata:** grower name, farm name, field name, field ID, crop,
  acres, county, state, and boundary source.
- **Field list:** grouped by farm; selecting a field zooms to its polygon and
  opens its popup.
- **Fit all fields:** restores the view to the full grower footprint.
- **Hide/show fields:** toggles the polygon layer without changing the basemap.

The HTML contains only vector coordinates and the metadata payload. It does
not contain satellite imagery, GeoTIFFs, NDVI rasters, or base64 images.

## Reproducibility

The generator sorts growers, farms, and fields deterministically. The manifest
records:

- generation time and tool version;
- grower, farm, and field counts;
- output path, byte size, and SHA-256;
- SHA-256 for each source metadata and boundary file;
- basemap URLs used by the HTML.

Re-running the command overwrites the HTML and manifest with the current
runtime data.

## Browser requirements

Open the HTML directly in a modern browser. Internet access is required for:

- Leaflet CSS and JavaScript from `unpkg.com`;
- Esri World Imagery and place-label tiles;
- OpenStreetMap tiles when that basemap is selected.

The field polygons and metadata still exist in the HTML when offline, but the
basemap tiles will not load.

## Validation

`--check` verifies that each generated HTML:

- is under the lightweight size threshold;
- contains the Esri World Imagery URL and Leaflet assets;
- contains the field list and fit-all controls;
- embeds parseable JSON with the expected grower, farm, and field records;
- does not embed binary image data.

After structural changes in the repository, also run:

```bash
bash scripts/validate.sh
```

## Notes and limits

- This workflow does not re-run the farm pipeline or modify field data.
- It does not fetch or embed new imagery; it only draws existing boundaries.
- The map is grower-level. If a grower has multiple farms, all farms and fields
  are included and grouped by farm in the sidebar.
- Esri and OpenStreetMap tile usage remains subject to their respective terms
  and attribution requirements.
