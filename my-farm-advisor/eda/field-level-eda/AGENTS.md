# Local Instructions

## Purpose

This folder owns the field-level EDA workflow: static comparisons of field
boundaries, CDL/cropland history, and weather across fields and growers.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks
for a broader skill change. Do not change parent `SKILL.md`, sibling EDA
workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. If routing context is needed, read `../INDEX.md` and
`../../SKILL.md`.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes.
Run `--check-inputs` against the runtime before a full build:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  my-farm-advisor/eda/field-level-eda/src/field_eda.py --all-growers --check-inputs
```

## Report boundary

This workflow emits figures, tables, and a manifest only. Do not add report
prose, HTML, or dashboard generation here; the one-time report is assembled
outside the subskill.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or
root files. Do not duplicate root-wide asset, vendor, or validation policy here
except this pointer to `../../../AGENTS.md`.
