# Asset Policy

## Repository hygiene

- Do not commit generated or downloaded runtime assets.
- Keep build artifacts, cache directories, and generated outputs out of Git.
- Geoadmin payloads such as `countries.geojson` and `states_usa.geojson` are runtime data, not repository content.

## Runtime data location

Raw and generated pipeline data lives outside Git under the runtime folder, normally `~/my-farm-advisor-runtime` and selected with `DATA_PIPELINE_DATA_ROOT`. Keep these out of the repository:

- `growers/`: boundary GeoJSON, per-field directories, derived tables, maps, manifests, and logs.
- `derived/`, `logs/`, `web-maps/`, `.venv/`, and root `data/`.
- `shared/cdl/` rasters, `shared/weather/`, and `shared/osm/` payloads.
- `boundary/field_boundaries.geojson`, per-field `field_boundary.geojson`, and `daily_weather.csv`.
- Generated tables such as `*_weather_*.csv`, `*_[0-9][0-9][0-9][0-9]_cdl.csv`, `*_crop_rotation.csv`, and `field-inventory.csv`.
- Generated reports such as `grower_web_map.json` and `*_grower_map.html`, plus raster and archive files.

## Small derived outputs

A summary may be committed only when it is small, human-reviewable, and reproducible. Keep each file at or below 100 KB and the whole commit at or below 1 MB, and state the regeneration command and data date in the file or commit message. Prefer text formats (`.md`, `.csv`, `.json`) over images. Never commit raw bundles, rasters, archives, credentials, or absolute local paths.

Audit the scope before committing:

```bash
scripts/check-commit-scope.sh                      # staged files
scripts/check-commit-scope.sh --range origin/main..HEAD
```

`scripts/validate.sh` runs the same guard against the staged index. Limits can be overridden with `COMMIT_SCOPE_MAX_FILE_BYTES` and `COMMIT_SCOPE_MAX_TOTAL_BYTES`.

## Large file handling

- Binary assets larger than 1 MB must use Git LFS.
- Required LFS-backed formats in this repo include `.npy`, `.npz`, `.raw`, `.bin`, `.so`, `.dylib`, and `.dll`.
- JSON and GeoJSON metadata files remain normal text files and must not be moved to Git LFS by default.

## Review requirements

- Any large file addition requires explicit reviewer approval.
- If a file should be regenerated or downloaded at runtime, exclude it instead of tracking it with Git or Git LFS.
