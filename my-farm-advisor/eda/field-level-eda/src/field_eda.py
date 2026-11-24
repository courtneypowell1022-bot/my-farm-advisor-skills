#!/usr/bin/env python3
"""Field-level EDA across boundaries, CDL/cropland, and weather.

Produces static PNG maps, statistical figures, CSV tables, and a provenance
manifest for every grower under the data-pipeline runtime. It never writes
back into `growers/` and never emits a narrative report: the one-time report is
assembled later from these artifacts.

Run with the pipeline virtualenv so geopandas, scipy, and matplotlib resolve:

    "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
      my-farm-advisor/eda/field-level-eda/src/field_eda.py \
      --all-growers --check-inputs
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    from matplotlib.patches import Patch, Rectangle
    from scipy import stats
except ImportError as exc:  # pragma: no cover - environment guard
    sys.exit(
        f"Missing analysis dependency ({exc}). Run with the pipeline virtualenv: "
        "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"
    )

SKILL_VERSION = "1.0.0"
AREA_CRS = "EPSG:5070"
ACRES_PER_M2 = 4046.8564224
CORN = "Corn"
SOY = "Soybeans"
OTHER = "Other"
CROP_GROUP_ORDER = [CORN, SOY, OTHER]
CROP_COLORS = {CORN: "#e6ab02", SOY: "#66a61e", OTHER: "#9e9e9e"}
STATE_NAMES = {
    "IL": "Illinois",
    "IA": "Iowa",
    "NE": "Nebraska",
    "MN": "Minnesota",
    "IN": "Indiana",
    "MO": "Missouri",
    "KS": "Kansas",
    "SD": "South Dakota",
    "ND": "North Dakota",
    "WI": "Wisconsin",
    "OH": "Ohio",
    "MI": "Michigan",
}
PALETTE = ["#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#17becf"]

BOUNDARY_COLUMNS = {"field_id", "geometry"}
WEATHER_COLUMNS = {
    "field_id",
    "date",
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
    "RH2M",
    "WS10M",
}
CDL_COLUMNS = {"field_id", "year", "crop_code", "crop_name", "pixel_count", "pct"}

GDD_BASE_C = 10.0
GDD_CAP_C = 30.0
HOT_DAY_C = 35.0
DRY_DAY_MM = 1.0
WET_DAY_MM = 1.0


# ---------------------------------------------------------------------------
# paths and discovery
# ---------------------------------------------------------------------------


def resolve_data_root() -> Path:
    raw = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if raw:
        path = Path(raw).expanduser()
        return path if path.name == "data-pipeline" else path / "data-pipeline"
    return Path.home() / "my-farm-advisor-runtime" / "data-pipeline"


def parse_year_range(raw: str) -> tuple[int, int]:
    parts = str(raw).replace(":", "-").split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected START-END, for example 2021-2025")
    start, end = int(parts[0]), int(parts[1])
    if start > end:
        raise argparse.ArgumentTypeError("start year must be <= end year")
    return start, end


def parse_month_range(raw: str) -> tuple[int, int]:
    parts = str(raw).replace(":", "-").split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected START-END, for example 4-10")
    start, end = int(parts[0]), int(parts[1])
    if not (1 <= start <= end <= 12):
        raise argparse.ArgumentTypeError("months must satisfy 1 <= start <= end <= 12")
    return start, end


def crop_group(name: object) -> str:
    text = str(name).strip().lower()
    if text == "corn":
        return CORN
    if text in {"soybeans", "soybean"}:
        return SOY
    return OTHER


def grower_label(grower: dict) -> str:
    state = str(grower.get("state", "")).upper()
    if state in STATE_NAMES:
        return STATE_NAMES[state]
    return str(grower.get("grower_name") or grower["grower_slug"])


def discover_growers(
    growers_root: Path, selected: list[str] | None
) -> list[dict]:
    if not growers_root.is_dir():
        raise SystemExit(f"Growers root not found: {growers_root}")
    requested = set(selected or [])
    found: list[dict] = []
    for directory in sorted(growers_root.iterdir()):
        if not directory.is_dir() or directory.name.startswith(("_", ".")):
            continue
        grower_file = directory / "grower.json"
        if not grower_file.exists():
            continue
        if requested and directory.name not in requested:
            continue
        meta = _read_json(grower_file)
        found.append(
            {
                "grower_slug": directory.name,
                "grower_name": meta.get("display_name", directory.name),
                "grower_dir": directory,
                "grower_meta": meta,
            }
        )
    missing = requested - {g["grower_slug"] for g in found}
    if missing:
        raise SystemExit(
            "Requested grower(s) not found under "
            f"{growers_root}: {', '.join(sorted(missing))}"
        )
    if not found:
        raise SystemExit(f"No growers with grower.json under {growers_root}")
    return found


def discover_farms(grower: dict) -> list[dict]:
    farms: list[dict] = []
    farms_root = grower["grower_dir"] / "farms"
    if not farms_root.is_dir():
        raise SystemExit(f"Farms directory missing for {grower['grower_slug']}")
    for directory in sorted(farms_root.iterdir()):
        farm_file = directory / "farm.json"
        if not directory.is_dir() or not farm_file.exists():
            continue
        meta = _read_json(farm_file)
        farms.append(
            {
                "grower_slug": grower["grower_slug"],
                "grower_name": grower["grower_name"],
                "farm_slug": directory.name,
                "farm_name": meta.get("display_name", directory.name),
                "state": str(meta.get("state", "")).upper(),
                "farm_dir": directory,
                "farm_meta": meta,
            }
        )
    if not farms:
        raise SystemExit(f"No farms with farm.json for {grower['grower_slug']}")
    return farms


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read JSON metadata {path}: {exc}") from exc


def _single_table(farm_dir: Path, patterns: list[str], label: str) -> Path:
    tables = farm_dir / "derived" / "tables"
    for pattern in patterns:
        hits = sorted(tables.glob(pattern))
        if hits:
            return hits[0]
    raise SystemExit(f"Missing {label} for {farm_dir} (looked for {patterns})")


def _table_for_year(farm_dir: Path, year: int) -> Path:
    return _single_table(
        farm_dir,
        [f"*_{year}_cdl.csv"],
        f"CDL {year} table",
    )


def load_boundaries(farm: dict) -> gpd.GeoDataFrame:
    path = farm["farm_dir"] / "boundary" / "field_boundaries.geojson"
    if path.exists():
        frame = gpd.read_file(path)
    else:
        per_field = sorted(
            (farm["farm_dir"] / "fields").glob("*/boundary/field_boundary.geojson")
        )
        if not per_field:
            raise SystemExit(
                f"No boundary file for {farm['grower_slug']}/{farm['farm_slug']}"
            )
        frame = gpd.GeoDataFrame(
            pd.concat([gpd.read_file(item) for item in per_field], ignore_index=True),
            crs=gpd.read_file(per_field[0]).crs,
        )
    missing = BOUNDARY_COLUMNS - set(frame.columns)
    if missing:
        raise SystemExit(
            f"Boundary missing columns {sorted(missing)}: "
            f"{farm['grower_slug']}/{farm['farm_slug']}"
        )
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    frame = frame.to_crs(AREA_CRS)
    frame["grower_slug"] = farm["grower_slug"]
    frame["farm_slug"] = farm["farm_slug"]
    return frame


def load_weather(
    farm: dict, years: tuple[int, int]
) -> tuple[pd.DataFrame, Path]:
    start, end = years
    path = _single_table(
        farm["farm_dir"],
        [f"*_weather_{start}_{end}.csv", "*_weather_*.csv"],
        f"weather {start}-{end} table",
    )
    frame = pd.read_csv(path)
    missing = WEATHER_COLUMNS - set(frame.columns)
    if missing:
        raise SystemExit(f"Weather missing columns {sorted(missing)}: {path}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "field_id"]).copy()
    frame["year"] = frame["date"].dt.year
    frame = frame[frame["year"].between(start, end)]
    if frame.empty:
        raise SystemExit(f"Weather has no rows for {start}-{end}: {path}")
    frame["grower_slug"] = farm["grower_slug"]
    frame["farm_slug"] = farm["farm_slug"]
    return frame, path


def load_cdl(
    farm: dict, years: tuple[int, int]
) -> tuple[pd.DataFrame, list[Path]]:
    frames: list[pd.DataFrame] = []
    paths: list[Path] = []
    for year in range(years[0], years[1] + 1):
        path = _table_for_year(farm["farm_dir"], year)
        frame = pd.read_csv(path)
        missing = CDL_COLUMNS - set(frame.columns)
        if missing:
            raise SystemExit(f"CDL missing columns {sorted(missing)}: {path}")
        frame["grower_slug"] = farm["grower_slug"]
        frame["farm_slug"] = farm["farm_slug"]
        frames.append(frame)
        paths.append(path)
    combined = pd.concat(frames, ignore_index=True)
    combined["crop_name"] = combined["crop_name"].fillna("Unknown").astype(str)
    return combined, paths


def load_rotation(farm: dict) -> tuple[pd.DataFrame, Path]:
    path = _single_table(farm["farm_dir"], ["*_crop_rotation.csv"], "crop rotation table")
    frame = pd.read_csv(path)
    if "field_id" not in frame.columns:
        raise SystemExit(f"Crop rotation table missing field_id: {path}")
    frame["grower_slug"] = farm["grower_slug"]
    frame["farm_slug"] = farm["farm_slug"]
    return frame, path


# ---------------------------------------------------------------------------
# derived metrics
# ---------------------------------------------------------------------------


def derive_geometry(boundary: gpd.GeoDataFrame) -> pd.DataFrame:
    frame = boundary.copy()
    area = frame.geometry.area
    perimeter = frame.geometry.length
    centroids = frame.geometry.centroid
    lonlat = centroids.to_crs("EPSG:4326")
    frame["calc_area_m2"] = area
    frame["area_acres"] = area / ACRES_PER_M2
    frame["perimeter_m"] = perimeter
    frame["compactness"] = 4.0 * np.pi * area / (perimeter**2)
    frame["centroid_x"] = centroids.x
    frame["centroid_y"] = centroids.y
    frame["centroid_lon"] = lonlat.x
    frame["centroid_lat"] = lonlat.y
    frame["boundary_crop"] = frame.get("crop", pd.Series(index=frame.index)).astype(
        "object"
    )
    frame["state"] = frame.get("state", pd.Series(index=frame.index)).astype("object")
    frame["county"] = frame.get("county", pd.Series(index=frame.index)).astype("object")
    return pd.DataFrame(frame.drop(columns=["geometry"]))


def _max_dry_run(values: pd.Series, threshold: float = DRY_DAY_MM) -> int:
    best = current = 0
    for value in values.to_numpy():
        if value < threshold:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return int(best)


def derive_weather(
    weather: pd.DataFrame, months: tuple[int, int]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start_month, end_month = months
    frame = weather.copy()
    frame = frame[frame["date"].dt.month.between(start_month, end_month)]
    frame["gdd"] = (
        (
            (
                frame["T2M_MAX"].clip(upper=GDD_CAP_C)
                + frame["T2M_MIN"].clip(lower=GDD_BASE_C)
            )
            / 2.0
        )
        - GDD_BASE_C
    ).clip(lower=0.0)
    frame = frame.sort_values(["grower_slug", "field_id", "date"])

    records: list[dict] = []
    for (grower, farm, field, year), group in frame.groupby(
        ["grower_slug", "farm_slug", "field_id", "year"], sort=True
    ):
        records.append(
            {
                "grower_slug": grower,
                "farm_slug": farm,
                "field_id": field,
                "year": int(year),
                "gdd": float(group["gdd"].sum()),
                "precip_mm": float(group["PRECTOTCORR"].sum()),
                "hot35_days": int((group["T2M_MAX"] >= HOT_DAY_C).sum()),
                "wet_days": int((group["PRECTOTCORR"] >= WET_DAY_MM).sum()),
                "max_dry_spell_days": _max_dry_run(group["PRECTOTCORR"]),
                "mean_temp_c": float(group["T2M"].mean()),
            }
        )
    field_year = pd.DataFrame(records)

    field_summary = (
        field_year.groupby(["grower_slug", "farm_slug", "field_id"], as_index=False)
        .agg(
            gdd_mean=("gdd", "mean"),
            precip_mean=("precip_mm", "mean"),
            hot35_mean=("hot35_days", "mean"),
            wet_days_mean=("wet_days", "mean"),
            precip_cv=(
                "precip_mm",
                lambda values: (
                    100.0 * values.std(ddof=1) / values.mean()
                    if len(values) > 1 and values.mean()
                    else np.nan
                ),
            ),
        )
        .reset_index(drop=True)
    )
    return field_year, field_summary


def derive_cdl(cdl: pd.DataFrame, rotation: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = cdl.copy()
    frame["crop_group"] = frame["crop_name"].map(crop_group)

    dominant = (
        frame.sort_values("pct")
        .groupby(["grower_slug", "farm_slug", "field_id", "year"], as_index=False)
        .tail(1)[
            [
                "grower_slug",
                "farm_slug",
                "field_id",
                "year",
                "crop_name",
                "crop_group",
                "pct",
            ]
        ]
        .rename(columns={"crop_name": "dominant_crop", "pct": "dominant_pct"})
    )
    diversity = (
        frame.groupby(["grower_slug", "farm_slug", "field_id", "year"], as_index=False)
        .agg(n_crops=("crop_name", "nunique"), total_pixels=("pixel_count", "sum"))
    )
    dominant = dominant.merge(
        diversity, on=["grower_slug", "farm_slug", "field_id", "year"], how="left"
    )
    dominant["edge_pct"] = (100.0 - dominant["dominant_pct"]).clip(lower=0.0)

    wide = dominant.pivot(
        index=["grower_slug", "farm_slug", "field_id"],
        columns="year",
        values="dominant_crop",
    )
    wide.columns = [f"crop_{int(year)}" for year in wide.columns]
    wide = wide.reset_index()

    pct_wide = dominant.pivot(
        index=["grower_slug", "farm_slug", "field_id"],
        columns="year",
        values="dominant_pct",
    )
    pct_wide.columns = [f"dominant_pct_{int(year)}" for year in pct_wide.columns]
    pct_wide = pct_wide.reset_index()

    edge_wide = dominant.pivot(
        index=["grower_slug", "farm_slug", "field_id"],
        columns="year",
        values="edge_pct",
    )
    edge_wide.columns = [f"edge_pct_{int(year)}" for year in edge_wide.columns]
    edge_wide = edge_wide.reset_index()

    rotation_cols = [
        column
        for column in (
            "field_id",
            "grower_slug",
            "farm_slug",
            "rotation_sequence",
            "rotation_count",
            "crop_diversity",
            "corn_years",
            "soybean_years",
            "rotation_confidence",
        )
        if column in rotation.columns
    ]
    rotation_keep = rotation[rotation_cols].drop_duplicates(
        subset=["grower_slug", "field_id"]
    )

    combined = functools.reduce(
        lambda left, right: left.merge(
            right, on=["grower_slug", "farm_slug", "field_id"], how="left"
        ),
        [wide, pct_wide, edge_wide, rotation_keep],
    )
    return dominant, combined


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def bh_adjust(p_values: list[float]) -> list[float]:
    values = np.asarray(p_values, dtype=float)
    if values.size == 0:
        return []
    order = np.argsort(values)
    ranked = values[order]
    adjusted = ranked * values.size / np.arange(1, values.size + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result = np.empty(values.size)
    result[order] = np.clip(adjusted, 0.0, 1.0)
    return result.tolist()


def kruskal_summary(
    frame: pd.DataFrame, value: str, group: str
) -> tuple[float, float, float, int, int] | None:
    groups = [g[value].dropna().to_numpy() for _, g in frame.groupby(group)]
    groups = [g for g in groups if len(g) > 0]
    if len(groups) < 2:
        return None
    try:
        statistic, p_value = stats.kruskal(*groups)
    except ValueError:
        return None
    total = sum(len(g) for g in groups)
    k = len(groups)
    if total > k:
        # Epsilon-squared can dip below zero for a weak statistic; clamp to its
        # valid [0, 1] range so the annotation never reports a negative effect.
        epsilon_sq = float(np.clip((statistic - k + 1) / (total - k), 0.0, 1.0))
    else:
        epsilon_sq = float("nan")
    return statistic, p_value, epsilon_sq, total, k


def mann_whitney_row(
    left: pd.Series,
    right: pd.Series,
    *,
    category: str,
    comparison: str,
    group_a: str,
    group_b: str,
    metric: str,
) -> dict | None:
    left = left.dropna()
    right = right.dropna()
    if len(left) < 2 or len(right) < 2:
        return None
    try:
        U, p_value = stats.mannwhitneyu(left, right, alternative="two-sided")
    except ValueError:
        return None
    rank_biserial = 2.0 * U / (len(left) * len(right)) - 1.0
    return {
        "category": category,
        "comparison": comparison,
        "test": "mann_whitney_u",
        "metric": metric,
        "group_a": group_a,
        "group_b": group_b,
        "n": f"{len(left)} vs {len(right)}",
        "statistic": float(U),
        "p_value": float(p_value),
        "p_bh": None,
        "effect_size": float(rank_biserial),
        "effect_size_name": "rank_biserial",
        "note": "Rank-biserial positive means group_a is higher.",
    }


def spearman_row(
    left: pd.Series,
    right: pd.Series,
    *,
    category: str,
    comparison: str,
    group_a: str,
    metric: str,
    note: str = "",
) -> dict | None:
    mask = left.notna() & right.notna()
    if mask.sum() < 3:
        return None
    rho, p_value = stats.spearmanr(left[mask], right[mask])
    return {
        "category": category,
        "comparison": comparison,
        "test": "spearman_rho",
        "metric": metric,
        "group_a": group_a,
        "group_b": "",
        "n": int(mask.sum()),
        "statistic": float(rho),
        "p_value": float(p_value),
        "p_bh": None,
        "effect_size": float(rho),
        "effect_size_name": "spearman_rho",
        "note": note,
    }


def apply_bh(rows: list[dict]) -> list[dict]:
    usable = [row for row in rows if row.get("p_value") is not None]
    adjusted = bh_adjust([row["p_value"] for row in usable])
    for row, value in zip(usable, adjusted):
        row["p_bh"] = value
    return rows


# ---------------------------------------------------------------------------
# plotting helpers
# ---------------------------------------------------------------------------


def style_axes() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 150,
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.labelsize": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
        }
    )


def footer(fig, text: str, *, gap: float = 0.04) -> None:
    # Draw below the figure box so `bbox_inches="tight"` grows the canvas instead
    # of letting the note collide with x-axis labels.
    fig.text(0.5, -gap, text, ha="center", va="top", fontsize=7.5, color="#555555")


def annotate(ax, text: str, *, location: str = "upper right") -> None:
    positions = {
        "upper right": (0.98, 0.98, "right", "top"),
        "upper left": (0.02, 0.98, "left", "top"),
        "lower right": (0.98, 0.02, "right", "bottom"),
    }
    x, y, ha, va = positions[location]
    ax.text(
        x,
        y,
        text,
        transform=ax.transAxes,
        ha=ha,
        va=va,
        fontsize=8,
        bbox=dict(boxstyle="round,pad=0.35", facecolor="white", edgecolor="#cccccc", alpha=0.9),
    )


def _scale_bar(
    ax, bounds: tuple[float, float, float, float], *, side: str = "left"
) -> None:
    xmin, ymin, xmax, ymax = bounds
    width = xmax - xmin
    height = ymax - ymin
    if width <= 0 or height <= 0:
        return
    target = 0.28 * width
    length = 0.15 * width
    label = f"{length / 1000:.1f} km"
    for candidate_km in (0.5, 1, 2, 5, 10, 20, 50, 100):
        if candidate_km * 1000 <= target:
            length = candidate_km * 1000
            label = f"{candidate_km:g} km"
    if side == "right":
        x0 = xmax - 0.05 * width - length
    else:
        x0 = xmin + 0.05 * width
    y0 = ymin + 0.06 * height
    ax.plot(
        [x0, x0 + length],
        [y0, y0],
        color="#222222",
        linewidth=2.0,
        solid_capstyle="butt",
        zorder=7,
    )
    ax.text(
        x0 + length / 2,
        y0 + 0.02 * height,
        label,
        ha="center",
        va="bottom",
        fontsize=7,
        zorder=7,
    )


def _draw_fields(
    ax,
    gdf: gpd.GeoDataFrame,
    *,
    column: str | None = None,
    cmap=None,
    norm=None,
    color_map: dict | None = None,
    linewidth: float = 0.8,
    markers: bool = False,
) -> None:
    if color_map is not None:
        colors = [color_map.get(value, "#cccccc") for value in gdf[column]]
        gdf.plot(ax=ax, color=colors, edgecolor="white", linewidth=linewidth)
    elif column is not None:
        gdf.plot(
            ax=ax,
            column=column,
            cmap=cmap,
            norm=norm,
            edgecolor="white",
            linewidth=linewidth,
            missing_kwds={"color": "#dddddd"},
        )
    else:
        gdf.plot(ax=ax, color="#8fb8d8", edgecolor="white", linewidth=linewidth)
    if not markers:
        return
    # A centroid dot keeps each parcel findable in the true-scale overview,
    # where a 1-3 km field is only a few pixels wide.
    points = gdf.geometry.representative_point()
    if color_map is not None:
        colors = [color_map.get(value, "#cccccc") for value in gdf[column]]
        ax.scatter(
            points.x,
            points.y,
            s=16,
            color=colors,
            edgecolor="#333333",
            linewidth=0.6,
            zorder=5,
        )
    elif column is not None:
        ax.scatter(
            points.x,
            points.y,
            s=16,
            c=gdf[column],
            cmap=cmap,
            norm=norm,
            edgecolor="#333333",
            linewidth=0.6,
            zorder=5,
        )
    else:
        ax.scatter(
            points.x,
            points.y,
            s=16,
            color="#1f77b4",
            edgecolor="#333333",
            linewidth=0.6,
            zorder=5,
        )


def _detail_window(
    frame: gpd.GeoDataFrame, side_m: float = 8_000.0
) -> tuple[float, float, float, float]:
    """Square window centred on the field with the most near neighbours."""
    points = frame.geometry.representative_point()
    x = points.x.to_numpy(dtype=float)
    y = points.y.to_numpy(dtype=float)
    half = side_m / 2.0
    distances = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    counts = (distances <= half).sum(axis=1)
    index = int(np.argmax(counts))
    centre_x, centre_y = float(x[index]), float(y[index])
    return (centre_x - half, centre_y - half, centre_x + half, centre_y + half)


def _inset_bounds(
    frame: gpd.GeoDataFrame, window: tuple[float, float, float, float]
) -> list[float]:
    """Place the detail inset in the emptiest corner that avoids the window."""
    xmin, ymin, xmax, ymax = frame.total_bounds
    span_x = max(xmax - xmin, 1.0)
    span_y = max(ymax - ymin, 1.0)
    points = frame.geometry.representative_point()
    points_x = (points.x.to_numpy() - xmin) / span_x
    points_y = (points.y.to_numpy() - ymin) / span_y

    window_rect = (
        (window[0] - xmin) / span_x,
        (window[1] - ymin) / span_y,
        (window[2] - xmin) / span_x,
        (window[3] - ymin) / span_y,
    )

    size = 0.36
    corners = {
        "lower left": (0.02, 0.04),
        "lower right": (0.62, 0.04),
        "upper left": (0.02, 0.62),
        "upper right": (0.62, 0.62),
    }
    best_name = "lower left"
    best_score = float("inf")
    for name, (x0, y0) in corners.items():
        rect = (x0, y0, x0 + size, y0 + size)
        if _rects_overlap(rect, window_rect):
            continue
        covered = int(
            (
                (points_x >= rect[0])
                & (points_x <= rect[2])
                & (points_y >= rect[1])
                & (points_y <= rect[3])
            ).sum()
        )
        if covered < best_score:
            best_score = covered
            best_name = name
    x0, y0 = corners[best_name]
    return [x0, y0, size, size]


def _rects_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _plot_overview(
    ax,
    frame: gpd.GeoDataFrame,
    *,
    window: tuple[float, float, float, float],
    title: str,
    subtitle: str,
    column: str | None = None,
    cmap=None,
    norm=None,
    color_map: dict | None = None,
    scale_side: str = "left",
) -> None:
    ax.set_aspect("equal")
    ax.set_axis_off()
    _draw_fields(
        ax,
        frame,
        column=column,
        cmap=cmap,
        norm=norm,
        color_map=color_map,
        linewidth=0.7,
        markers=True,
    )
    ax.add_patch(
        Rectangle(
            (window[0], window[1]),
            window[2] - window[0],
            window[3] - window[1],
            fill=False,
            edgecolor="#333333",
            linewidth=1.0,
            linestyle=(0, (4, 3)),
            zorder=6,
        )
    )
    xmin, ymin, xmax, ymax = frame.total_bounds
    pad_x = 0.06 * (xmax - xmin)
    pad_y = 0.06 * (ymax - ymin)
    ax.set_xlim(xmin - pad_x, xmax + pad_x)
    ax.set_ylim(ymin - pad_y, ymax + pad_y)
    ax.set_title(title, fontsize=10.5, fontweight="bold", pad=14)
    ax.text(
        0.5,
        1.01,
        subtitle,
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=7.5,
        color="#555555",
    )
    _scale_bar(ax, (xmin, ymin, xmax, ymax), side=scale_side)


def _plot_detail(
    ax,
    frame: gpd.GeoDataFrame,
    *,
    window: tuple[float, float, float, float],
    column: str | None = None,
    cmap=None,
    norm=None,
    color_map: dict | None = None,
) -> None:
    ax.set_aspect("equal")
    ax.set_facecolor("#fbfbfb")
    _draw_fields(
        ax,
        frame,
        column=column,
        cmap=cmap,
        norm=norm,
        color_map=color_map,
        linewidth=0.8,
    )
    ax.set_xlim(window[0], window[2])
    ax.set_ylim(window[1], window[3])
    x0, y0, x1, y1 = window
    width = x1 - x0
    height = y1 - y0
    label_w = 0.18 * width
    label_h = 0.07 * height
    candidates = [
        (0.0, 0.0),
        (0.0, 1.8 * label_h),
        (0.0, -1.8 * label_h),
        (1.2 * label_w, 0.0),
        (-1.2 * label_w, 0.0),
        (1.2 * label_w, 1.8 * label_h),
        (-1.2 * label_w, 1.8 * label_h),
        (1.2 * label_w, -1.8 * label_h),
        (-1.2 * label_w, -1.8 * label_h),
    ]
    placed: list[tuple[float, float, float, float]] = []
    for _, row in frame.iterrows():
        point = row.geometry.representative_point()
        if not (x0 <= point.x <= x1 and y0 <= point.y <= y1):
            continue
        chosen: tuple[float, float] | None = None
        chosen_rect: tuple[float, float, float, float] | None = None
        for dx, dy in candidates:
            centre_x = min(
                max(point.x + dx, x0 + label_w / 2), x1 - label_w / 2
            )
            centre_y = min(
                max(point.y + dy, y0 + label_h / 2), y1 - label_h / 2
            )
            rect = (
                centre_x - label_w / 2,
                centre_y - label_h / 2,
                centre_x + label_w / 2,
                centre_y + label_h / 2,
            )
            if not any(_rects_overlap(rect, other) for other in placed):
                chosen = (centre_x - point.x, centre_y - point.y)
                chosen_rect = rect
                break
        if chosen is None:
            centre_x = min(
                max(point.x + candidates[-1][0], x0 + label_w / 2),
                x1 - label_w / 2,
            )
            centre_y = min(
                max(point.y + candidates[-1][1], y0 + label_h / 2),
                y1 - label_h / 2,
            )
            chosen = (centre_x - point.x, centre_y - point.y)
            chosen_rect = (
                centre_x - label_w / 2,
                centre_y - label_h / 2,
                centre_x + label_w / 2,
                centre_y + label_h / 2,
            )
        placed.append(chosen_rect)
        centre_x = point.x + chosen[0]
        centre_y = point.y + chosen[1]
        leader = None
        if chosen != (0.0, 0.0):
            leader = dict(
                arrowstyle="-",
                color="#888888",
                linewidth=0.5,
                shrinkA=0,
                shrinkB=1,
            )
        ax.annotate(
            str(row["field_id"]),
            xy=(point.x, point.y),
            xytext=(centre_x, centre_y),
            textcoords="data",
            fontsize=6,
            ha="center",
            va="center",
            color="#202020",
            bbox=dict(
                boxstyle="round,pad=0.12",
                facecolor="white",
                edgecolor="none",
                alpha=0.8,
            ),
            arrowprops=leader,
            zorder=6,
        )
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_ylabel("")
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color("#666666")
        spine.set_linewidth(0.8)
    _scale_bar(ax, window)


def _render_map(
    farms: list[tuple[dict, gpd.GeoDataFrame]],
    *,
    out_path: Path,
    suptitle: str,
    title_for,
    subtitle: str,
    column: str,
    cmap=None,
    norm=None,
    color_map: dict | None = None,
    colorbar_label: str | None = None,
    legend_handles: list | None = None,
    footer_text: str | None = None,
) -> Path:
    fig, axes = plt.subplots(
        1, len(farms), figsize=(18, 7.0), constrained_layout=True
    )
    axes = np.atleast_1d(axes)
    for ax, (farm, frame) in zip(axes, farms):
        grower = farm["grower_slug"]
        window = _detail_window(frame)
        bounds = _inset_bounds(frame, window)
        # Keep the overview scale bar away from the inset's corner.
        scale_side = "right" if bounds[0] < 0.5 else "left"
        _plot_overview(
            ax,
            frame,
            window=window,
            title=title_for(grower, frame),
            subtitle=subtitle,
            column=column,
            cmap=cmap,
            norm=norm,
            color_map=color_map,
            scale_side=scale_side,
        )
        inset = ax.inset_axes(bounds)
        _plot_detail(
            inset,
            frame,
            window=window,
            column=column,
            cmap=cmap,
            norm=norm,
            color_map=color_map,
        )
    if colorbar_label is not None:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        colorbar = fig.colorbar(
            sm,
            ax=list(axes),
            orientation="horizontal",
            fraction=0.04,
            pad=0.03,
            aspect=45,
        )
        colorbar.set_label(colorbar_label)
    if legend_handles:
        fig.legend(
            handles=legend_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.005),
            ncol=len(legend_handles),
            frameon=False,
            fontsize=8,
        )
    fig.suptitle(suptitle, fontsize=13, fontweight="bold")
    if footer_text:
        footer(fig, footer_text, gap=0.085 if colorbar_label or legend_handles else 0.04)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path




# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------


def figure_boundaries(fields: pd.DataFrame, labels: dict, colors: dict, metrics: dict, out: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.6), constrained_layout=True)
    growers = list(labels)
    specs = [
        ("area_acres", "Field size (acres)", metrics.get("area"), "upper right"),
        ("compactness", "Shape compactness (Polsby–Popper)", metrics.get("compactness"), "upper left"),
    ]
    rng = np.random.default_rng(7)
    for ax, (column, title, stat, position) in zip(axes, specs):
        values = [fields.loc[fields["grower_slug"] == g, column].dropna().to_numpy() for g in growers]
        box = ax.boxplot(values, widths=0.5, patch_artist=True, showfliers=False)
        for patch, grower in zip(box["boxes"], growers):
            patch.set_facecolor(colors[grower])
            patch.set_edgecolor(colors[grower])
            patch.set_alpha(0.30)
        for index, (grower, series) in enumerate(zip(growers, values), start=1):
            jitter = rng.normal(index, 0.055, len(series))
            ax.scatter(
                jitter,
                series,
                s=24,
                color=colors[grower],
                edgecolor="white",
                linewidth=0.4,
                alpha=0.9,
                zorder=3,
            )
        ax.set_xticks(range(1, len(growers) + 1))
        ax.set_xticklabels([labels[g] for g in growers])
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.25)
        if stat:
            annotate(
                ax,
                f"Kruskal–Wallis p={stat['p']:.2g}\nε²={stat['eps']:.2f} (n={stat['n']})",
                location=position,
            )
    fig.suptitle("Field boundaries: size and shape by grower", fontsize=13, fontweight="bold")
    footer(
        fig,
        "Each dot is one field. Box shows the interquartile range. Compactness: 1.0 = circle; lower = more irregular.",
    )
    path = out / "figures" / "b01_area_compactness_by_grower.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def figure_cdl(
    fields: pd.DataFrame,
    cdl: pd.DataFrame,
    labels: dict,
    colors: dict,
    metrics: dict,
    out: Path,
) -> Path:
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(15, 8.2),
        constrained_layout=True,
        gridspec_kw={"width_ratios": [1.45, 1.0]},
    )

    composition = (
        cdl[cdl["year"] == cdl["year"].max()]
        .assign(group=lambda frame: frame["crop_name"].map(crop_group))
        .groupby(["grower_slug", "field_id", "group"], as_index=False)["pct"]
        .sum()
        .pivot(index=["grower_slug", "field_id"], columns="group", values="pct")
        .fillna(0.0)
        .reset_index()
    )
    for group in CROP_GROUP_ORDER:
        if group not in composition.columns:
            composition[group] = 0.0

    ax = axes[0]
    y = 0.0
    ticks: list[float] = []
    tick_labels: list[str] = []
    group_centers: list[tuple[float, str]] = []
    for grower in labels:
        subset = composition[composition["grower_slug"] == grower].copy()
        subset = subset.sort_values(CORN)
        start = y
        for _, row in subset.iterrows():
            left = 0.0
            for group in CROP_GROUP_ORDER:
                value = float(row[group])
                if value <= 0:
                    continue
                ax.barh(
                    y,
                    value,
                    left=left,
                    height=0.8,
                    color=CROP_COLORS[group],
                    edgecolor="white",
                    linewidth=0.4,
                )
                left += value
            ticks.append(y)
            tick_labels.append(str(row["field_id"]))
            y += 1.0
        group_centers.append(((start + y - 1.0) / 2.0, labels[grower]))
        y += 1.0
    ax.set_yticks(ticks)
    ax.set_yticklabels(tick_labels, fontsize=6)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of CDL pixels (%)")
    ax.set_title(f"Per-field composition, {int(cdl['year'].max())}")
    for center, label in group_centers:
        ax.text(
            -0.015,
            center,
            label,
            transform=ax.get_yaxis_transform(),
            ha="right",
            va="center",
            fontsize=8,
            fontweight="bold",
            color="#333333",
            clip_on=False,
        )
    ax.grid(axis="x", alpha=0.25)

    ax = axes[1]
    yearly = cdl.copy()
    yearly["is_corn"] = yearly["crop_name"].eq(CORN)
    share = (
        yearly.groupby(["grower_slug", "year"])
        .apply(
            lambda frame: 100.0 * frame.loc[frame["is_corn"], "pixel_count"].sum()
            / max(frame["pixel_count"].sum(), 1),
            include_groups=False,
        )
        .rename("corn_share")
        .reset_index()
    )
    for grower in labels:
        subset = share[share["grower_slug"] == grower].sort_values("year")
        ax.plot(
            subset["year"],
            subset["corn_share"],
            marker="o",
            linewidth=2.0,
            color=colors[grower],
            label=labels[grower],
        )
    ax.set_ylim(0, 100)
    ax.set_xticks(sorted(share["year"].unique()))
    ax.set_xlabel("CDL year")
    ax.set_ylabel("Corn share of pixels (%)")
    ax.set_title("Corn share by year")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    if metrics.get("agreement"):
        annotate(
            ax,
            "Boundary crop = 2023 CDL dominant\n"
            f"{metrics['agreement']['match']}/{metrics['agreement']['total']} fields",
            location="lower right",
        )

    # The crop-class key belongs to panel A, but panel A has no empty space.
    # Panel B's lower-left corner is clear (all series sit above ~30%), so the
    # key sits there with an explicit title rather than as a crowded fig legend.
    crop_handles = [Patch(facecolor=CROP_COLORS[g], label=g) for g in CROP_GROUP_ORDER]
    ax.legend(
        handles=crop_handles,
        title="CDL class (panel A)",
        loc="lower left",
        frameon=False,
        fontsize=7,
        title_fontsize=7,
    )
    fig.suptitle("CDL / cropland: composition and rotation", fontsize=13, fontweight="bold")
    footer(fig, "Crop groups: Corn, Soybeans, and all other CDL classes (edges, grass, forest, etc.).", gap=0.05)
    path = out / "figures" / "c01_crop_composition_2019_2023.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def figure_weather(
    field_year: pd.DataFrame,
    labels: dict,
    colors: dict,
    metrics: dict,
    out: Path,
) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)
    growers = list(labels)
    years = sorted(field_year["year"].unique())
    year_cmap = plt.get_cmap("viridis")
    year_colors = {year: year_cmap(i / max(len(years) - 1, 1)) for i, year in enumerate(years)}

    ax = axes[0]
    rng = np.random.default_rng(11)
    for index, grower in enumerate(growers, start=1):
        subset = field_year[field_year["grower_slug"] == grower]
        box = ax.boxplot(
            [subset["gdd"].to_numpy()],
            positions=[index],
            widths=0.5,
            patch_artist=True,
            showfliers=False,
        )
        box["boxes"][0].set_facecolor(colors[grower])
        box["boxes"][0].set_edgecolor(colors[grower])
        box["boxes"][0].set_alpha(0.25)
        for year in years:
            values = subset.loc[subset["year"] == year, "gdd"].to_numpy()
            ax.scatter(
                rng.normal(index, 0.05, len(values)),
                values,
                s=16,
                color=year_colors[year],
                edgecolor="white",
                linewidth=0.3,
                alpha=0.9,
                zorder=3,
            )
    ax.set_xticks(range(1, len(growers) + 1))
    ax.set_xticklabels([labels[g] for g in growers])
    ax.set_ylabel("Apr–Oct GDD (°C-days)")
    ax.set_title("Growing degree days by grower")
    ax.grid(axis="y", alpha=0.25)
    stat = metrics.get("gdd")
    if stat:
        annotate(ax, f"Kruskal–Wallis p={stat['p']:.2g}\nε²={stat['eps']:.2f}", location="upper left")
    year_handles = [Patch(facecolor=year_colors[y], label=str(y)) for y in years]
    ax.legend(handles=year_handles, title="Year", fontsize=7, title_fontsize=7, frameon=False, ncol=2)

    ax = axes[1]
    for grower in growers:
        subset = field_year[field_year["grower_slug"] == grower]
        for _, field in subset.groupby("field_id"):
            field = field.sort_values("year")
            ax.plot(
                field["year"],
                field["precip_mm"],
                color=colors[grower],
                alpha=0.22,
                linewidth=0.8,
            )
        mean = subset.groupby("year")["precip_mm"].mean()
        ax.plot(
            mean.index,
            mean.to_numpy(),
            color=colors[grower],
            linewidth=2.2,
            marker="o",
            label=labels[grower],
        )
    ax.set_xticks(years)
    ax.set_xlabel("Year")
    ax.set_ylabel("Apr–Oct precipitation (mm)")
    ax.set_title("Precipitation by field and year")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, fontsize=8)
    stat = metrics.get("precip")
    if stat:
        annotate(ax, f"Kruskal–Wallis p={stat['p']:.2g}\nε²={stat['eps']:.2f}", location="upper right")

    fig.suptitle("Weather: heat and rainfall across growers", fontsize=13, fontweight="bold")
    footer(fig, "Thin lines are individual fields; thick lines are grower means. Fields on one farm share nearly identical weather.")
    path = out / "figures" / "w01_seasonal_distributions.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def figure_compactness_edge(
    fields: pd.DataFrame,
    labels: dict,
    colors: dict,
    metrics: dict,
    out: Path,
) -> Path:
    frame = fields.dropna(subset=["compactness", "edge_pct_2023"]).copy()
    frame["pp_centered"] = frame["compactness"] - frame.groupby("grower_slug")["compactness"].transform("mean")
    frame["edge_centered"] = frame["edge_pct_2023"] - frame.groupby("grower_slug")["edge_pct_2023"].transform("mean")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.8), constrained_layout=True)
    ax = axes[0]
    for grower in labels:
        subset = frame[frame["grower_slug"] == grower]
        ax.scatter(
            subset["compactness"],
            subset["edge_pct_2023"],
            s=44,
            color=colors[grower],
            edgecolor="white",
            linewidth=0.5,
            alpha=0.9,
            label=labels[grower],
        )
    if len(frame) >= 3:
        slope, intercept = np.polyfit(frame["compactness"], frame["edge_pct_2023"], 1)
        xs = np.linspace(frame["compactness"].min(), frame["compactness"].max(), 50)
        ax.plot(xs, slope * xs + intercept, color="#333333", linewidth=1.0, linestyle="--")
    ax.set_xlabel("Shape compactness (Polsby–Popper)")
    ax.set_ylabel("CDL edge share, 2023 (%)")
    ax.set_title("All fields")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False, fontsize=8)
    stat = metrics.get("edge_all")
    if stat:
        annotate(ax, f"Spearman ρ={stat['rho']:+.2f}\np={stat['p']:.3f} (n={stat['n']})", location="upper right")

    ax = axes[1]
    for grower in labels:
        subset = frame[frame["grower_slug"] == grower]
        ax.scatter(
            subset["pp_centered"],
            subset["edge_centered"],
            s=44,
            color=colors[grower],
            edgecolor="white",
            linewidth=0.5,
            alpha=0.9,
            label=labels[grower],
        )
    ax.axhline(0, color="#999999", linewidth=0.8)
    ax.axvline(0, color="#999999", linewidth=0.8)
    ax.set_xlabel("Compactness, deviation from grower mean")
    ax.set_ylabel("Edge share, deviation from grower mean (%)")
    ax.set_title("Within-grower (centered)")
    ax.grid(alpha=0.25)
    stat = metrics.get("edge_centered")
    if stat:
        annotate(ax, f"Spearman ρ={stat['rho']:+.2f}\np={stat['p']:.3f} (n={stat['n']})", location="upper right")

    fig.suptitle("Field shape vs CDL edge share", fontsize=13, fontweight="bold")
    footer(fig, "Edge share = CDL pixels not in the field's dominant crop. Centering removes the regional gradient.")
    path = out / "figures" / "x01_compactness_vs_cdl_edge.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def map_boundaries(
    boundary_by_farm: dict,
    labels: dict,
    colors: dict,
    out: Path,
) -> Path:
    farms = []
    for farm, frame in boundary_by_farm:
        enriched = frame.copy()
        area = enriched.geometry.area
        enriched["compactness"] = 4.0 * np.pi * area / (enriched.geometry.length**2)
        enriched["area_acres"] = area / ACRES_PER_M2
        farms.append((farm, enriched))
    all_values = pd.concat([frame["compactness"] for _, frame in farms])
    norm = Normalize(vmin=float(all_values.min()), vmax=float(all_values.max()))
    return _render_map(
        farms,
        out_path=out / "maps" / "boundaries_by_grower.png",
        suptitle="Field boundaries by grower",
        title_for=lambda grower, frame: (
            f"{labels[grower]} — {len(frame)} fields, "
            f"{frame['area_acres'].sum():,.0f} ac"
        ),
        subtitle="Shaded by shape compactness; dashed box = detail inset",
        column="compactness",
        cmap=plt.get_cmap("viridis"),
        norm=norm,
        colorbar_label="Shape compactness (1.0 = circle)",
        footer_text=(
            "Overview is true scale (fields are 1-3 km across); the dashed 8 km "
            "box is enlarged in each inset."
        ),
    )


def map_cdl(
    boundary_by_farm: dict,
    fields: pd.DataFrame,
    labels: dict,
    out: Path,
) -> Path:
    crop_lookup = fields.set_index(["grower_slug", "field_id"])["crop_2023"].to_dict()
    farms = []
    for farm, frame in boundary_by_farm:
        grower = farm["grower_slug"]
        prepared = frame.copy()
        prepared["crop_2023"] = [
            crop_lookup.get((grower, field_id), "No data")
            for field_id in prepared["field_id"]
        ]
        farms.append((farm, prepared))
    handles = [
        Patch(facecolor=CROP_COLORS[CORN], label="Corn"),
        Patch(facecolor=CROP_COLORS[SOY], label="Soybeans"),
        Patch(facecolor=CROP_COLORS[OTHER], label="Other"),
    ]
    return _render_map(
        farms,
        out_path=out / "maps" / "cdl_2023_by_grower.png",
        suptitle="CDL dominant crop, 2023",
        title_for=lambda grower, frame: f"{labels[grower]} — 2023 dominant crop",
        subtitle="CDL dominant class per field; dashed box = detail inset",
        column="crop_2023",
        color_map={
            CORN: CROP_COLORS[CORN],
            SOY: CROP_COLORS[SOY],
            OTHER: CROP_COLORS[OTHER],
            "No data": "#dddddd",
        },
        legend_handles=handles,
        footer_text=(
            "Overview is true scale; the dashed 8 km box is enlarged in each "
            "inset so field shapes and IDs are readable."
        ),
    )


def map_weather(
    boundary_by_farm: dict,
    fields: pd.DataFrame,
    labels: dict,
    out: Path,
) -> Path:
    gdd_lookup = fields.set_index(["grower_slug", "field_id"])["gdd_mean"].to_dict()
    farms = []
    for farm, frame in boundary_by_farm:
        grower = farm["grower_slug"]
        prepared = frame.copy()
        prepared["gdd_mean"] = [
            gdd_lookup.get((grower, field_id), np.nan)
            for field_id in prepared["field_id"]
        ]
        farms.append((farm, prepared))
    all_values = pd.Series(list(gdd_lookup.values())).dropna()
    norm = Normalize(vmin=float(all_values.min()), vmax=float(all_values.max()))
    return _render_map(
        farms,
        out_path=out / "maps" / "weather_gdd_by_grower.png",
        suptitle="Weather: growing degree days by field",
        title_for=lambda grower, frame: f"{labels[grower]} — 5-yr mean GDD",
        subtitle="Apr-Oct growing degree days; dashed box = detail inset",
        column="gdd_mean",
        cmap=plt.get_cmap("magma"),
        norm=norm,
        colorbar_label="Apr-Oct GDD (°C-days), shared scale",
        footer_text=(
            "Fields on one farm share gridded weather, so within-grower colour "
            "variation is small; the inset shows the densest cluster."
        ),
    )


# ---------------------------------------------------------------------------
# tables, manifest, orchestration
# ---------------------------------------------------------------------------


def build_tables(
    fields: pd.DataFrame,
    field_year: pd.DataFrame,
    dominant: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    boundary_columns = [
        "grower_slug",
        "farm_slug",
        "field_id",
        "state",
        "county",
        "area_acres",
        "perimeter_m",
        "compactness",
        "centroid_lat",
        "centroid_lon",
        "boundary_crop",
        "crop_2023",
        "dominant_pct_2023",
        "edge_pct_2023",
        "gdd_mean",
        "precip_mean",
        "agreement_2023",
    ]
    boundary_table = fields[[c for c in boundary_columns if c in fields.columns]].copy()
    boundary_table = boundary_table.rename(columns={"grower_slug": "grower"})
    boundary_table = boundary_table.sort_values(["grower", "field_id"])

    cdl_columns = [
        "grower_slug",
        "field_id",
        "boundary_crop",
        "crop_2023",
        "dominant_pct_2023",
        "edge_pct_2023",
        "rotation_sequence",
        "rotation_count",
        "crop_diversity",
        "corn_years",
        "soybean_years",
        "agreement_2023",
    ]
    for year in sorted(dominant["year"].unique()):
        cdl_columns.extend([f"crop_{int(year)}", f"dominant_pct_{int(year)}", f"edge_pct_{int(year)}"])
    cdl_columns = [c for c in dict.fromkeys(cdl_columns) if c in fields.columns]
    cdl_table = fields[cdl_columns].copy().rename(columns={"grower_slug": "grower"})
    cdl_table = cdl_table.sort_values(["grower", "field_id"])

    weather_table = field_year.merge(
        fields[["grower_slug", "field_id", "boundary_crop"]],
        on=["grower_slug", "field_id"],
        how="left",
    ).rename(columns={"grower_slug": "grower"})
    weather_table = weather_table.sort_values(["grower", "field_id", "year"])

    summary_rows = []
    for grower, subset in fields.groupby("grower_slug"):
        farm_crop = subset["boundary_crop"].value_counts()
        summary_rows.append(
            {
                "grower": grower,
                "state": subset["state"].dropna().iloc[0] if subset["state"].notna().any() else "",
                "fields": len(subset),
                "acres_total": round(float(subset["area_acres"].sum()), 1),
                "acres_mean": round(float(subset["area_acres"].mean()), 1),
                "compactness_mean": round(float(subset["compactness"].mean()), 3),
                "corn_fields": int(farm_crop.get(CORN, 0)),
                "soy_fields": int(farm_crop.get(SOY, 0)),
                "rotation_diversity_mean": round(float(subset["crop_diversity"].mean()), 2),
                "gdd_mean": round(float(subset["gdd_mean"].mean()), 1),
                "precip_mean": round(float(subset["precip_mean"].mean()), 1),
                "hot35_mean": round(float(subset["hot35_mean"].mean()), 1),
                "agreement_2023": f"{int(subset['agreement_2023'].sum())}/{len(subset)}",
            }
        )
    summary = pd.DataFrame(summary_rows).sort_values("grower")

    return {
        "grower_summary": summary,
        "boundary_comparison": boundary_table,
        "cdl_comparison": cdl_table,
        "weather_comparison": weather_table,
    }


def write_manifest(
    out: Path,
    parameters: dict,
    input_paths: list[Path],
    counts: dict,
    key_metrics: dict,
) -> Path:
    inputs = []
    for path in input_paths:
        inputs.append(
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    outputs = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            outputs.append(
                {
                    "path": str(path.relative_to(out)),
                    "bytes": path.stat().st_size,
                }
            )
    payload = {
        "skill": "field-level-eda",
        "version": SKILL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": parameters,
        "counts": counts,
        "key_metrics": key_metrics,
        "inputs": inputs,
        "outputs": outputs,
    }
    path = out / "manifest.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_metrics(
    fields: pd.DataFrame,
    growers: list[str],
) -> tuple[dict, list[dict], list[dict]]:
    metrics: dict = {}
    tests: list[dict] = []
    key: dict = {}

    area = kruskal_summary(fields, "area_acres", "grower_slug")
    if area:
        metrics["area"] = {"p": area[1], "eps": area[2], "n": area[3]}
        tests.append(
            {
                "category": "boundary",
                "comparison": "field area across growers",
                "test": "kruskal_wallis",
                "metric": "area_acres",
                "group_a": "all growers",
                "group_b": "",
                "n": area[3],
                "statistic": area[0],
                "p_value": area[1],
                "p_bh": None,
                "effect_size": area[2],
                "effect_size_name": "epsilon_squared",
                "note": "",
            }
        )
    compact = kruskal_summary(fields, "compactness", "grower_slug")
    if compact:
        metrics["compactness"] = {"p": compact[1], "eps": compact[2], "n": compact[3]}
        tests.append(
            {
                "category": "boundary",
                "comparison": "compactness across growers",
                "test": "kruskal_wallis",
                "metric": "compactness",
                "group_a": "all growers",
                "group_b": "",
                "n": compact[3],
                "statistic": compact[0],
                "p_value": compact[1],
                "p_bh": None,
                "effect_size": compact[2],
                "effect_size_name": "epsilon_squared",
                "note": "",
            }
        )
    apply_bh([row for row in tests if row["category"] == "boundary"])

    pairwise: list[dict] = []
    for metric in ("area_acres", "compactness"):
        metric_rows: list[dict] = []
        for left, right in combinations(growers, 2):
            row = mann_whitney_row(
                fields.loc[fields["grower_slug"] == left, metric],
                fields.loc[fields["grower_slug"] == right, metric],
                category="boundary",
                comparison=f"{metric} pairwise",
                group_a=left,
                group_b=right,
                metric=metric,
            )
            if row:
                metric_rows.append(row)
        # Correct within each metric family of three comparisons, not across the
        # six rows that mix two different questions.
        apply_bh(metric_rows)
        pairwise.extend(metric_rows)
    tests.extend(pairwise)

    agreement = (
        fields["boundary_crop"].astype(str).str.lower()
        == fields["crop_2023"].astype(str).str.lower()
    )
    metrics["agreement"] = {"match": int(agreement.sum()), "total": int(len(agreement))}
    tests.append(
        {
            "category": "cdl",
            "comparison": "boundary crop vs 2023 CDL dominant",
            "test": "agreement_rate",
            "metric": "crop_label",
            "group_a": "boundary crop",
            "group_b": "CDL 2023",
            "n": int(len(agreement)),
            "statistic": float(agreement.mean()),
            "p_value": None,
            "p_bh": None,
            "effect_size": float(agreement.mean()),
            "effect_size_name": "agreement_rate",
            "note": "Exact match of boundary crop property and CDL dominant class.",
        }
    )

    edge_rows: list[dict] = []
    overall = spearman_row(
        fields["compactness"],
        fields["edge_pct_2023"],
        category="cdl",
        comparison="compactness vs edge share",
        group_a="all growers",
        metric="edge_pct_2023",
    )
    if overall:
        metrics["edge_all"] = {"rho": overall["statistic"], "p": overall["p_value"], "n": overall["n"]}
        edge_rows.append(overall)
    centered = fields.copy()
    centered["pp_centered"] = centered["compactness"] - centered.groupby("grower_slug")["compactness"].transform("mean")
    centered["edge_centered"] = centered["edge_pct_2023"] - centered.groupby("grower_slug")["edge_pct_2023"].transform("mean")
    overall_centered = spearman_row(
        centered["pp_centered"],
        centered["edge_centered"],
        category="cdl",
        comparison="compactness vs edge share (centered)",
        group_a="all growers",
        metric="edge_pct_2023_centered",
        note="Grower means removed from both variables.",
    )
    if overall_centered:
        metrics["edge_centered"] = {
            "rho": overall_centered["statistic"],
            "p": overall_centered["p_value"],
            "n": overall_centered["n"],
        }
        edge_rows.append(overall_centered)
    for grower in growers:
        subset = fields[fields["grower_slug"] == grower]
        row = spearman_row(
            subset["compactness"],
            subset["edge_pct_2023"],
            category="cdl",
            comparison="compactness vs edge share (within grower)",
            group_a=grower,
            metric="edge_pct_2023",
        )
        if row:
            edge_rows.append(row)
    apply_bh(edge_rows)
    tests.extend(edge_rows)

    weather_kw = []
    for metric, label in (
        ("gdd_mean", "Apr–Oct GDD"),
        ("precip_mean", "Apr–Oct precipitation"),
        ("hot35_mean", "days >= 35C"),
    ):
        result = kruskal_summary(fields, metric, "grower_slug")
        if result:
            key[f"{metric}_kw"] = {"p": result[1], "eps": result[2], "n": result[3]}
            if metric in {"gdd_mean", "precip_mean"}:
                short = "gdd" if metric == "gdd_mean" else "precip"
                metrics[short] = {"p": result[1], "eps": result[2], "n": result[3]}
            weather_kw.append(
                {
                    "category": "weather",
                    "comparison": f"{label} across growers",
                    "test": "kruskal_wallis",
                    "metric": metric,
                    "group_a": "all growers",
                    "group_b": "",
                    "n": result[3],
                    "statistic": result[0],
                    "p_value": result[1],
                    "p_bh": None,
                    "effect_size": result[2],
                    "effect_size_name": "epsilon_squared",
                    "note": "Field-level 5-year means (n=10 per grower).",
                }
            )
    apply_bh(weather_kw)
    tests.extend(weather_kw)

    crop_rows: list[dict] = []
    for metric, label in (("gdd_mean", "GDD"), ("precip_mean", "precipitation")):
        metric_rows: list[dict] = []
        for grower in growers:
            subset = fields[fields["grower_slug"] == grower]
            row = mann_whitney_row(
                subset.loc[subset["boundary_crop"] == CORN, metric],
                subset.loc[subset["boundary_crop"] == SOY, metric],
                category="weather",
                comparison=f"{label}: corn vs soy within grower",
                group_a=f"{grower} corn",
                group_b=f"{grower} soy",
                metric=metric,
            )
            if row:
                metric_rows.append(row)
        apply_bh(metric_rows)
        crop_rows.extend(metric_rows)
    tests.extend(crop_rows)

    key["boundary_cdl_agreement"] = metrics["agreement"]
    key["compactness_edge"] = {
        "all": metrics.get("edge_all"),
        "centered": metrics.get("edge_centered"),
    }
    for grower, subset in fields.groupby("grower_slug"):
        key.setdefault("growers", {})[grower] = {
            "fields": int(len(subset)),
            "acres": round(float(subset["area_acres"].sum()), 1),
            "gdd_mean": round(float(subset["gdd_mean"].mean()), 1),
            "precip_mean": round(float(subset["precip_mean"].mean()), 1),
            "hot35_mean": round(float(subset["hot35_mean"].mean()), 1),
            "agreement": f"{int(subset['agreement_2023'].sum())}/{len(subset)}",
        }
    return metrics, tests, key


def run(args: argparse.Namespace) -> int:
    data_root = resolve_data_root()
    growers_root = Path(args.growers_root).expanduser() if args.growers_root else data_root / "growers"
    out = Path(args.out).expanduser() if args.out else data_root / "derived" / "eda" / "field-comparison" / "latest"

    growers = discover_growers(growers_root, args.grower)
    all_farms = [farm for grower in growers for farm in discover_farms(grower)]

    input_paths: list[Path] = []
    for grower in growers:
        input_paths.append(grower["grower_dir"] / "grower.json")
    for farm in all_farms:
        input_paths.append(farm["farm_dir"] / "farm.json")

    boundary_frames: dict[str, gpd.GeoDataFrame] = {}
    farm_records: dict[str, dict] = {}
    weather_frames: list[pd.DataFrame] = []
    cdl_frames: list[pd.DataFrame] = []
    rotation_frames: list[pd.DataFrame] = []
    problems: list[str] = []

    for farm in all_farms:
        key = f"{farm['grower_slug']}/{farm['farm_slug']}"
        try:
            boundary = load_boundaries(farm)
            weather, weather_path = load_weather(farm, args.weather_years)
            cdl, cdl_paths = load_cdl(farm, args.cdl_years)
            rotation, rotation_path = load_rotation(farm)
        except SystemExit as exc:
            problems.append(f"{key}: {exc}")
            continue

        expected = set(boundary["field_id"].astype(str))
        weather_fields = set(weather["field_id"].astype(str))
        if expected - weather_fields:
            problems.append(f"{key}: weather missing fields {sorted(expected - weather_fields)}")
        for year in range(args.weather_years[0], args.weather_years[1] + 1):
            if weather.loc[weather["year"] == year].empty:
                problems.append(f"{key}: weather has no {year} rows")
        for year in range(args.cdl_years[0], args.cdl_years[1] + 1):
            year_rows = cdl.loc[cdl["year"] == year]
            if year_rows.empty:
                problems.append(f"{key}: CDL has no {year} rows")
                continue
            year_fields = set(year_rows["field_id"].astype(str))
            if expected - year_fields:
                problems.append(f"{key}: CDL {year} missing fields {sorted(expected - year_fields)}")

        boundary_frames[key] = boundary
        farm_records[key] = farm
        weather_frames.append(weather)
        cdl_frames.append(cdl)
        rotation_frames.append(rotation)
        input_paths.extend([weather_path, rotation_path, *cdl_paths])

        if args.check_inputs:
            print(
                f"PASS: {key} — {len(boundary)} fields, "
                f"weather {args.weather_years[0]}-{args.weather_years[1]} "
                f"({len(weather):,} rows), CDL {args.cdl_years[0]}-{args.cdl_years[1]} "
                f"({len(cdl_paths)} files)"
            )

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1

    if args.check_inputs:
        print(f"PASS: input contract satisfied for {len(all_farms)} farm(s)")
        return 0

    geometry = pd.concat(
        [derive_geometry(frame) for frame in boundary_frames.values()], ignore_index=True
    )
    weather = pd.concat(weather_frames, ignore_index=True)
    cdl = pd.concat(cdl_frames, ignore_index=True)
    rotation = pd.concat(rotation_frames, ignore_index=True)

    field_year, field_summary = derive_weather(weather, args.season)
    dominant, cdl_wide = derive_cdl(cdl, rotation)

    fields = geometry.merge(
        field_summary, on=["grower_slug", "farm_slug", "field_id"], how="left"
    ).merge(
        cdl_wide, on=["grower_slug", "farm_slug", "field_id"], how="left"
    )
    fields["agreement_2023"] = (
        fields["boundary_crop"].astype(str).str.lower()
        == fields["crop_2023"].astype(str).str.lower()
    )

    labels = {}
    colors = {}
    for index, grower in enumerate(growers):
        slug = grower["grower_slug"]
        state = next(
            (farm["state"] for farm in all_farms if farm["grower_slug"] == slug), ""
        )
        labels[slug] = grower_label(
            {
                "state": state,
                "grower_name": grower["grower_name"],
                "grower_slug": slug,
            }
        )
        colors[slug] = PALETTE[index % len(PALETTE)]

    metrics, tests, key_metrics = collect_metrics(
        fields, [grower["grower_slug"] for grower in growers]
    )

    if args.clean:
        for sub in ("figures", "maps", "tables"):
            target = out / sub
            if target.exists():
                for item in target.glob("*"):
                    item.unlink()
    for sub in ("figures", "maps", "tables"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    style_axes()
    plt.rcParams["savefig.dpi"] = args.dpi

    boundary_by_farm = [
        (farm_records[key], frame) for key, frame in boundary_frames.items()
    ]

    written: list[Path] = []
    written.append(figure_boundaries(fields, labels, colors, metrics, out))
    written.append(figure_cdl(fields, cdl, labels, colors, metrics, out))
    written.append(figure_weather(field_year, labels, colors, metrics, out))
    written.append(figure_compactness_edge(fields, labels, colors, metrics, out))
    written.append(map_boundaries(boundary_by_farm, labels, colors, out))
    written.append(map_cdl(boundary_by_farm, fields, labels, out))
    written.append(map_weather(boundary_by_farm, fields, labels, out))

    tables = build_tables(fields, field_year, dominant)
    tests_frame = pd.DataFrame(tests)
    if not tests_frame.empty:
        column_order = [
            "category",
            "comparison",
            "test",
            "metric",
            "group_a",
            "group_b",
            "n",
            "statistic",
            "p_value",
            "p_bh",
            "effect_size",
            "effect_size_name",
            "note",
        ]
        tests_frame = tests_frame[[c for c in column_order if c in tests_frame.columns]]
    tables["statistical_tests"] = tests_frame

    for name, frame in tables.items():
        path = out / "tables" / f"{name}.csv"
        frame.to_csv(path, index=False, float_format="%.6g")
        written.append(path)

    parameters = {
        "growers_root": str(growers_root),
        "growers": [grower["grower_slug"] for grower in growers],
        "weather_years": list(args.weather_years),
        "cdl_years": list(args.cdl_years),
        "season_months": list(args.season),
        "gdd_base_c": GDD_BASE_C,
        "gdd_cap_c": GDD_CAP_C,
        "hot_day_c": HOT_DAY_C,
        "dry_day_mm": DRY_DAY_MM,
        "area_crs": AREA_CRS,
    }
    counts = {
        "growers": len({farm["grower_slug"] for farm in all_farms}),
        "farms": len(all_farms),
        "fields": int(len(fields)),
        "weather_rows": int(len(field_year)),
        "cdl_rows": int(len(cdl)),
    }
    manifest = write_manifest(out, parameters, input_paths, counts, key_metrics)
    written.append(manifest)

    print(
        f"Wrote {len(written)} artifacts for {counts['fields']} fields "
        f"across {counts['farms']} farm(s) to {out}"
    )
    for path in written:
        print(f"  {path.relative_to(out)}  ({path.stat().st_size:,} bytes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Static field-level EDA across boundaries, CDL, and weather."
    )
    parser.add_argument("--growers-root", help="Override the growers directory")
    parser.add_argument(
        "--out",
        help="Output directory (default: <data-root>/derived/eda/field-comparison/latest)",
    )
    parser.add_argument(
        "--grower",
        action="append",
        help="Grower slug to include; repeatable. Default: all growers.",
    )
    parser.add_argument(
        "--all-growers",
        action="store_true",
        help="Explicitly include all growers (the default).",
    )
    parser.add_argument(
        "--weather-years",
        type=parse_year_range,
        default=(2021, 2025),
        help="Weather year range, for example 2021-2025",
    )
    parser.add_argument(
        "--cdl-years",
        type=parse_year_range,
        default=(2019, 2023),
        help="CDL year range, for example 2019-2023",
    )
    parser.add_argument(
        "--season",
        type=parse_month_range,
        default=(4, 10),
        help="Season month range, for example 4-10",
    )
    parser.add_argument("--dpi", type=int, default=150, help="PNG resolution")
    parser.add_argument(
        "--check-inputs",
        action="store_true",
        help="Validate the input contract and exit without plotting",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove previous figures/maps/tables before writing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.grower and args.all_growers:
        raise SystemExit("Use either --grower or --all-growers, not both")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
