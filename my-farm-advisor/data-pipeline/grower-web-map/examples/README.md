# Grower Web Map Examples

This workflow generates runtime HTML, so example outputs are not committed to
the repository. Regenerate them from a real runtime with:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"

"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_maps.py \
  --all-growers \
  --check
```

Expected per-grower outputs:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/derived/web-maps/<grower_slug>_grower_map.html
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/manifests/grower_web_map.json
```

For the Assignment 1 runtime, the two expected maps are:

```text
.../growers/il-grower-01/derived/web-maps/il-grower-01_grower_map.html
.../growers/ia-grower-01/derived/web-maps/ia-grower-01_grower_map.html
```

Open either file in a browser. The sidebar lists all fields for the grower and
lets you zoom to a field; clicking a polygon shows its grower, farm, field,
crop, area, and location metadata.
