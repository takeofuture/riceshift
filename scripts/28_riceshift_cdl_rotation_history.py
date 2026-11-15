#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
28_riceshift_cdl_rotation_history.py

Build Sacramento Valley crop-history products from annual USDA CDL rasters.

This is a data-preparation step, not a recommendation model.

Paper alignment
---------------
Salvato et al. (2024) used:
  * CDL 2007-2021
  * DWR/LandIQ field boundaries
  * 11 broad crop groups
  * field-level dominant crop per year
  * strict continuous-rice / rotated-rice categories

This script reproduces the historical logic as closely as possible with two modes:

A) Raster mode (default):
   30 m CDL pixels are treated as the spatial units. This is fully reproducible
   from public CDL alone but does not correct CDL edge/classification error.

B) Field mode (optional):
   pass --fields /path/to/field_boundaries.gpkg
   Each polygon gets a dominant CDL class/group per year, matching the paper's
   field-level concept much more closely.

The original paper's specific DWR/LandIQ field boundary layer is not bundled
here. If you later obtain it, pass it with --fields.

Outputs
-------
processed/28_riceshift_rotation_history/
  class_mapping.csv
  yearly_group_counts.csv
  transition_counts.csv
  pixel_or_field_history.parquet
  rotation_class_summary.csv
  raster/rotation_class.tif          # raster mode
  raster/rice_year_count.tif         # raster mode
  raster/rotation_year_count.tif     # raster mode
  fields/rotation_fields.gpkg        # field mode

Rotation definitions
--------------------
For the paper window 2007-2021 (15 years), exact paper thresholds are used:
  continuous_rice:
      rice >= 12 years and NO summer/winter annual, alfalfa, or grass years
  rotated_rice:
      rice >= 7 years and rotation crop on >= 2 separate years
  rice_plus_other:
      rice >= 9 years but not classified above

For other windows, thresholds are scaled proportionally:
  continuous rice >= ceil(12/15 * n_years)
  rotated rice    >= ceil( 7/15 * n_years)
  rice+other      >= ceil( 9/15 * n_years)
  rotation years  >= max(2, ceil(2/15 * n_years))

Important
---------
The crop-group mapping below is intentionally conservative and explicit.
Raw CDL class values are retained in the long table so the mapping can be
audited/changed later.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import from_bounds

PROJECT_DIR = Path(__file__).resolve().parent
CDL_ROOT = PROJECT_DIR / "data" / "usda_cdl_california"
OUT_ROOT = PROJECT_DIR / "processed" / "28_riceshift_rotation_history"
MANIFEST_DIR = OUT_ROOT / "_manifests"

BBOX = (-123.20, 38.00, -120.70, 40.70)  # Sacramento Valley broad AOI
DEFAULT_START = 2007
DEFAULT_END = 2025

# Conservative CDL grouping focused on rice-rotation crops in Sacramento Valley.
# Unknown/unlisted agricultural classes remain "other".
GROUPS: Dict[str, set[int]] = {
    "rice": {3},
    "fallow": {61},
    # Warm-season / summer annual candidates commonly discussed for CA rice rotations
    "summer_annual": {
        1,   # corn
        2,   # cotton
        4,   # sorghum
        5,   # soybeans
        6,   # sunflower
        12,  # sweet corn
        13,  # popcorn
        31,  # canola (often cool season; kept annual here for conservative grouping)
        33,  # safflower
        42,  # dry beans
        54,  # tomatoes
    },
    # Cool-season / winter annuals
    "winter_annual": {
        21,  # barley
        22,  # durum wheat
        23,  # spring wheat
        24,  # winter wheat
        27,  # rye
        28,  # oats
    },
    "alfalfa": {36},
    "grasses": {37, 58, 59, 60, 176},
    "walnut": {76},
    "almond": {75},
    "other_perennial": {
        66, 67, 68, 69, 70, 71, 72, 74, 77,
        204, 205, 206, 207, 208, 209, 210, 211,
        212, 213, 214, 215, 216, 217, 218, 219,
        220, 221, 222, 223,
    },
}

ROTATION_GROUPS = {"summer_annual", "winter_annual", "alfalfa", "grasses"}

GROUP_CODE = {
    "other": 0,
    "rice": 1,
    "fallow": 2,
    "summer_annual": 3,
    "winter_annual": 4,
    "alfalfa": 5,
    "grasses": 6,
    "walnut": 7,
    "almond": 8,
    "other_perennial": 9,
}

ROTATION_CLASS_CODE = {
    "other": 0,
    "continuous_rice": 1,
    "rotated_rice": 2,
    "rice_plus_other": 3,
}


def ensure_dirs() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def class_to_group(arr: np.ndarray) -> np.ndarray:
    out = np.zeros(arr.shape, dtype=np.uint8)  # other
    for group, classes in GROUPS.items():
        code = GROUP_CODE[group]
        for c in classes:
            out[arr == c] = code
    return out


def group_name_from_code(code: int) -> str:
    rev = {v: k for k, v in GROUP_CODE.items()}
    return rev.get(int(code), "other")


def cdl_path(year: int) -> Path:
    return CDL_ROOT / str(year) / f"cdl_california_{year}.tif"


def available_years(start: int, end: int) -> list[int]:
    years = [y for y in range(start, end + 1) if cdl_path(y).exists()]
    missing = [y for y in range(start, end + 1) if not cdl_path(y).exists()]
    if missing:
        print("WARNING missing CDL years:", missing)
    if not years:
        raise FileNotFoundError(f"No CDL rasters found under {CDL_ROOT}")
    return years


def aoi_window(src) -> rasterio.windows.Window:
    from rasterio.warp import transform_bounds
    b = transform_bounds("EPSG:4326", src.crs, *BBOX, densify_pts=21)
    w = from_bounds(*b, transform=src.transform)
    return w.round_offsets().round_lengths()


def build_common_grid(years: list[int]) -> dict:
    """
    Build one fixed 30 m Sacramento Valley grid for all CDL years.

    Historical CDL rasters do not necessarily use the same native pixel size
    or raster extent. Rotation-history analysis requires every year to refer
    to exactly the same pixels.

    We use the CRS of the latest available CDL year and a fixed 30 m grid.
    Categorical CDL values are later reprojected with nearest-neighbor only.
    """
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    ref_year = years[-1]
    ref_path = cdl_path(ref_year)

    with rasterio.open(ref_path) as src:
        target_crs = src.crs
        if target_crs is None:
            raise RuntimeError(f"CDL {ref_year} has no CRS: {ref_path}")

        left, bottom, right, top = transform_bounds(
            "EPSG:4326",
            target_crs,
            *BBOX,
            densify_pts=21,
        )

        res = 30.0

        # Snap all edges to a stable 30 m grid.
        left   = math.floor(left   / res) * res
        bottom = math.floor(bottom / res) * res
        right  = math.ceil(right   / res) * res
        top    = math.ceil(top     / res) * res

        width = int(round((right - left) / res))
        height = int(round((top - bottom) / res))

        transform = from_origin(left, top, res, res)

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            crs=target_crs,
            transform=transform,
            width=width,
            height=height,
            count=1,
            dtype="uint8",
            nodata=0,
        )

    print(
        f"[COMMON GRID] reference_year={ref_year} "
        f"shape=({height}, {width}) resolution={res}m "
        f"crs={target_crs}"
    )

    return profile


def read_aoi(year: int, target_profile: dict) -> tuple[np.ndarray, dict, rasterio.Affine]:
    """
    Read one CDL year onto the fixed common 30 m grid.

    CDL is categorical data, therefore nearest-neighbor resampling is required.
    """
    from rasterio.warp import reproject, Resampling

    p = cdl_path(year)

    height = int(target_profile["height"])
    width = int(target_profile["width"])

    dst = np.zeros((height, width), dtype=np.uint8)

    with rasterio.open(p) as src:
        if src.crs is None:
            raise RuntimeError(f"CDL {year} has no CRS: {p}")

        print(
            f"[ALIGN] {year}: "
            f"native={src.height}x{src.width} "
            f"res=({abs(src.transform.a):.3f}, {abs(src.transform.e):.3f}) "
            f"-> common={height}x{width} 30m"
        )

        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=target_profile["transform"],
            dst_crs=target_profile["crs"],
            dst_nodata=0,
            resampling=Resampling.nearest,
        )

    profile = target_profile.copy()
    return dst, profile, profile["transform"]


def write_mapping() -> None:
    rows = []
    for group, classes in GROUPS.items():
        for c in sorted(classes):
            rows.append({"group": group, "cdl_class": c, "group_code": GROUP_CODE[group]})
    pd.DataFrame(rows).to_csv(MANIFEST_DIR / "class_mapping.csv", index=False)


def thresholds(n: int) -> dict[str, int]:
    if n == 15:
        return {
            "continuous_rice_years": 12,
            "rotated_rice_years": 7,
            "rice_plus_other_years": 9,
            "rotation_years": 2,
        }
    return {
        "continuous_rice_years": math.ceil((12 / 15) * n),
        "rotated_rice_years": math.ceil((7 / 15) * n),
        "rice_plus_other_years": math.ceil((9 / 15) * n),
        "rotation_years": max(2, math.ceil((2 / 15) * n)),
    }


def classify_history(group_stack: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray], dict]:
    # stack shape: [year, row, col]
    n = group_stack.shape[0]
    th = thresholds(n)
    rice = np.sum(group_stack == GROUP_CODE["rice"], axis=0)
    rot = np.zeros(rice.shape, dtype=np.uint16)
    for g in ROTATION_GROUPS:
        rot += np.sum(group_stack == GROUP_CODE[g], axis=0).astype(np.uint16)

    continuous = (rice >= th["continuous_rice_years"]) & (rot == 0)
    rotated = (rice >= th["rotated_rice_years"]) & (rot >= th["rotation_years"])
    rice_other = (
        (rice >= th["rice_plus_other_years"])
        & (~continuous)
        & (~rotated)
    )

    cls = np.zeros(rice.shape, dtype=np.uint8)
    cls[continuous] = ROTATION_CLASS_CODE["continuous_rice"]
    cls[rotated] = ROTATION_CLASS_CODE["rotated_rice"]
    cls[rice_other] = ROTATION_CLASS_CODE["rice_plus_other"]

    return cls, {"rice_years": rice, "rotation_years": rot}, th


def write_raster(path: Path, arr: np.ndarray, profile: dict, nodata: Optional[float] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    p = profile.copy()

    # Remove inherited source tiling/block settings.
    # Historical CDL files may carry block sizes that are invalid for a
    # newly-created tiled GeoTIFF (GDAL requires multiples of 16).
    p.pop("blockxsize", None)
    p.pop("blockysize", None)
    p.pop("interleave", None)

    p.update(
        driver="GTiff",
        dtype=str(arr.dtype),
        count=1,
        compress="DEFLATE",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="IF_SAFER",
    )
    if nodata is not None:
        p["nodata"] = nodata
    with rasterio.open(path, "w", **p) as dst:
        dst.write(arr, 1)


def raster_mode(years: list[int]) -> None:
    stack = []
    yearly_counts = []

    # Every historical CDL year is aligned to exactly the same 30 m grid.
    base_profile = build_common_grid(years)
    expected_shape = (
        int(base_profile["height"]),
        int(base_profile["width"]),
    )

    for y in years:
        arr, profile, _ = read_aoi(y, base_profile)

        if arr.shape != expected_shape:
            raise RuntimeError(
                f"Unexpected aligned grid mismatch in {y}: "
                f"{arr.shape} vs {expected_shape}"
            )

        groups = class_to_group(arr)
        stack.append(groups)

        counts = Counter(groups.ravel().tolist())
        row = {"year": y, "pixels_total": int(groups.size)}
        for name, code in GROUP_CODE.items():
            row[name] = int(counts.get(code, 0))
        yearly_counts.append(row)

    group_stack = np.stack(stack, axis=0)

    cls, counts, th = classify_history(group_stack)

    out_raster = OUT_ROOT / "raster"
    write_raster(out_raster / "rotation_class.tif", cls, base_profile, nodata=0)
    write_raster(
        out_raster / "rice_year_count.tif",
        counts["rice_years"].astype(np.uint8),
        base_profile,
        nodata=0,
    )
    write_raster(
        out_raster / "rotation_year_count.tif",
        counts["rotation_years"].astype(np.uint8),
        base_profile,
        nodata=0,
    )

    pd.DataFrame(yearly_counts).to_csv(OUT_ROOT / "yearly_group_counts.csv", index=False)

    # Year-to-year transitions from rice to relevant groups and back.
    rows = []
    for i in range(1, len(years)):
        prev = group_stack[i - 1]
        cur = group_stack[i]
        for g in ROTATION_GROUPS | {"fallow"}:
            gc = GROUP_CODE[g]
            rows.append({
                "from_year": years[i - 1],
                "to_year": years[i],
                "transition": f"rice->{g}",
                "pixels": int(np.sum((prev == GROUP_CODE["rice"]) & (cur == gc))),
            })
            rows.append({
                "from_year": years[i - 1],
                "to_year": years[i],
                "transition": f"{g}->rice",
                "pixels": int(np.sum((prev == gc) & (cur == GROUP_CODE["rice"]))),
            })
    pd.DataFrame(rows).to_csv(OUT_ROOT / "transition_counts.csv", index=False)

    class_rows = []
    for name, code in ROTATION_CLASS_CODE.items():
        class_rows.append({
            "rotation_class": name,
            "code": code,
            "pixels": int(np.sum(cls == code)),
        })
    pd.DataFrame(class_rows).to_csv(OUT_ROOT / "rotation_class_summary.csv", index=False)

    (MANIFEST_DIR / "rotation_thresholds.json").write_text(
        json.dumps(
            {
                "years": years,
                "n_years": len(years),
                "thresholds": th,
                "definition_note": "Paper thresholds exact for 15 years; proportional otherwise.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def field_mode(years: list[int], field_path: Path, field_id: Optional[str]) -> None:
    try:
        import geopandas as gpd
        from rasterstats import zonal_stats
    except Exception as e:
        raise RuntimeError("Field mode requires: pip install geopandas rasterstats pyarrow") from e

    gdf = gpd.read_file(field_path)
    if gdf.empty:
        raise ValueError("Field boundary layer is empty.")
    if field_id is None:
        field_id = "_field_id"
        gdf[field_id] = np.arange(len(gdf), dtype=np.int64)
    elif field_id not in gdf.columns:
        raise ValueError(f"--field-id column not found: {field_id}")

    # Clip by Sacramento Valley bbox.
    aoi = gpd.GeoDataFrame(
        geometry=gpd.GeoSeries.from_bbox(BBOX),
        crs="EPSG:4326",
    )
    gdf = gdf.to_crs("EPSG:4326")
    gdf = gpd.clip(gdf, aoi)
    if gdf.empty:
        raise ValueError("No fields intersect Sacramento Valley AOI.")

    history_rows = []
    for y in years:
        p = cdl_path(y)
        with rasterio.open(p) as src:
            gg = gdf.to_crs(src.crs)
            zs = zonal_stats(
                gg.geometry,
                str(p),
                categorical=True,
                nodata=src.nodata,
                all_touched=False,
            )
        for idx, cat in enumerate(zs):
            if not cat:
                dominant = None
            else:
                dominant = int(max(cat.items(), key=lambda kv: kv[1])[0])
            group_code = int(class_to_group(np.array([[dominant if dominant is not None else -9999]]) )[0,0])
            history_rows.append({
                "field_id": gdf.iloc[idx][field_id],
                "year": y,
                "cdl_class": dominant,
                "crop_group": group_name_from_code(group_code),
            })

    hist = pd.DataFrame(history_rows)
    hist.to_parquet(OUT_ROOT / "field_crop_history.parquet", index=False)
    hist.to_csv(OUT_ROOT / "field_crop_history.csv", index=False)

    # Classify field histories with the same threshold logic.
    th = thresholds(len(years))
    summaries = []
    for fid, h in hist.groupby("field_id"):
        c = Counter(h["crop_group"].fillna("other").tolist())
        rice = c.get("rice", 0)
        rot = sum(c.get(g, 0) for g in ROTATION_GROUPS)
        if rice >= th["continuous_rice_years"] and rot == 0:
            rc = "continuous_rice"
        elif rice >= th["rotated_rice_years"] and rot >= th["rotation_years"]:
            rc = "rotated_rice"
        elif rice >= th["rice_plus_other_years"]:
            rc = "rice_plus_other"
        else:
            rc = "other"
        summaries.append({
            "field_id": fid,
            "rice_years": rice,
            "rotation_years": rot,
            "rotation_class": rc,
        })

    summ = pd.DataFrame(summaries)
    out = gdf.merge(summ, on=field_id, how="left")
    flddir = OUT_ROOT / "fields"
    flddir.mkdir(parents=True, exist_ok=True)
    out.to_file(flddir / "rotation_fields.gpkg", layer="rotation_fields", driver="GPKG")
    summ.to_csv(OUT_ROOT / "rotation_class_summary.csv", index=False)

    (MANIFEST_DIR / "rotation_thresholds.json").write_text(
        json.dumps({"years": years, "n_years": len(years), "thresholds": th}, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start-year", type=int, default=DEFAULT_START)
    p.add_argument("--end-year", type=int, default=DEFAULT_END)
    p.add_argument("--fields", type=Path, default=None)
    p.add_argument("--field-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    write_mapping()
    years = available_years(args.start_year, args.end_year)
    print("Years:", years)
    print("AOI:", BBOX)
    if args.fields:
        print("Mode: field polygons", args.fields)
        field_mode(years, args.fields, args.field_id)
    else:
        print("Mode: 30 m raster pixels")
        raster_mode(years)
    print("DONE:", OUT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
