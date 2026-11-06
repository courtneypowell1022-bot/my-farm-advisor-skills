#!/usr/bin/env python3
"""Generate lightweight grower-level interactive HTML maps from pipeline boundaries.

The script reads the canonical data-pipeline runtime tree, embeds only field
polygon geometry and a small metadata payload, and writes one standalone HTML
file per grower. Basemap tiles and Leaflet are loaded from the internet; no
imagery, rasters, or binary payloads are embedded in the HTML.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

TOOL_NAME = "grower-web-map"
TOOL_VERSION = "1.0.0"
MAX_RECOMMENDED_BYTES = 500_000
DEFAULT_OUTPUT_SUBDIR = Path("derived") / "web-maps"

ESRI_IMAGERY_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ESRI_LABELS_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)
OSM_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"


@dataclass(slots=True)
class FieldRecord:
    field_id: str
    field_slug: str
    field_name: str
    farm_slug: str
    farm_name: str
    crop: str
    area_acres: float | None
    county: str
    state: str
    source: str
    geometry: dict[str, Any]
    source_path: Path


@dataclass(slots=True)
class FarmRecord:
    slug: str
    name: str
    state: str
    country: str
    path: Path


@dataclass(slots=True)
class GrowerResult:
    grower_slug: str
    grower_name: str
    farm_count: int
    field_count: int
    total_acres: float
    output_path: Path
    manifest_path: Path
    output_bytes: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate one lightweight interactive Leaflet map per grower from "
            "canonical data-pipeline field boundaries."
        )
    )
    parser.add_argument(
        "--data-root",
        help=(
            "Runtime root (for example ~/my-farm-advisor-runtime). Defaults to "
            "DATA_PIPELINE_DATA_ROOT."
        ),
    )
    parser.add_argument(
        "--all-growers",
        action="store_true",
        help="Render every grower. This is the default when --grower is omitted.",
    )
    parser.add_argument(
        "--grower",
        action="append",
        default=[],
        metavar="SLUG",
        help="Grower slug to render. Repeat for multiple growers. Defaults to all.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Absolute directory for generated HTML files. Defaults to each "
            "grower's derived/web-maps directory."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate each generated HTML file after writing it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned outputs without writing files.",
    )
    args = parser.parse_args(argv)
    if args.check and args.dry_run:
        parser.error("--check cannot be combined with --dry-run")
    if args.all_growers and args.grower:
        parser.error("--all-growers cannot be combined with --grower")
    return args


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_runtime_base(raw_data_root: str | None) -> Path:
    value = raw_data_root or os.environ.get("DATA_PIPELINE_DATA_ROOT", "")
    if not value.strip():
        fail(
            "DATA_PIPELINE_DATA_ROOT is required or pass --data-root. "
            "The runtime root must be an absolute path."
        )

    root = Path(value).expanduser()
    if not root.is_absolute():
        fail(f"runtime root must be an absolute path: {root}")

    root = root.resolve(strict=False)
    candidates = [root, root / "data-pipeline"]
    for candidate in candidates:
        if (candidate / "growers").is_dir() and (candidate / "src").is_dir():
            return candidate
    fail(
        f"could not find a data-pipeline runtime under {root}. "
        "Expected <root>/data-pipeline/growers and <root>/data-pipeline/src."
    )
    raise AssertionError("unreachable")


def validate_slug(value: str, label: str) -> str:
    slug = str(value).strip()
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        fail(f"invalid {label}: {value!r}")
    return slug


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_inventory(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            field_id = str(row.get("field_id") or "").strip()
            field_slug = str(row.get("field_slug") or "").strip()
            if field_id and field_slug:
                mapping[field_id] = field_slug
    return mapping


def natural_key(value: str) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
    )


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def round_coordinates(value: Any, ndigits: int = 6) -> Any:
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
        ):
            return [round(float(value[0]), ndigits), round(float(value[1]), ndigits)]
        return [round_coordinates(item, ndigits) for item in value]
    raise RuntimeError(f"invalid geometry coordinate value: {value!r}")


def iter_positions(node: Any) -> Iterable[tuple[float, float]]:
    if not isinstance(node, list):
        return
    if (
        len(node) >= 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in node[:2])
    ):
        yield float(node[0]), float(node[1])
        return
    for child in node:
        yield from iter_positions(child)


def validate_crs(document: dict[str, Any], path: Path) -> None:
    crs = document.get("crs")
    if crs is None:
        return
    text = json.dumps(crs).lower()
    if "4326" not in text and "crs84" not in text:
        raise RuntimeError(
            f"{path} declares a non-WGS84 CRS ({crs!r}); re-export boundaries in EPSG:4326 first"
        )


def normalize_geometry(geometry: Any, path: Path, field_id: str) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        raise RuntimeError(f"field {field_id} in {path} has no geometry object")
    geometry_type = geometry.get("type")
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise RuntimeError(
            f"field {field_id} in {path} has unsupported geometry type {geometry_type!r}"
        )

    coordinates = round_coordinates(geometry.get("coordinates"))
    positions = list(iter_positions(coordinates))
    if not positions:
        raise RuntimeError(f"field {field_id} in {path} has no polygon coordinates")

    for longitude, latitude in positions:
        if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
            raise RuntimeError(
                f"field {field_id} in {path} has an out-of-range coordinate "
                f"({longitude}, {latitude}); expected EPSG:4326 lon/lat"
            )

    return {"type": geometry_type, "coordinates": coordinates}


def load_farm_fields(farm: FarmRecord) -> list[FieldRecord]:
    inventory = read_inventory(farm.path / "manifests" / "field-inventory.csv")
    boundary_path = farm.path / "boundary" / "field_boundaries.geojson"
    feature_items: list[tuple[dict[str, Any], Path]] = []

    if boundary_path.exists():
        document = read_json(boundary_path)
        validate_crs(document, boundary_path)
        raw_features = document.get("features")
        if not isinstance(raw_features, list):
            raise RuntimeError(f"{boundary_path} is not a GeoJSON FeatureCollection")
        feature_items.extend(
            (item, boundary_path) for item in raw_features if isinstance(item, dict)
        )
    else:
        field_root = farm.path / "fields"
        if field_root.is_dir():
            for field_dir in sorted(field_root.iterdir(), key=lambda item: natural_key(item.name)):
                field_boundary = field_dir / "boundary" / "field_boundary.geojson"
                if not field_boundary.exists():
                    continue
                document = read_json(field_boundary)
                validate_crs(document, field_boundary)
                raw_features = document.get("features")
                if isinstance(raw_features, list):
                    feature_items.extend(
                        (item, field_boundary) for item in raw_features if isinstance(item, dict)
                    )
        if not feature_items:
            raise RuntimeError(
                f"farm {farm.slug} has no boundary/field_boundaries.geojson and no "
                "per-field boundary files"
            )

    records: dict[str, FieldRecord] = {}
    for index, (feature, feature_source) in enumerate(feature_items):
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        field_id = as_text(properties.get("field_id"))
        if not field_id:
            field_id = as_text(properties.get("field_slug")) or as_text(feature.get("id"))
        if not field_id:
            raise RuntimeError(f"farm {farm.slug} has a feature without a field_id at index {index}")

        field_slug = (
            inventory.get(field_id)
            or as_text(properties.get("field_slug"))
            or field_id
        )
        field_json_path = farm.path / "fields" / field_slug / "field.json"
        field_metadata: dict[str, Any] = {}
        if field_json_path.exists():
            field_metadata = read_json(field_json_path)

        field_name = (
            as_text(field_metadata.get("display_name"))
            or as_text(properties.get("field_name"))
            or as_text(properties.get("display_name"))
            or as_text(properties.get("name"))
            or field_id
        )
        crop = (
            as_text(properties.get("crop"))
            or as_text(properties.get("crop_name"))
            or as_text(field_metadata.get("crop"))
            or "Unknown"
        )
        area_acres = (
            as_float(properties.get("area_acres"))
            or as_float(properties.get("area_ac"))
            or as_float(field_metadata.get("area_acres"))
        )
        if area_acres is None:
            area_ha = (
                as_float(properties.get("area_ha_est"))
                or as_float(properties.get("area_ha"))
                or as_float(field_metadata.get("area_ha_est"))
            )
            if area_ha is not None:
                area_acres = area_ha * 2.471053814671653
        county = (
            as_text(properties.get("county"))
            or as_text(properties.get("county_name"))
            or as_text(field_metadata.get("county"))
        )
        state = as_text(properties.get("state")) or as_text(field_metadata.get("state")) or farm.state
        source = as_text(properties.get("source")) or "pipeline boundary archive"

        record = FieldRecord(
            field_id=field_id,
            field_slug=field_slug,
            field_name=field_name,
            farm_slug=farm.slug,
            farm_name=farm.name,
            crop=crop,
            area_acres=area_acres,
            county=county,
            state=state,
            source=source,
            geometry=normalize_geometry(feature.get("geometry"), feature_source, field_id),
            source_path=feature_source,
        )
        records[field_id] = record

    if not records:
        raise RuntimeError(f"farm {farm.slug} has no usable field polygons")

    return sorted(records.values(), key=lambda item: natural_key(item.field_id))


def discover_growers(runtime_base: Path, requested: list[str]) -> list[Path]:
    growers_root = runtime_base / "growers"
    if requested:
        candidates = [growers_root / validate_slug(slug, "grower slug") for slug in requested]
        missing = [str(path) for path in candidates if not path.is_dir()]
        if missing:
            fail("grower directory not found: " + ", ".join(missing))
        return candidates

    if not growers_root.is_dir():
        fail(f"growers directory not found under runtime: {growers_root}")
    return sorted(
        (path for path in growers_root.iterdir() if path.is_dir()),
        key=lambda item: item.name,
    )


def load_grower(grower_dir: Path) -> tuple[str, str, list[FarmRecord], list[FieldRecord], list[Path]]:
    grower_metadata_path = grower_dir / "grower.json"
    if not grower_metadata_path.exists():
        raise RuntimeError(f"missing grower metadata: {grower_metadata_path}")
    grower_metadata = read_json(grower_metadata_path)
    grower_slug = as_text(grower_metadata.get("grower_slug")) or grower_dir.name
    grower_name = as_text(grower_metadata.get("display_name")) or grower_slug

    farms_root = grower_dir / "farms"
    if not farms_root.is_dir():
        raise RuntimeError(f"grower {grower_slug} has no farms directory")

    farms: list[FarmRecord] = []
    fields: list[FieldRecord] = []
    source_paths: list[Path] = [grower_metadata_path]

    for farm_dir in sorted(
        (path for path in farms_root.iterdir() if path.is_dir()),
        key=lambda item: natural_key(item.name),
    ):
        farm_metadata_path = farm_dir / "farm.json"
        if not farm_metadata_path.exists():
            continue
        farm_metadata = read_json(farm_metadata_path)
        farm = FarmRecord(
            slug=as_text(farm_metadata.get("farm_slug")) or farm_dir.name,
            name=as_text(farm_metadata.get("display_name")) or farm_dir.name,
            state=as_text(farm_metadata.get("state")),
            country=as_text(farm_metadata.get("country")),
            path=farm_dir,
        )
        farm_fields = load_farm_fields(farm)
        farms.append(farm)
        fields.extend(farm_fields)
        source_paths.append(farm_metadata_path)
        source_paths.extend(record.source_path for record in farm_fields)
        for record in farm_fields:
            field_metadata_path = farm_dir / "fields" / record.field_slug / "field.json"
            if field_metadata_path.exists():
                source_paths.append(field_metadata_path)

    if not farms:
        raise RuntimeError(f"grower {grower_slug} has no farms with farm.json")
    if not fields:
        raise RuntimeError(f"grower {grower_slug} has no fields with boundary polygons")

    return grower_slug, grower_name, farms, fields, source_paths


def payload_for_grower(
    grower_slug: str,
    grower_name: str,
    farms: list[FarmRecord],
    fields: list[FieldRecord],
) -> dict[str, Any]:
    features = []
    for record in fields:
        features.append(
            {
                "type": "Feature",
                "geometry": record.geometry,
                "properties": {
                    "field_id": record.field_id,
                    "field_slug": record.field_slug,
                    "field_name": record.field_name,
                    "farm_slug": record.farm_slug,
                    "farm_name": record.farm_name,
                    "crop": record.crop,
                    "area_acres": record.area_acres,
                    "county": record.county,
                    "state": record.state,
                    "source": record.source,
                },
            }
        )
    return {
        "grower": {"slug": grower_slug, "name": grower_name},
        "farms": [
            {
                "slug": farm.slug,
                "name": farm.name,
                "state": farm.state,
                "country": farm.country,
            }
            for farm in farms
        ],
        "fields": {"type": "FeatureCollection", "features": features},
    }


def total_acres(fields: list[FieldRecord]) -> float:
    return sum(record.area_acres or 0.0 for record in fields)


def summary_text(farm_count: int, field_count: int, acres: float) -> str:
    farms_label = "farm" if farm_count == 1 else "farms"
    fields_label = "field" if field_count == 1 else "fields"
    if acres > 0:
        return f"{farm_count:,} {farms_label} · {field_count:,} {fields_label} · {acres:,.1f} acres"
    return f"{farm_count:,} {farms_label} · {field_count:,} {fields_label}"


def source_note(fields: list[FieldRecord]) -> str:
    sources = sorted({record.source for record in fields if record.source})
    return ", ".join(sources) if sources else "pipeline boundary archive"


def build_html(
    *,
    grower_name: str,
    farm_count: int,
    field_count: int,
    acres: float,
    source_text: str,
    payload: dict[str, Any],
) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_json = payload_json.replace("</", "<\\/")
    template = _HTML_TEMPLATE
    replacements = {
        "__TITLE__": html.escape(f"{grower_name} Grower Map"),
        "__GROWER_NAME__": html.escape(grower_name),
        "__SUMMARY__": html.escape(summary_text(farm_count, field_count, acres)),
        "__SOURCE_NOTE__": html.escape(source_text),
        "__DATA_JSON__": payload_json,
        "__ESRI_IMAGERY__": ESRI_IMAGERY_URL,
        "__ESRI_LABELS__": ESRI_LABELS_URL,
        "__OSM_URL__": OSM_URL,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def manifest_payload(
    *,
    runtime_base: Path,
    grower_slug: str,
    grower_name: str,
    farms: list[FarmRecord],
    fields: list[FieldRecord],
    source_paths: list[Path],
    output_path: Path,
    output_bytes: int,
    output_sha256: str,
) -> dict[str, Any]:
    unique_sources = sorted(
        {path for path in source_paths if path.exists()},
        key=lambda path: str(path),
    )
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "runtime_base": str(runtime_base),
        "grower_slug": grower_slug,
        "grower_name": grower_name,
        "farm_count": len(farms),
        "field_count": len(fields),
        "total_acres": round(total_acres(fields), 1),
        "output": str(output_path),
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
        "basemaps": {
            "default": ESRI_IMAGERY_URL,
            "labels": ESRI_LABELS_URL,
            "streets": OSM_URL,
        },
        "sources": [
            {"path": str(path), "sha256": sha256_file(path)}
            for path in unique_sources
        ],
        "fields": [
            {
                "field_id": record.field_id,
                "field_slug": record.field_slug,
                "farm_slug": record.farm_slug,
            }
            for record in fields
        ],
    }


def check_html(
    *,
    output_path: Path,
    grower_name: str,
    farms: list[FarmRecord],
    fields: list[FieldRecord],
) -> list[str]:
    text = output_path.read_text(encoding="utf-8")
    errors: list[str] = []
    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_RECOMMENDED_BYTES:
        errors.append(
            f"HTML is {byte_count:,} bytes; expected under {MAX_RECOMMENDED_BYTES:,} bytes"
        )
    if "data:image" in text or "base64," in text:
        errors.append("HTML appears to embed a binary image payload")
    if ESRI_IMAGERY_URL not in text:
        errors.append("Esri World Imagery basemap URL is missing")
    if "leaflet" not in text.lower():
        errors.append("Leaflet assets are missing")
    if "Fit all fields" not in text:
        errors.append("fit-all control is missing")
    if 'id="field-list"' not in text:
        errors.append("field list control is missing")

    match = re.search(
        r'<script id="grower-map-data" type="application/json">(.*?)</script>',
        text,
        re.DOTALL,
    )
    if not match:
        errors.append("embedded grower map JSON payload is missing")
        return errors

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        errors.append(f"embedded grower map JSON does not parse: {exc}")
        return errors

    actual_ids = {
        str(feature.get("properties", {}).get("field_id"))
        for feature in payload.get("fields", {}).get("features", [])
    }
    expected_ids = {record.field_id for record in fields}
    if actual_ids != expected_ids:
        errors.append(
            "embedded field IDs do not match the boundary inputs "
            f"(expected {sorted(expected_ids)}, got {sorted(actual_ids)})"
        )

    embedded_grower_name = str(payload.get("grower", {}).get("name", ""))
    if embedded_grower_name != grower_name:
        errors.append(
            f"embedded grower name is {embedded_grower_name!r}, expected {grower_name!r}"
        )

    embedded_farm_names = {
        str(farm.get("name", "")) for farm in payload.get("farms", []) if isinstance(farm, dict)
    }
    expected_farm_names = {farm.name for farm in farms}
    if embedded_farm_names != expected_farm_names:
        errors.append(
            "embedded farm names do not match farm metadata "
            f"(expected {sorted(expected_farm_names)}, got {sorted(embedded_farm_names)})"
        )

    return errors


def process_grower(
    *,
    runtime_base: Path,
    grower_dir: Path,
    output_dir: Path | None,
    dry_run: bool,
) -> GrowerResult:
    grower_slug, grower_name, farms, fields, source_paths = load_grower(grower_dir)
    payload = payload_for_grower(grower_slug, grower_name, farms, fields)
    html_text = build_html(
        grower_name=grower_name,
        farm_count=len(farms),
        field_count=len(fields),
        acres=total_acres(fields),
        source_text=source_note(fields),
        payload=payload,
    )

    target_dir = output_dir or (grower_dir / DEFAULT_OUTPUT_SUBDIR)
    output_path = target_dir / f"{grower_slug}_grower_map.html"
    manifest_path = grower_dir / "manifests" / "grower_web_map.json"

    if dry_run:
        return GrowerResult(
            grower_slug=grower_slug,
            grower_name=grower_name,
            farm_count=len(farms),
            field_count=len(fields),
            total_acres=total_acres(fields),
            output_path=output_path,
            manifest_path=manifest_path,
            output_bytes=len(html_text.encode("utf-8")),
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")
    output_bytes = output_path.stat().st_size
    manifest = manifest_payload(
        runtime_base=runtime_base,
        grower_slug=grower_slug,
        grower_name=grower_name,
        farms=farms,
        fields=fields,
        source_paths=source_paths,
        output_path=output_path,
        output_bytes=output_bytes,
        output_sha256=sha256_file(output_path),
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return GrowerResult(
        grower_slug=grower_slug,
        grower_name=grower_name,
        farm_count=len(farms),
        field_count=len(fields),
        total_acres=total_acres(fields),
        output_path=output_path,
        manifest_path=manifest_path,
        output_bytes=output_bytes,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_base = resolve_runtime_base(args.data_root)
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else None
    if output_dir is not None and not output_dir.is_absolute():
        fail("--output-dir must be an absolute path")

    grower_dirs = discover_growers(runtime_base, args.grower)
    if not grower_dirs:
        fail(f"no grower directories found under {runtime_base / 'growers'}")

    print("=" * 60)
    print("  Grower interactive web maps")
    print(f"  Runtime: {runtime_base}")
    print(f"  Growers: {len(grower_dirs)}")
    print("=" * 60)

    results: list[GrowerResult] = []
    failures: list[str] = []

    for grower_dir in grower_dirs:
        try:
            result = process_grower(
                runtime_base=runtime_base,
                grower_dir=grower_dir,
                output_dir=output_dir,
                dry_run=args.dry_run,
            )
            results.append(result)
            if not args.dry_run and args.check:
                _, _, farms, fields, _ = load_grower(grower_dir)
                errors = check_html(
                    output_path=result.output_path,
                    grower_name=result.grower_name,
                    farms=farms,
                    fields=fields,
                )
                if errors:
                    failures.extend(f"{result.grower_slug}: {error}" for error in errors)
        except Exception as exc:  # noqa: BLE001 - CLI should report per-grower failures.
            failures.append(f"{grower_dir.name}: {exc}")

    for result in results:
        action = "would write" if args.dry_run else "wrote"
        print()
        print(f"{result.grower_name} ({result.grower_slug})")
        print(
            f"  {result.farm_count} farm(s), {result.field_count} field(s), "
            f"{result.total_acres:,.1f} acres"
        )
        print(f"  {action}: {result.output_path} ({result.output_bytes:,} bytes)")
        if not args.dry_run:
            print(f"  manifest: {result.manifest_path}")

    print()
    if failures:
        print("Validation failures:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    if args.check and not args.dry_run:
        print(f"✓ All {len(results)} grower map(s) passed validation")
    else:
        print(f"✓ Generated {len(results)} grower map(s)")
    return 0


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root {
  color-scheme: light;
  --ink: #14251b;
  --muted: #5f7064;
  --line: #d9e2da;
  --panel: #ffffff;
  --soft: #f3f6f2;
  --accent: #1f6b3a;
  --accent-soft: #e4f2e8;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: var(--ink);
  background: var(--soft);
}
#app { display: flex; height: 100vh; min-height: 100vh; }
#sidebar {
  width: 340px;
  min-width: 300px;
  height: 100%;
  overflow-y: auto;
  padding: 18px;
  background: var(--panel);
  border-right: 1px solid var(--line);
  box-shadow: 4px 0 18px rgba(20, 37, 27, 0.06);
  z-index: 500;
}
#map { flex: 1; min-width: 0; height: 100%; background: #dfe8df; }
.eyebrow {
  margin: 0 0 4px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
h1 { margin: 0; font-size: 23px; line-height: 1.2; }
.summary { margin: 8px 0 0; color: var(--muted); font-size: 13px; }
.actions { display: flex; gap: 8px; margin: 18px 0 20px; }
.actions button {
  flex: 1;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--soft);
  color: var(--ink);
  padding: 9px 10px;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.actions button:hover { background: var(--accent-soft); border-color: #b8d8c2; }
.section-title {
  margin: 20px 0 8px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.farm-group { margin-bottom: 14px; }
.farm-group h2 { margin: 0 0 6px; font-size: 12px; color: var(--ink); }
.field-list { display: flex; flex-direction: column; gap: 5px; }
.field-item {
  width: 100%;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: var(--soft);
  color: var(--ink);
  text-align: left;
  font: inherit;
  cursor: pointer;
}
.field-item:hover { border-color: #b8d8c2; background: var(--accent-soft); }
.field-item.active { border-color: var(--accent); background: var(--accent-soft); }
.field-name { font-size: 13px; font-weight: 650; }
.field-meta { color: var(--muted); font-size: 11px; white-space: nowrap; }
.legend { display: grid; gap: 6px; }
.legend-item { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
.swatch { width: 13px; height: 13px; border: 1px solid rgba(0,0,0,0.18); border-radius: 3px; flex: none; }
.hint, .provenance { margin: 18px 0 0; color: var(--muted); font-size: 11px; line-height: 1.45; }
.provenance { border-top: 1px solid var(--line); padding-top: 12px; }
.popup { min-width: 210px; }
.popup-title { margin-bottom: 7px; font-size: 15px; font-weight: 700; }
.popup table { width: 100%; border-collapse: collapse; font-size: 12px; }
.popup th, .popup td { padding: 4px 0; vertical-align: top; text-align: left; border-bottom: 1px solid #edf1ed; }
.popup th { width: 34%; color: var(--muted); font-weight: 600; }
.leaflet-control-layers { font-size: 12px; }
@media (max-width: 760px) {
  #app { flex-direction: column; }
  #sidebar { width: 100%; height: 44vh; min-height: 300px; border-right: 0; border-bottom: 1px solid var(--line); box-shadow: 0 4px 18px rgba(20, 37, 27, 0.06); }
  #map { height: 56vh; }
}
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <p class="eyebrow">Grower web map</p>
    <h1>__GROWER_NAME__</h1>
    <p class="summary">__SUMMARY__</p>

    <div class="actions">
      <button type="button" id="fit-all">Fit all fields</button>
      <button type="button" id="toggle-fields">Hide fields</button>
    </div>

    <div class="section-title">Fields</div>
    <div id="field-list" class="field-list"></div>

    <div class="section-title">Crop legend</div>
    <div id="legend" class="legend"></div>

    <p class="hint">Click a field polygon or a field name to inspect metadata. Basemap tiles require an internet connection.</p>
    <p class="provenance">Boundaries: __SOURCE_NOTE__</p>
  </aside>
  <main id="map" aria-label="Interactive grower field map"></main>
</div>

<script id="grower-map-data" type="application/json">__DATA_JSON__</script>
<script>
(function () {
  "use strict";

  const data = JSON.parse(document.getElementById("grower-map-data").textContent);
  const fields = data.fields.features;
  const layersByField = new Map();

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[character];
    });
  }

  function cropKey(value) {
    return String(value == null ? "Unknown" : value).trim().toLowerCase();
  }

  function colorForCrop(value) {
    const crop = cropKey(value);
    if (crop.includes("soy")) return "#4caf50";
    if (crop.includes("corn") || crop.includes("maize")) return "#f4c542";
    if (crop.includes("wheat")) return "#d9913b";
    if (crop.includes("cotton")) return "#e8e8e8";
    if (crop.includes("alfalfa") || crop.includes("hay")) return "#7cb342";
    return "#7aa6d6";
  }

  function formatAcres(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "—";
  }

  function fieldKey(properties) {
    return String(properties.farm_slug || "") + "::" + String(properties.field_slug || properties.field_id || "");
  }

  function popupHtml(properties) {
    const rows = [
      ["Grower", data.grower.name],
      ["Farm", properties.farm_name],
      ["Field", properties.field_name],
      ["Field ID", properties.field_id],
      ["Crop", properties.crop],
      ["Area", properties.area_acres == null ? "—" : formatAcres(properties.area_acres) + " acres"],
      ["County", properties.county],
      ["State", properties.state],
      ["Source", properties.source]
    ].filter(function (row) {
      return row[1] != null && String(row[1]).trim() !== "";
    });

    return '<div class="popup"><div class="popup-title">' +
      escapeHtml(properties.field_name || properties.field_id || "Field") +
      '</div><table>' +
      rows.map(function (row) {
        return "<tr><th>" + escapeHtml(row[0]) + "</th><td>" + escapeHtml(row[1]) + "</td></tr>";
      }).join("") +
      "</table></div>";
  }

  const map = L.map("map", {
    zoomControl: true,
    preferCanvas: true,
    worldCopyJump: true
  });

  const satellite = L.tileLayer("__ESRI_IMAGERY__", {
    maxZoom: 19,
    maxNativeZoom: 19,
    attribution: "Tiles &copy; Esri"
  });
  const labels = L.tileLayer("__ESRI_LABELS__", {
    maxZoom: 19,
    maxNativeZoom: 19,
    opacity: 0.9,
    attribution: "Tiles &copy; Esri"
  });
  const streets = L.tileLayer("__OSM_URL__", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  });

  satellite.addTo(map);
  labels.addTo(map);
  L.control.layers(
    { "Esri World Imagery": satellite, "OpenStreetMap": streets },
    { "Esri place labels": labels },
    { collapsed: false }
  ).addTo(map);
  L.control.scale({ imperial: true, metric: true }).addTo(map);

  const fieldsLayer = L.geoJSON(data.fields, {
    style: function (feature) {
      return {
        color: "#ffffff",
        weight: 2,
        opacity: 0.95,
        fillColor: colorForCrop(feature.properties.crop),
        fillOpacity: 0.30
      };
    },
    onEachFeature: function (feature, layer) {
      const properties = feature.properties || {};
      const key = fieldKey(properties);
      layersByField.set(key, layer);
      layer.bindPopup(popupHtml(properties), { maxWidth: 340, autoPan: true });
      layer.bindTooltip(escapeHtml(properties.field_name || properties.field_id || "Field"), {
        sticky: true,
        direction: "top",
        opacity: 0.9
      });
      layer.on({
        mouseover: function () {
          layer.setStyle({ weight: 4, fillOpacity: 0.48 });
          layer.bringToFront();
        },
        mouseout: function () {
          fieldsLayer.resetStyle(layer);
        },
        click: function () {
          setActiveField(key);
        }
      });
    }
  }).addTo(map);

  const allBounds = fieldsLayer.getBounds();

  function setActiveField(key) {
    document.querySelectorAll(".field-item").forEach(function (element) {
      element.classList.toggle("active", element.dataset.fieldKey === key);
    });
  }

  function zoomToField(key) {
    const layer = layersByField.get(key);
    if (!layer) return;
    map.fitBounds(layer.getBounds().pad(0.2), { maxZoom: 17 });
    layer.openPopup();
    setActiveField(key);
  }

  function addFieldList() {
    const list = document.getElementById("field-list");
    const fieldsByFarm = new Map();

    data.farms.forEach(function (farm) {
      fieldsByFarm.set(farm.slug, []);
    });
    fields.forEach(function (feature) {
      const properties = feature.properties || {};
      const farmSlug = properties.farm_slug || "";
      if (!fieldsByFarm.has(farmSlug)) fieldsByFarm.set(farmSlug, []);
      fieldsByFarm.get(farmSlug).push(properties);
    });

    fieldsByFarm.forEach(function (farmFields, farmSlug) {
      if (!farmFields.length) return;
      const farm = data.farms.find(function (item) { return item.slug === farmSlug; });
      const group = document.createElement("section");
      group.className = "farm-group";

      const heading = document.createElement("h2");
      heading.textContent = farm ? farm.name : farmSlug;
      group.appendChild(heading);

      farmFields.sort(function (a, b) {
        return String(a.field_id).localeCompare(String(b.field_id), undefined, { numeric: true });
      }).forEach(function (properties) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "field-item";
        button.dataset.fieldKey = fieldKey(properties);
        const areaText = properties.area_acres == null
          ? ""
          : " · " + formatAcres(properties.area_acres) + " ac";
        button.innerHTML =
          '<span class="field-name">' + escapeHtml(properties.field_name || properties.field_id) + "</span>" +
          '<span class="field-meta">' + escapeHtml(properties.crop || "Unknown") + areaText + "</span>";
        button.addEventListener("click", function () {
          zoomToField(button.dataset.fieldKey);
        });
        group.appendChild(button);
      });

      list.appendChild(group);
    });
  }

  function addLegend() {
    const labelsByCrop = new Map();
    fields.forEach(function (feature) {
      const label = String(feature.properties.crop || "Unknown");
      const key = cropKey(label);
      if (!labelsByCrop.has(key)) labelsByCrop.set(key, label);
    });

    const legend = document.getElementById("legend");
    Array.from(labelsByCrop.entries()).sort(function (a, b) {
      return a[0].localeCompare(b[0]);
    }).forEach(function (entry) {
      const item = document.createElement("div");
      item.className = "legend-item";
      item.innerHTML =
        '<span class="swatch" style="background:' + colorForCrop(entry[1]) + '"></span>' +
        "<span>" + escapeHtml(entry[1]) + "</span>";
      legend.appendChild(item);
    });
  }

  document.getElementById("fit-all").addEventListener("click", function () {
    if (allBounds.isValid()) map.fitBounds(allBounds.pad(0.12));
    setActiveField("");
  });

  let fieldsVisible = true;
  const toggleButton = document.getElementById("toggle-fields");
  toggleButton.addEventListener("click", function () {
    if (fieldsVisible) {
      map.removeLayer(fieldsLayer);
      toggleButton.textContent = "Show fields";
    } else {
      map.addLayer(fieldsLayer);
      toggleButton.textContent = "Hide fields";
    }
    fieldsVisible = !fieldsVisible;
  });

  addFieldList();
  addLegend();
  if (allBounds.isValid()) map.fitBounds(allBounds.pad(0.12));
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
