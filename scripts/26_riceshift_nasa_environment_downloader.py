#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
26_riceshift_nasa_environment_downloader.py

RiceShift / NASA Space Apps 2026
STEP_VERSION = 26R.3-rolling-smap-ca

Purpose
-------
Prepare NASA environmental inputs without keeping multi-terabyte global SMAP
archives on disk.

Key SMAP policy
---------------
For SPL3SMP_E v006:
  1. Find daily global granules for 2015-2025.
  2. Download ONE granule at a time.
  3. Crop the native SMAP grid to California + a geographic buffer.
  4. Preserve all spatial datasets in AM/PM retrieval groups, plus metadata.
  5. Store a compressed HDF5 subset.
  6. Delete the temporary global HDF5 immediately.
  7. Continue to the next day.

This makes the peak raw-storage requirement approximately one global granule,
not the ~2.4 TB required to keep the whole 2015-2025 global archive.

Other sources
-------------
- NASA POWER: annual regional NetCDF chunks for Sacramento Valley.
- GPM: inventory by default; explicit full download remains optional.
- ECOSTRESS: inventory by default; explicit download remains optional.

Authentication
--------------
config.py next to this script:

    NASA_USERNAME = "..."
    NASA_PASSWORD = "..."

Examples
--------
POWER + inventories only:
    python 26_riceshift_nasa_environment_downloader.py

SMAP rolling California subset, full 2015-2025:
    python 26_riceshift_nasa_environment_downloader.py \
        --sources smap --smap-rolling-subset

SMAP smoke test, first 2 matching daily granules:
    python 26_riceshift_nasa_environment_downloader.py \
        --sources smap --smap-rolling-subset --max-files 2

Resume is automatic: an existing non-empty subset output is skipped.

Custom retained SMAP bbox:
    python 26_riceshift_nasa_environment_downloader.py \
        --sources smap --smap-rolling-subset \
        --smap-bbox -125.5 31.5 -113.0 42.7

Keep temporary global SMAP raw files for debugging:
    ... --keep-smap-raw
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests

STEP_VERSION = "26R.3-rolling-smap-ca"

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
OUT_ROOT = DATA_DIR / "riceshift_nasa"
MANIFEST_DIR = OUT_ROOT / "_manifests"

# Primary RiceShift analysis region.
SACRAMENTO_BBOX = (-123.20, 38.00, -120.70, 40.70)

# Retain more than Sacramento Valley so the analysis region can be widened later
# without re-downloading the global SMAP archive.
# Approximate California extent plus geographic buffer.
DEFAULT_SMAP_BBOX = (-125.00, 32.00, -113.50, 42.50)

CORE_START = "2015-03-31"
CORE_END = "2025-12-31"
ECOSTRESS_START = "2018-07-09"
ECOSTRESS_END = CORE_END

POWER_PARAMS = (
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "RH2M",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
)

EARTHDATA_COLLECTIONS = {
    "smap": {
        "short_name": "SPL3SMP_E",
        "version": "006",
        "start": CORE_START,
        "end": CORE_END,
        "notes": "SMAP Enhanced L3 Radiometer Daily 9 km Soil Moisture",
    },
    "gpm": {
        "short_name": "GPM_3IMERGDF",
        "version": "07",
        "start": "2015-01-01",
        "end": CORE_END,
        "notes": "GPM IMERG Final Daily 0.1-degree precipitation",
    },
    "ecostress_lst": {
        "short_name": "ECO_L2T_LSTE",
        "version": "002",
        "start": ECOSTRESS_START,
        "end": ECOSTRESS_END,
        "notes": "ECOSTRESS tiled LST/Emissivity 70 m",
    },
    "ecostress_et": {
        "short_name": "ECO_L3T_JET",
        "version": "002",
        "start": ECOSTRESS_START,
        "end": ECOSTRESS_END,
        "notes": "ECOSTRESS tiled ET 70 m",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)


def load_earthdata_credentials() -> tuple[str, str]:
    try:
        from config import NASA_USERNAME, NASA_PASSWORD
    except ImportError as e:
        raise RuntimeError(
            "config.py was not found next to this script. "
            "Create config.py with NASA_USERNAME and NASA_PASSWORD."
        ) from e

    user = str(NASA_USERNAME or "").strip()
    pw = str(NASA_PASSWORD or "").strip()
    if not user or not pw:
        raise RuntimeError("NASA_USERNAME / NASA_PASSWORD are empty in config.py")
    return user, pw


def earthaccess_login():
    try:
        import earthaccess
    except Exception as e:
        raise RuntimeError("pip install earthaccess") from e

    user, pw = load_earthdata_credentials()
    os.environ["EARTHDATA_USERNAME"] = user
    os.environ["EARTHDATA_PASSWORD"] = pw
    auth = earthaccess.login(strategy="environment")
    if not auth:
        raise RuntimeError("Earthdata login failed")
    return earthaccess


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


# ---------------------------------------------------------------------------
# POWER
# ---------------------------------------------------------------------------

def power_download(start: str, end: str, inventory_only: bool = False) -> None:
    base = "https://power.larc.nasa.gov/api/temporal/daily/regional"
    west, south, east, north = SACRAMENTO_BBOX
    outdir = OUT_ROOT / "power_daily"
    outdir.mkdir(parents=True, exist_ok=True)

    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()

    for year in range(start_dt.year, end_dt.year + 1):
        y_start = max(start_dt, date(year, 1, 1))
        y_end = min(end_dt, date(year, 12, 31))
        if y_start > y_end:
            continue

        for param in POWER_PARAMS:
            params = {
                "parameters": param,
                "community": "AG",
                "longitude-min": west,
                "longitude-max": east,
                "latitude-min": south,
                "latitude-max": north,
                "start": y_start.strftime("%Y%m%d"),
                "end": y_end.strftime("%Y%m%d"),
                "format": "NETCDF",
                "time-standard": "UTC",
                "user": "RiceShift",
            }
            req = requests.Request("GET", base, params=params).prepare()
            target = outdir / (
                f"power_daily_{param}_{y_start.isoformat()}_{y_end.isoformat()}.nc"
            )

            append_csv(
                MANIFEST_DIR / "power_requests.csv",
                {
                    "recorded_utc": utc_now(),
                    "source": "NASA_POWER",
                    "product": param,
                    "start": y_start.isoformat(),
                    "end": y_end.isoformat(),
                    "bbox": json.dumps(SACRAMENTO_BBOX),
                    "url": req.url,
                    "local_path": str(target),
                },
            )

            print("[POWER]", year, param, req.url)

            if inventory_only:
                continue
            if target.exists() and target.stat().st_size > 0:
                print("[EXISTS]", target)
                continue

            part = target.with_suffix(target.suffix + ".part")
            part.unlink(missing_ok=True)

            with requests.get(req.url, stream=True, timeout=300) as r:
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"NASA POWER HTTP {r.status_code}\n"
                        f"URL: {req.url}\n"
                        f"Response: {r.text[:4000]}"
                    )
                with part.open("wb") as f:
                    for chunk in r.iter_content(8 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)

            if not part.exists() or part.stat().st_size <= 0:
                raise IOError(f"Empty POWER download: {req.url}")
            part.replace(target)
            print(
                "[DOWNLOADED]",
                target,
                f"{target.stat().st_size / 1024**2:.1f} MB",
            )


# ---------------------------------------------------------------------------
# Earthdata inventory helpers
# ---------------------------------------------------------------------------

def granule_filename(g) -> str:
    try:
        for u in g.data_links(access="external"):
            name = Path(u.split("?")[0]).name
            if name:
                return name
    except Exception:
        pass
    try:
        return str(g["umm"]["GranuleUR"])
    except Exception:
        return "granule"


def granule_temporal(g) -> dict:
    try:
        return g["umm"]["TemporalExtent"]["RangeDateTime"]
    except Exception:
        return {}


def granule_date(g) -> str:
    temporal = granule_temporal(g)
    s = temporal.get("BeginningDateTime")
    if s:
        return str(s)[:10]

    name = granule_filename(g)
    # Common SMAP form contains YYYYMMDD or AYYYYDDD.
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"A(20\d{2})(\d{3})", name)
    if m:
        d = datetime.strptime(m.group(1) + m.group(2), "%Y%j").date()
        return d.isoformat()

    return "unknown-date"


def save_inventory(name: str, results: list) -> Path:
    rows = []
    for g in results:
        try:
            links = list(g.data_links(access="external"))
        except Exception:
            links = []
        temporal = granule_temporal(g)
        size = getattr(g, "size", None)
        if callable(size):
            try:
                size = size()
            except Exception:
                size = None
        rows.append(
            {
                "filename": granule_filename(g),
                "time_start": temporal.get("BeginningDateTime"),
                "time_end": temporal.get("EndingDateTime"),
                "size_mb": size,
                "urls": links,
            }
        )

    path = MANIFEST_DIR / f"{name}_inventory.json"
    path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print("[INVENTORY]", name, len(rows), "->", path)
    return path


def estimate_granule_size_gb(results: list) -> float:
    total_mb = 0.0
    for g in results:
        try:
            s = getattr(g, "size", None)
            if callable(s):
                s = s()
            if s is not None:
                total_mb += float(s)
        except Exception:
            pass
    return total_mb / 1024.0


def effective_dates(key: str, start: str, end: str) -> tuple[str, str]:
    spec = EARTHDATA_COLLECTIONS[key]
    a = max(datetime.strptime(start, "%Y-%m-%d").date(),
            datetime.strptime(spec["start"], "%Y-%m-%d").date())
    b = min(datetime.strptime(end, "%Y-%m-%d").date(),
            datetime.strptime(spec["end"], "%Y-%m-%d").date())
    if a > b:
        raise ValueError(f"No overlap for {key}: requested {start}..{end}")
    return a.isoformat(), b.isoformat()


def search_collection(
    earthaccess,
    key: str,
    start: str,
    end: str,
    bbox: tuple[float, float, float, float],
) -> list:
    spec = EARTHDATA_COLLECTIONS[key]
    s, e = effective_dates(key, start, end)

    print(
        f"[SEARCH] {key} {spec['short_name']} v{spec['version']} "
        f"{s} -> {e} bbox={bbox}"
    )

    results = earthaccess.search_data(
        short_name=spec["short_name"],
        version=spec["version"],
        temporal=(s, e),
        bounding_box=bbox,
        downloadable=True,
        count=-1,
    )
    results = list(results)
    save_inventory(key, results)

    est_gb = estimate_granule_size_gb(results)
    if est_gb > 0:
        print(
            f"[SIZE ESTIMATE] {key}: {len(results)} granules, "
            f"~{est_gb:.1f} GB global/raw"
        )
    else:
        print(
            f"[SIZE ESTIMATE] {key}: {len(results)} granules; "
            "size metadata unavailable"
        )
    return results


# ---------------------------------------------------------------------------
# SMAP rolling subset
# ---------------------------------------------------------------------------

def _copy_attrs(src, dst) -> None:
    for k, v in src.attrs.items():
        try:
            dst.attrs[k] = v
        except Exception:
            dst.attrs[k] = str(v)


def _find_lat_lon_names(group) -> tuple[Optional[str], Optional[str]]:
    names = list(group.keys())
    datasets = [n for n in names if hasattr(group[n], "shape")]

    # Prefer exact/common names, then keyword matching.
    lat_candidates = [
        n for n in datasets
        if n.lower() in ("latitude", "latitude_pm", "lat")
    ]
    lon_candidates = [
        n for n in datasets
        if n.lower() in ("longitude", "longitude_pm", "lon")
    ]

    if not lat_candidates:
        lat_candidates = [n for n in datasets if "latitude" in n.lower()]
    if not lon_candidates:
        lon_candidates = [n for n in datasets if "longitude" in n.lower()]

    for lat_name in lat_candidates:
        lat_shape = group[lat_name].shape
        if len(lat_shape) != 2:
            continue
        for lon_name in lon_candidates:
            if group[lon_name].shape == lat_shape:
                return lat_name, lon_name

    return None, None


def _iter_groups_with_latlon(h5):
    found = []

    def visitor(name, obj):
        import h5py
        if isinstance(obj, h5py.Group):
            lat_name, lon_name = _find_lat_lon_names(obj)
            if lat_name and lon_name:
                found.append((name, obj, lat_name, lon_name))

    h5.visititems(visitor)
    return found


def _safe_dataset_create(dst_group, name: str, data, src_ds) -> None:
    import numpy as np

    kwargs = {}
    arr = np.asarray(data)

    # Compression helps greatly for regional subsets. Scalar datasets cannot
    # use chunk/filter options.
    if arr.ndim > 0 and arr.size > 0:
        kwargs.update(compression="gzip", compression_opts=4, shuffle=True)

    # Preserve fillvalue where possible.
    try:
        fv = src_ds.fillvalue
        if fv is not None:
            kwargs["fillvalue"] = fv
    except Exception:
        pass

    try:
        dst = dst_group.create_dataset(name, data=arr, **kwargs)
    except Exception:
        # Some unusual HDF5 dtypes/filters may reject copied options.
        kwargs.pop("fillvalue", None)
        kwargs.pop("shuffle", None)
        kwargs.pop("compression_opts", None)
        kwargs.pop("compression", None)
        dst = dst_group.create_dataset(name, data=arr)

    _copy_attrs(src_ds, dst)


def subset_smap_hdf5(
    raw_path: Path,
    output_path: Path,
    bbox: tuple[float, float, float, float],
) -> dict:
    """
    Crop each SMAP retrieval group containing a native 2-D latitude/longitude
    grid. All datasets sharing that spatial shape are retained.

    A per-group `riceshift_bbox_mask` is also stored. This allows later
    analysis to distinguish the exact requested geographic bbox from the
    rectangular row/column crop needed to preserve a regular array.
    """
    import h5py
    import numpy as np

    west, south, east, north = bbox
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".part")
    tmp.unlink(missing_ok=True)

    groups_written = 0
    datasets_written = 0
    windows = []

    with h5py.File(raw_path, "r") as src, h5py.File(tmp, "w") as dst:
        _copy_attrs(src, dst)
        dst.attrs["riceshift_step_version"] = STEP_VERSION
        dst.attrs["riceshift_subset_bbox_wsen"] = json.dumps(bbox)
        dst.attrs["riceshift_source_filename"] = raw_path.name
        dst.attrs["riceshift_created_utc"] = utc_now()

        spatial_groups = _iter_groups_with_latlon(src)
        if not spatial_groups:
            raise RuntimeError(
                f"No SMAP group with matching 2-D latitude/longitude arrays: "
                f"{raw_path}"
            )

        spatial_group_names = set()

        for group_name, src_group, lat_name, lon_name in spatial_groups:
            spatial_group_names.add(group_name)
            lat = np.asarray(src_group[lat_name][...])
            lon = np.asarray(src_group[lon_name][...])

            valid = (
                np.isfinite(lat)
                & np.isfinite(lon)
                & (lon >= west)
                & (lon <= east)
                & (lat >= south)
                & (lat <= north)
            )
            if not valid.any():
                print(f"[SMAP SUBSET] no cells in bbox for group {group_name}")
                continue

            rr, cc = np.where(valid)
            r0, r1 = int(rr.min()), int(rr.max()) + 1
            c0, c1 = int(cc.min()), int(cc.max()) + 1
            crop_mask = valid[r0:r1, c0:c1]

            dst_group = dst.require_group(group_name)
            _copy_attrs(src_group, dst_group)
            dst_group.attrs["riceshift_crop_window"] = json.dumps(
                [r0, r1, c0, c1]
            )
            dst_group.attrs["riceshift_latitude_dataset"] = lat_name
            dst_group.attrs["riceshift_longitude_dataset"] = lon_name

            _safe_dataset_create(
                dst_group,
                "riceshift_bbox_mask",
                crop_mask.astype("uint8"),
                src_group[lat_name],
            )
            datasets_written += 1

            grid_shape = lat.shape

            for name, obj in src_group.items():
                if not isinstance(obj, h5py.Dataset):
                    continue

                if len(obj.shape) >= 2 and tuple(obj.shape[:2]) == tuple(grid_shape):
                    # Preserve extra dimensions after the spatial dimensions.
                    slicer = (slice(r0, r1), slice(c0, c1)) + (
                        slice(None),
                    ) * (len(obj.shape) - 2)
                    data = obj[slicer]
                else:
                    # Small non-grid datasets/metadata are cheap to preserve.
                    # Avoid copying unexpectedly huge unrelated arrays.
                    if obj.size > 5_000_000:
                        print(
                            f"[SMAP SUBSET] skip large non-grid dataset "
                            f"{group_name}/{name} shape={obj.shape}"
                        )
                        continue
                    data = obj[...]

                _safe_dataset_create(dst_group, name, data, obj)
                datasets_written += 1

            groups_written += 1
            windows.append(
                {
                    "group": group_name,
                    "lat": lat_name,
                    "lon": lon_name,
                    "source_shape": list(grid_shape),
                    "crop": [r0, r1, c0, c1],
                    "subset_shape": [r1 - r0, c1 - c0],
                    "bbox_cells": int(crop_mask.sum()),
                }
            )

        # Copy root-level and non-spatial-group small metadata datasets/groups.
        # We intentionally do not duplicate large arrays outside retrieval groups.
        meta = dst.require_group("RiceShift_Metadata")
        meta.attrs["spatial_groups"] = json.dumps(windows)
        meta.attrs["groups_written"] = groups_written
        meta.attrs["datasets_written"] = datasets_written

    if groups_written == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"No SMAP spatial groups intersected bbox {bbox}")

    tmp.replace(output_path)

    return {
        "groups_written": groups_written,
        "datasets_written": datasets_written,
        "windows": windows,
        "output_bytes": output_path.stat().st_size,
    }


def _download_one_earthaccess(
    earthaccess,
    granule,
    raw_dir: Path,
    retries: int = 3,
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    expected_name = granule_filename(granule)
    expected = raw_dir / expected_name

    if expected.exists() and expected.stat().st_size > 0:
        return expected

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            result = earthaccess.download([granule], str(raw_dir))

            # earthaccess versions differ slightly in return type.
            candidates = []
            if result:
                if isinstance(result, (str, Path)):
                    candidates = [Path(result)]
                else:
                    candidates = [Path(x) for x in result if x]

            for p in candidates:
                if p.exists() and p.stat().st_size > 0:
                    return p

            if expected.exists() and expected.stat().st_size > 0:
                return expected

            # Last-resort: newest file in temp directory.
            files = [
                p for p in raw_dir.iterdir()
                if p.is_file() and not p.name.endswith(".part")
            ]
            if files:
                newest = max(files, key=lambda p: p.stat().st_mtime)
                if newest.stat().st_size > 0:
                    return newest

            raise IOError("earthaccess returned no usable downloaded file")

        except Exception as e:
            last_error = e
            print(
                f"[SMAP DOWNLOAD RETRY] attempt {attempt}/{retries}: "
                f"{type(e).__name__}: {e}"
            )
            if attempt < retries:
                time.sleep(5 * attempt)

    raise RuntimeError(f"SMAP download failed after {retries} tries: {last_error}")


def run_smap_rolling_subset(
    earthaccess,
    start: str,
    end: str,
    bbox: tuple[float, float, float, float],
    max_files: Optional[int],
    keep_raw: bool,
    min_free_gb: float,
) -> None:
    results = search_collection(
        earthaccess,
        "smap",
        start,
        end,
        bbox,
    )

    if max_files is not None:
        results = results[:max_files]

    subset_root = OUT_ROOT / "smap_california"
    raw_dir = OUT_ROOT / "_tmp_smap_global"
    subset_root.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest = MANIFEST_DIR / "smap_rolling_subset.csv"

    print(
        f"[SMAP ROLLING] {len(results)} granules; "
        f"retain bbox={bbox}; keep_raw={keep_raw}"
    )

    for idx, g in enumerate(results, start=1):
        d = granule_date(g)
        year = d[:4] if d[:4].isdigit() else "unknown"
        src_name = granule_filename(g)

        stem = Path(src_name).stem
        output = subset_root / year / f"{stem}_CA_subset.h5"

        if output.exists() and output.stat().st_size > 0:
            print(
                f"[SMAP {idx}/{len(results)}] EXISTS {d} "
                f"{output.name} "
                f"{output.stat().st_size/1024**2:.1f} MB"
            )
            continue

        free = disk_free_gb(OUT_ROOT)
        if free < min_free_gb:
            raise RuntimeError(
                f"Only {free:.1f} GB free; below --min-free-gb "
                f"{min_free_gb:.1f}. Stop before downloading another raw granule."
            )

        print(
            f"[SMAP {idx}/{len(results)}] {d} download -> subset -> delete raw"
        )

        raw_path = None
        raw_bytes = None

        try:
            raw_path = _download_one_earthaccess(earthaccess, g, raw_dir)
            raw_bytes = raw_path.stat().st_size
            print(
                f"  [RAW] {raw_path.name} "
                f"{raw_bytes/1024**2:.1f} MB"
            )

            try:
                stats = subset_smap_hdf5(raw_path, output, bbox)
            except RuntimeError as e:
                if "No SMAP spatial groups intersected bbox" in str(e):
                    print(f"  [NO COVERAGE] {d} — no valid SMAP cells in retained bbox")
                    append_csv(
                        manifest,
                        {
                            "recorded_utc": utc_now(),
                            "date": d,
                            "source_filename": src_name,
                            "raw_bytes": raw_bytes or "",
                            "subset_path": "",
                            "subset_bytes": "",
                            "bbox": json.dumps(bbox),
                            "groups_written": 0,
                            "datasets_written": 0,
                            "status": "NO_COVERAGE",
                        },
                    )
                    continue
                raise

            print(
                f"  [SUBSET] {output} "
                f"{stats['output_bytes']/1024**2:.1f} MB "
                f"groups={stats['groups_written']} "
                f"datasets={stats['datasets_written']}"
            )

            append_csv(
                manifest,
                {
                    "recorded_utc": utc_now(),
                    "date": d,
                    "source_filename": src_name,
                    "raw_bytes": raw_bytes,
                    "subset_path": str(output),
                    "subset_bytes": stats["output_bytes"],
                    "bbox": json.dumps(bbox),
                    "groups_written": stats["groups_written"],
                    "datasets_written": stats["datasets_written"],
                    "status": "OK",
                },
            )

        except Exception as e:
            append_csv(
                manifest,
                {
                    "recorded_utc": utc_now(),
                    "date": d,
                    "source_filename": src_name,
                    "raw_bytes": raw_bytes or "",
                    "subset_path": str(output),
                    "subset_bytes": "",
                    "bbox": json.dumps(bbox),
                    "groups_written": "",
                    "datasets_written": "",
                    "status": f"ERROR: {type(e).__name__}: {e}",
                },
            )
            raise

        finally:
            if raw_path and raw_path.exists() and not keep_raw:
                raw_path.unlink()
                print("  [RAW DELETED]", raw_path.name)

    # Clean empty temp directory when possible.
    if not keep_raw:
        try:
            raw_dir.rmdir()
        except OSError:
            pass

    print("[SMAP ROLLING] DONE")


# ---------------------------------------------------------------------------
# GPM / ECOSTRESS existing behavior
# ---------------------------------------------------------------------------

def download_results(
    earthaccess,
    key: str,
    results: list,
    max_files: Optional[int],
) -> None:
    outdir = OUT_ROOT / key
    outdir.mkdir(parents=True, exist_ok=True)
    if max_files is not None:
        results = results[:max_files]
    if not results:
        return
    print(f"[DOWNLOAD] {key}: {len(results)} granules")
    earthaccess.download(results, str(outdir))


def run_gpm(
    earthaccess,
    start: str,
    end: str,
    inventory_only: bool,
    download_gpm: bool,
    max_files: Optional[int],
) -> None:
    results = search_collection(
        earthaccess,
        "gpm",
        start,
        end,
        SACRAMENTO_BBOX,
    )
    if not inventory_only and download_gpm:
        download_results(earthaccess, "gpm", results, max_files)
    elif not inventory_only:
        print("[SAFE MODE] GPM inventory created; raw download skipped.")


def run_ecostress(
    earthaccess,
    start: str,
    end: str,
    inventory_only: bool,
    download_ecostress: bool,
    max_files: Optional[int],
) -> None:
    for key in ("ecostress_lst", "ecostress_et"):
        results = search_collection(
            earthaccess,
            key,
            start,
            end,
            SACRAMENTO_BBOX,
        )
        if not inventory_only and download_ecostress:
            download_results(earthaccess, key, results, max_files)
        elif not inventory_only:
            print(f"[SAFE MODE] {key} inventory created; raw download skipped.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()

    p.add_argument(
        "--sources",
        nargs="+",
        choices=["power", "smap", "gpm", "ecostress"],
        default=["power", "smap", "gpm", "ecostress"],
    )
    p.add_argument("--inventory-only", action="store_true")

    # New recommended SMAP mode.
    p.add_argument(
        "--smap-rolling-subset",
        action="store_true",
        help=(
            "Download one global SMAP granule at a time, crop to --smap-bbox, "
            "write compressed subset, then delete global raw."
        ),
    )

    # Backward-compatible alias. It now means rolling subset, NOT full archive.
    p.add_argument(
        "--download-smap",
        action="store_true",
        help=(
            "Alias for --smap-rolling-subset. Does NOT retain the full "
            "multi-terabyte SMAP archive."
        ),
    )

    p.add_argument(
        "--smap-bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=list(DEFAULT_SMAP_BBOX),
        help=(
            "Geographic bbox retained from each SMAP granule. "
            f"Default: {DEFAULT_SMAP_BBOX}"
        ),
    )
    p.add_argument(
        "--keep-smap-raw",
        action="store_true",
        help="Keep temporary global SMAP HDF5 after successful subset.",
    )
    p.add_argument(
        "--min-free-gb",
        type=float,
        default=20.0,
        help="Stop SMAP rolling download before free disk falls below this value.",
    )

    p.add_argument("--download-gpm", action="store_true")
    p.add_argument("--download-ecostress", action="store_true")
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument("--start", default=CORE_START)
    p.add_argument("--end", default=CORE_END)
    p.add_argument("--version", action="version", version=STEP_VERSION)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    smap_bbox = tuple(float(x) for x in args.smap_bbox)

    print("RiceShift NASA data preparation")
    print("Step version:", STEP_VERSION)
    print("RiceShift AOI:", SACRAMENTO_BBOX)
    print("SMAP retained bbox:", smap_bbox)
    print("Core period:", args.start, "to", args.end)
    print("Sources:", ", ".join(args.sources))
    print("Inventory only:", args.inventory_only)

    if "power" in args.sources:
        power_download(
            args.start,
            args.end,
            inventory_only=args.inventory_only,
        )

    nasa_sources = set(args.sources) & {"smap", "gpm", "ecostress"}
    if nasa_sources:
        ea = earthaccess_login()

        if "smap" in args.sources:
            rolling = args.smap_rolling_subset or args.download_smap
            if args.inventory_only or not rolling:
                search_collection(
                    ea,
                    "smap",
                    args.start,
                    args.end,
                    smap_bbox,
                )
                if not args.inventory_only:
                    print(
                        "[SAFE MODE] SMAP inventory created. "
                        "Use --smap-rolling-subset for one-day-at-a-time "
                        "California extraction."
                    )
            else:
                run_smap_rolling_subset(
                    ea,
                    args.start,
                    args.end,
                    smap_bbox,
                    args.max_files,
                    args.keep_smap_raw,
                    args.min_free_gb,
                )

        if "gpm" in args.sources:
            run_gpm(
                ea,
                args.start,
                args.end,
                args.inventory_only,
                args.download_gpm,
                args.max_files,
            )

        if "ecostress" in args.sources:
            run_ecostress(
                ea,
                args.start,
                args.end,
                args.inventory_only,
                args.download_ecostress,
                args.max_files,
            )

    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
