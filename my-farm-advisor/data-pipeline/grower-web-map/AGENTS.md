# Local Instructions

## Purpose

This folder owns the grower-level interactive web map workflow for the data
pipeline. It documents a runtime-installed generator that turns canonical
grower, farm, and field metadata plus boundary GeoJSON into one lightweight
Leaflet HTML file per grower.

## Safe edit scope

Edits should stay in this folder and its runtime-installed generator at
`../src/scripts/reporting/generate_grower_web_maps.py` unless the user
explicitly asks for a broader data-pipeline change. Do not change parent
`SKILL.md`, sibling workflows, or root policy from a subskill task unless
explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. If routing context is needed, read `../README.md`,
`../AGENTS.md`, and `../../SKILL.md`.

## Runtime contract

- `DATA_PIPELINE_DATA_ROOT` is required and must be an absolute writable path
  outside the skill checkout.
- Runtime base is `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`.
- The generator is installed at
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_web_maps.py`.
- Default map output is
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/derived/web-maps/`.
- The provenance manifest is
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower_slug>/manifests/grower_web_map.json`.
- Generated HTML and manifests stay outside the repository and out of Git.

## Local validation

From the repository root, run:

```bash
bash scripts/validate.sh
```

For a real runtime check, run the generator from the installed runtime source
copy with `--all-growers --check` and confirm that every grower map exists and
passes validation.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or
root files. Do not duplicate root-wide asset, vendor, or validation policy here
except this pointer to `../../../AGENTS.md`.
