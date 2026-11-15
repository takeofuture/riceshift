#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RiceShift Step 27
SMAP California subsets -> Sacramento Valley analysis-ready features

Input
-----
data/riceshift_nasa/smap_california/<year>/*_CA_subset.h5

Outputs
-------
processed/27_riceshift_smap_features/
    smap_daily_pass.csv/parquet
    smap_daily.csv/parquet
    smap_monthly.csv/parquet
    smap_seasonal.csv/parquet
    smap_annual.csv/parquet
    processed_files.csv
    _work/observations.jsonl
    _manifests/errors.jsonl
    _manifests/run_summary.json

Resume-safe:
- only new/changed completed H5 files are processed
- safe to rerun while Step 26 continues downloading
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
import pandas as pd


VERSION = "27.2-no-coverage"

ROOT = Path(__file__).resolve().parent

INPUT_ROOT = (
    ROOT
    / "data"
    / "riceshift_nasa"
    / "smap_california"
)

OUT_ROOT = (
    ROOT
    / "processed"
    / "27_riceshift_smap_features"
)

WORK_DIR = OUT_ROOT / "_work"
MANIFEST_DIR = OUT_ROOT / "_manifests"

OBS_JSONL = WORK_DIR / "observations.jsonl"
PROCESSED_CSV = OUT_ROOT / "processed_files.csv"
ERROR_JSONL = MANIFEST_DIR / "errors.jsonl"
SUMMARY_JSON = MANIFEST_DIR / "run_summary.json"

DAILY_PASS_CSV = OUT_ROOT / "smap_daily_pass.csv"
DAILY_PASS_PARQUET = OUT_ROOT / "smap_daily_pass.parquet"

DAILY_CSV = OUT_ROOT / "smap_daily.csv"
DAILY_PARQUET = OUT_ROOT / "smap_daily.parquet"

MONTHLY_CSV = OUT_ROOT / "smap_monthly.csv"
MONTHLY_PARQUET = OUT_ROOT / "smap_monthly.parquet"

SEASONAL_CSV = OUT_ROOT / "smap_seasonal.csv"
SEASONAL_PARQUET = OUT_ROOT / "smap_seasonal.parquet"

ANNUAL_CSV = OUT_ROOT / "smap_annual.csv"
ANNUAL_PARQUET = OUT_ROOT / "smap_annual.parquet"


# Sacramento Valley
WEST = -123.20
SOUTH = 38.00
EAST = -120.70
NORTH = 40.70

# NSIDC recommended-quality values
QA_GOOD = (0, 8)

DATE_RE = re.compile(r"_(20\d{6})_")

PROCESSED_FIELDS = [
    "source_file",
    "size_bytes",
    "mtime_ns",
    "fingerprint",
    "date",
    "passes",
    "processed_utc",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def atomic_json(path, obj):
    tmp = Path(str(path) + ".part")
    tmp.unlink(missing_ok=True)

    tmp.write_text(
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    tmp.replace(path)


def atomic_csv(df, path):
    tmp = Path(str(path) + ".part")
    tmp.unlink(missing_ok=True)

    df.to_csv(
        tmp,
        index=False,
    )

    tmp.replace(path)


def atomic_parquet(df, path):
    tmp = Path(str(path) + ".part")
    tmp.unlink(missing_ok=True)

    df.to_parquet(
        tmp,
        index=False,
    )

    tmp.replace(path)


def write_pair(df, csv_path, parquet_path):
    atomic_csv(df, csv_path)
    atomic_parquet(df, parquet_path)


def append_jsonl(path, row):
    with path.open(
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=True,
            )
            + "\n"
        )

        f.flush()
        os.fsync(f.fileno())


def source_fingerprint(path):
    st = path.stat()

    s = (
        f"{path.resolve()}|"
        f"{st.st_size}|"
        f"{st.st_mtime_ns}"
    )

    return hashlib.sha1(
        s.encode("utf-8")
    ).hexdigest()


def parse_date(path):
    m = DATE_RE.search(path.name)

    if not m:
        raise RuntimeError(
            f"Cannot parse date: {path.name}"
        )

    return datetime.strptime(
        m.group(1),
        "%Y%m%d",
    ).date()


def attr_float(ds, names):
    for name in names:

        if name not in ds.attrs:
            continue

        try:
            v = np.asarray(
                ds.attrs[name]
            ).reshape(-1)[0]

            if isinstance(v, bytes):
                v = v.decode()

            return float(v)

        except Exception:
            pass

    return None


def read_scaled(ds):
    raw = np.asarray(ds[...])

    arr = raw.astype(
        "float64",
        copy=False,
    )

    valid = np.isfinite(arr)

    fill = attr_float(
        ds,
        [
            "_FillValue",
            "fill_value",
            "missing_value",
        ],
    )

    if fill is not None:
        valid &= raw != fill

    scale = attr_float(
        ds,
        ["scale_factor"],
    )

    offset = attr_float(
        ds,
        ["add_offset"],
    )

    if scale is None:
        scale = 1.0

    if offset is None:
        offset = 0.0

    arr = (
        arr * scale
        + offset
    )

    valid &= np.isfinite(arr)

    return arr, valid


def read_raw(ds):
    raw = np.asarray(ds[...])

    arr = raw.astype(
        "float64",
        copy=False,
    )

    valid = np.isfinite(arr)

    fill = attr_float(
        ds,
        [
            "_FillValue",
            "fill_value",
            "missing_value",
        ],
    )

    if fill is not None:
        valid &= raw != fill

    return arr, valid


def pick_dataset(group, kind):
    candidates = []

    def visitor(name, obj):

        if not isinstance(
            obj,
            h5py.Dataset,
        ):
            return

        base = (
            name
            .split("/")[-1]
            .lower()
        )

        score = None

        if kind == "soil":

            if base in (
                "soil_moisture",
                "soil_moisture_pm",
            ):
                score = 0

            elif (
                base.startswith(
                    "soil_moisture"
                )
                and "option" not in base
            ):
                score = 10

        elif kind == "qa":

            if base in (
                "retrieval_qual_flag",
                "retrieval_qual_flag_pm",
            ):
                score = 0

            elif (
                base.startswith(
                    "retrieval_qual_flag"
                )
                and "option" not in base
            ):
                score = 10

        elif kind == "lat":

            if base in (
                "latitude",
                "latitude_pm",
            ):
                score = 0

            elif (
                base.startswith("latitude")
                and "centroid" not in base
            ):
                score = 10

        elif kind == "lon":

            if base in (
                "longitude",
                "longitude_pm",
            ):
                score = 0

            elif (
                base.startswith("longitude")
                and "centroid" not in base
            ):
                score = 10

        if score is not None:
            candidates.append(
                (
                    score,
                    name,
                    obj,
                )
            )

    group.visititems(visitor)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1],
        )
    )

    return candidates[0][2]


def align_coords(lat, lon, shape):

    lat = np.asarray(lat)
    lon = np.asarray(lon)

    if (
        lat.shape == shape
        and lon.shape == shape
    ):
        return lat, lon

    if len(shape) != 2:
        raise RuntimeError(
            f"Unexpected soil shape: {shape}"
        )

    nr, nc = shape

    if (
        lat.ndim == 1
        and lon.ndim == 1
    ):

        if (
            len(lat) == nr
            and len(lon) == nc
        ):

            lon2, lat2 = np.meshgrid(
                lon,
                lat,
            )

            return lat2, lon2

    raise RuntimeError(
        "Cannot align coordinate grids: "
        f"soil={shape}, "
        f"lat={lat.shape}, "
        f"lon={lon.shape}"
    )


def process_group(
    path,
    group_name,
    group,
    pass_name,
    date_value,
    fingerprint,
):

    soil_ds = pick_dataset(
        group,
        "soil",
    )

    qa_ds = pick_dataset(
        group,
        "qa",
    )

    lat_ds = pick_dataset(
        group,
        "lat",
    )

    lon_ds = pick_dataset(
        group,
        "lon",
    )

    if soil_ds is None:
        raise RuntimeError(
            f"{group_name}: soil_moisture missing"
        )

    if qa_ds is None:
        raise RuntimeError(
            f"{group_name}: retrieval_qual_flag missing"
        )

    if lat_ds is None or lon_ds is None:
        raise RuntimeError(
            f"{group_name}: lat/lon missing"
        )

    soil, soil_valid = read_scaled(
        soil_ds
    )

    qa, qa_valid = read_raw(
        qa_ds
    )

    lat, _ = read_scaled(
        lat_ds
    )

    lon, _ = read_scaled(
        lon_ds
    )

    if qa.shape != soil.shape:
        raise RuntimeError(
            f"QA shape mismatch "
            f"{qa.shape} != {soil.shape}"
        )

    lat, lon = align_coords(
        lat,
        lon,
        soil.shape,
    )

    geo_valid = (
        np.isfinite(lat)
        & np.isfinite(lon)
        & (lat >= -90)
        & (lat <= 90)
        & (lon >= -180)
        & (lon <= 180)
    )

    aoi = (
        geo_valid
        & (lon >= WEST)
        & (lon <= EAST)
        & (lat >= SOUTH)
        & (lat <= NORTH)
    )

    n_aoi = int(
        np.count_nonzero(aoi)
    )

    if n_aoi == 0:
        # A valid SMAP AM/PM swath may simply not intersect the
        # Sacramento Valley on this pass.  This is expected orbital
        # coverage behavior, not a processing error.
        return {
            "recorded_utc": utc_now(),
            "source_file": str(path.resolve()),
            "source_fingerprint": fingerprint,
            "date": date_value.isoformat(),
            "year": date_value.year,
            "month": date_value.month,
            "day": date_value.day,
            "pass": pass_name,
            "group": group_name,
            "soil_dataset": soil_ds.name,
            "qa_dataset": qa_ds.name,
            "qa_policy": "retrieval_qual_flag in {0,8}",
            "status": "NO_COVERAGE",
            "n_aoi": 0,
            "n_qa_good": 0,
            "n_valid": 0,
            "coverage_fraction": math.nan,
            "soil_moisture_mean": math.nan,
            "soil_moisture_std": math.nan,
            "soil_moisture_min": math.nan,
            "soil_moisture_p10": math.nan,
            "soil_moisture_median": math.nan,
            "soil_moisture_p90": math.nan,
            "soil_moisture_max": math.nan,
            "unit": "m3/m3",
        }

    qa_int = qa.astype(
        "int64",
        copy=False,
    )

    qa_good = (
        qa_valid
        & np.isin(
            qa_int,
            QA_GOOD,
        )
    )

    valid = (
        aoi
        & soil_valid
        & qa_good
        & (soil >= 0.0)
        & (soil <= 1.0)
    )

    values = soil[valid]

    n_valid = int(
        values.size
    )

    if n_valid:

        mean = float(
            np.mean(values)
        )

        std = float(
            np.std(values)
        )

        p10, p50, p90 = [
            float(x)
            for x in np.percentile(
                values,
                [10, 50, 90],
            )
        ]

        vmin = float(
            np.min(values)
        )

        vmax = float(
            np.max(values)
        )

    else:

        mean = math.nan
        std = math.nan
        p10 = math.nan
        p50 = math.nan
        p90 = math.nan
        vmin = math.nan
        vmax = math.nan

    return {
        "recorded_utc": utc_now(),
        "source_file": str(
            path.resolve()
        ),
        "source_fingerprint":
            fingerprint,
        "date":
            date_value.isoformat(),
        "year":
            date_value.year,
        "month":
            date_value.month,
        "day":
            date_value.day,
        "pass":
            pass_name,
        "group":
            group_name,
        "soil_dataset":
            soil_ds.name,
        "qa_dataset":
            qa_ds.name,
        "qa_policy":
            "retrieval_qual_flag in {0,8}",
        "status":
            "OK",
        "n_aoi":
            n_aoi,
        "n_qa_good":
            int(
                np.count_nonzero(
                    aoi & qa_good
                )
            ),
        "n_valid":
            n_valid,
        "coverage_fraction":
            (
                n_valid / n_aoi
                if n_aoi
                else math.nan
            ),
        "soil_moisture_mean":
            mean,
        "soil_moisture_std":
            std,
        "soil_moisture_min":
            vmin,
        "soil_moisture_p10":
            p10,
        "soil_moisture_median":
            p50,
        "soil_moisture_p90":
            p90,
        "soil_moisture_max":
            vmax,
        "unit":
            "m3/m3",
    }


def process_file(path_text):

    path = Path(path_text)

    date_value = parse_date(
        path
    )

    fp = source_fingerprint(
        path
    )

    rows = []

    with h5py.File(
        path,
        "r",
    ) as h:

        for group_name in h.keys():

            upper = (
                group_name.upper()
            )

            if (
                "SOIL_MOISTURE_RETRIEVAL_DATA_AM"
                in upper
            ):
                pass_name = "AM"

            elif (
                "SOIL_MOISTURE_RETRIEVAL_DATA_PM"
                in upper
            ):
                pass_name = "PM"

            else:
                continue

            group = h[group_name]

            rows.append(
                process_group(
                    path,
                    group_name,
                    group,
                    pass_name,
                    date_value,
                    fp,
                )
            )

    if not rows:
        raise RuntimeError(
            "No AM/PM soil moisture groups found"
        )

    st = path.stat()

    return {
        "source_file":
            str(path.resolve()),
        "size_bytes":
            st.st_size,
        "mtime_ns":
            st.st_mtime_ns,
        "fingerprint":
            fp,
        "date":
            date_value.isoformat(),
        "rows":
            rows,
    }


def load_done():

    if not PROCESSED_CSV.exists():
        return set()

    d = pd.read_csv(
        PROCESSED_CSV
    )

    if "fingerprint" not in d:
        return set()

    return set(
        d["fingerprint"]
        .dropna()
        .astype(str)
    )


def append_processed(result):

    new_file = (
        not PROCESSED_CSV.exists()
    )

    with PROCESSED_CSV.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as f:

        w = csv.DictWriter(
            f,
            fieldnames=PROCESSED_FIELDS,
        )

        if new_file:
            w.writeheader()

        w.writerow({
            "source_file":
                result["source_file"],
            "size_bytes":
                result["size_bytes"],
            "mtime_ns":
                result["mtime_ns"],
            "fingerprint":
                result["fingerprint"],
            "date":
                result["date"],
            "passes":
                len(result["rows"]),
            "processed_utc":
                utc_now(),
        })

        f.flush()
        os.fsync(f.fileno())


def load_observations():

    rows = {}

    malformed = 0

    if OBS_JSONL.exists():

        with OBS_JSONL.open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as f:

            for line in f:

                line = line.strip()

                if not line:
                    continue

                try:

                    r = json.loads(
                        line
                    )

                    key = (
                        r["source_file"],
                        r["pass"],
                    )

                    # latest record wins
                    rows[key] = r

                except Exception:
                    malformed += 1

    if not rows:
        return pd.DataFrame(), malformed

    d = pd.DataFrame(
        rows.values()
    )

    d["date"] = pd.to_datetime(
        d["date"]
    )

    d = (
        d.sort_values(
            [
                "date",
                "pass",
                "source_file",
            ]
        )
        .reset_index(drop=True)
    )

    return d, malformed


def wmean(values, weights):

    v = pd.to_numeric(
        values,
        errors="coerce",
    )

    w = pd.to_numeric(
        weights,
        errors="coerce",
    )

    ok = (
        v.notna()
        & w.notna()
        & (w > 0)
    )

    if ok.any():

        return float(
            (
                v[ok]
                * w[ok]
            ).sum()
            / w[ok].sum()
        )

    if v.notna().any():
        return float(
            v.mean()
        )

    return math.nan


def build_daily(passes):

    rows = []

    for date_value, g in passes.groupby(
        "date",
        sort=True,
    ):

        d = pd.Timestamp(
            date_value
        )

        n_valid = pd.to_numeric(
            g["n_valid"],
            errors="coerce",
        ).fillna(0)

        n_aoi = pd.to_numeric(
            g["n_aoi"],
            errors="coerce",
        ).fillna(0)

        am = g[
            g["pass"] == "AM"
        ]

        pm = g[
            g["pass"] == "PM"
        ]

        am_value = (
            float(
                am[
                    "soil_moisture_mean"
                ].iloc[0]
            )
            if len(am)
            else math.nan
        )

        pm_value = (
            float(
                pm[
                    "soil_moisture_mean"
                ].iloc[0]
            )
            if len(pm)
            else math.nan
        )

        am_n = (
            int(
                am["n_valid"].iloc[0]
            )
            if len(am)
            else 0
        )

        pm_n = (
            int(
                pm["n_valid"].iloc[0]
            )
            if len(pm)
            else 0
        )

        rows.append({
            "date":
                d.date().isoformat(),
            "year":
                d.year,
            "month":
                d.month,
            "day":
                d.day,
            "pass_count":
                int(
                    g["pass"].nunique()
                ),
            "passes_present":
                ",".join(
                    sorted(
                        g["pass"]
                        .astype(str)
                        .unique()
                    )
                ),
            "n_valid":
                int(n_valid.sum()),
            "n_valid_am":
                am_n,
            "n_valid_pm":
                pm_n,
            "n_aoi":
                int(n_aoi.sum()),
            "coverage_fraction":
                (
                    float(
                        n_valid.sum()
                        / n_aoi.sum()
                    )
                    if n_aoi.sum() > 0
                    else math.nan
                ),
            "soil_moisture_mean":
                wmean(
                    g[
                        "soil_moisture_mean"
                    ],
                    n_valid,
                ),
            "soil_moisture_am":
                am_value,
            "soil_moisture_pm":
                pm_value,
            "unit":
                "m3/m3",
        })

    return pd.DataFrame(
        rows
    )


def aggregate(df, group_columns):

    rows = []

    for key, g in df.groupby(
        group_columns,
        sort=True,
    ):

        if not isinstance(
            key,
            tuple,
        ):
            key = (key,)

        row = dict(
            zip(
                group_columns,
                key,
            )
        )

        n = pd.to_numeric(
            g["n_valid"],
            errors="coerce",
        ).fillna(0)

        nam = pd.to_numeric(
            g["n_valid_am"],
            errors="coerce",
        ).fillna(0)

        npm = pd.to_numeric(
            g["n_valid_pm"],
            errors="coerce",
        ).fillna(0)

        row.update({
            "days_present":
                int(
                    g["date"].nunique()
                ),
            "soil_moisture_mean":
                wmean(
                    g[
                        "soil_moisture_mean"
                    ],
                    n,
                ),
            "soil_moisture_am":
                wmean(
                    g[
                        "soil_moisture_am"
                    ],
                    nam,
                ),
            "soil_moisture_pm":
                wmean(
                    g[
                        "soil_moisture_pm"
                    ],
                    npm,
                ),
            "coverage_mean":
                float(
                    pd.to_numeric(
                        g[
                            "coverage_fraction"
                        ],
                        errors="coerce",
                    ).mean()
                ),
            "valid_pixel_observations":
                int(
                    n.sum()
                ),
            "unit":
                "m3/m3",
        })

        rows.append(
            row
        )

    return pd.DataFrame(
        rows
    )


def build_monthly(daily):

    return aggregate(
        daily,
        ["year", "month"],
    )


def build_annual(daily):

    out = aggregate(
        daily,
        ["year"],
    )

    out["season"] = (
        "full_year_jan_dec"
    )

    return out


def build_seasonal(daily):

    outputs = []

    # Rice season
    rice = daily[
        daily["month"].isin(
            list(range(4, 12))
        )
    ].copy()

    if not rice.empty:

        r = aggregate(
            rice,
            ["year"],
        )

        r["season"] = (
            "rice_apr_nov"
        )

        outputs.append(r)

    # Winter cover season:
    # target year 2020 =
    # Nov-Dec 2019 + Jan-Mar 2020
    winter = daily[
        daily["month"].isin(
            [11, 12, 1, 2, 3]
        )
    ].copy()

    if not winter.empty:

        winter["year"] = (
            np.where(
                winter["month"].isin(
                    [11, 12]
                ),
                winter["year"] + 1,
                winter["year"],
            )
            .astype(int)
        )

        # First complete winter possible:
        # Nov-Dec 2015 + Jan-Mar 2016
        max_data_year = int(daily["year"].max())

        winter = winter[
            (winter["year"] >= 2016)
            & (winter["year"] <= max_data_year)
        ]

        w = aggregate(
            winter,
            ["year"],
        )

        w["season"] = (
            "winter_cover_nov_mar"
        )

        outputs.append(w)

    if not outputs:
        return pd.DataFrame()

    out = pd.concat(
        outputs,
        ignore_index=True,
    )

    return (
        out.sort_values(
            ["season", "year"]
        )
        .reset_index(drop=True)
    )


def reset():

    for p in [
        OBS_JSONL,
        PROCESSED_CSV,
        ERROR_JSONL,
        SUMMARY_JSON,
        DAILY_PASS_CSV,
        DAILY_PASS_PARQUET,
        DAILY_CSV,
        DAILY_PARQUET,
        MONTHLY_CSV,
        MONTHLY_PARQUET,
        SEASONAL_CSV,
        SEASONAL_PARQUET,
        ANNUAL_CSV,
        ANNUAL_PARQUET,
    ]:
        p.unlink(
            missing_ok=True
        )


def parse_args():

    p = argparse.ArgumentParser()

    p.add_argument(
        "--workers",
        type=int,
        default=2,
    )

    p.add_argument(
        "--limit",
        type=int,
        default=0,
    )

    p.add_argument(
        "--min-age-seconds",
        type=int,
        default=90,
    )

    p.add_argument(
        "--reset",
        action="store_true",
    )

    return p.parse_args()


def main():

    args = parse_args()

    ensure_dirs()

    if args.reset:
        reset()
        ensure_dirs()

    if not INPUT_ROOT.exists():
        raise FileNotFoundError(
            INPUT_ROOT
        )

    # Force parquet dependency check now.
    try:
        import pyarrow  # noqa
    except Exception as e:
        raise RuntimeError(
            "pyarrow is required"
        ) from e

    now = time.time()

    visible = sorted(
        INPUT_ROOT.rglob(
            "*_CA_subset.h5"
        )
    )

    # Avoid touching the file Step27 may
    # currently be finishing.
    stable = [
        p
        for p in visible
        if (
            p.is_file()
            and p.stat().st_size > 0
            and (
                now
                - p.stat().st_mtime
                >= args.min_age_seconds
            )
        )
    ]

    done = load_done()

    pending = [
        p
        for p in stable
        if source_fingerprint(p)
        not in done
    ]

    if args.limit > 0:
        pending = pending[
            : args.limit
        ]

    print("=" * 78)
    print(
        "STEP 27 — SMAP SACRAMENTO VALLEY"
    )
    print(
        "Visible subset files :",
        len(visible),
    )
    print(
        "Stable files         :",
        len(stable),
    )
    print(
        "Already processed    :",
        len(done),
    )
    print(
        "Pending this run     :",
        len(pending),
    )
    print(
        "Workers              :",
        args.workers,
    )
    print(
        "QA                   :",
        "retrieval_qual_flag 0 or 8",
    )
    print(
        "BBox                 :",
        (
            WEST,
            SOUTH,
            EAST,
            NORTH,
        ),
    )
    print("=" * 78)

    completed = 0
    errors = 0

    if pending:

        with ProcessPoolExecutor(
            max_workers=max(
                1,
                args.workers,
            )
        ) as executor:

            futures = {
                executor.submit(
                    process_file,
                    str(p),
                ): p
                for p in pending
            }

            for i, future in enumerate(
                as_completed(futures),
                1,
            ):

                p = futures[future]

                try:

                    result = (
                        future.result()
                    )

                    for row in result[
                        "rows"
                    ]:
                        append_jsonl(
                            OBS_JSONL,
                            row,
                        )

                    append_processed(
                        result
                    )

                    completed += 1

                    if (
                        i <= 10
                        or i % 100 == 0
                        or i == len(pending)
                    ):

                        print(
                            f"[{i}/{len(pending)}] "
                            f"OK {p.name} "
                            f"passes="
                            f"{len(result['rows'])}"
                        )

                except Exception as e:

                    errors += 1

                    append_jsonl(
                        ERROR_JSONL,
                        {
                            "recorded_utc":
                                utc_now(),
                            "source_file":
                                str(p),
                            "error_type":
                                type(e).__name__,
                            "error":
                                str(e),
                        },
                    )

                    print(
                        f"[{i}/{len(pending)}] "
                        f"ERROR {p.name}: "
                        f"{type(e).__name__}: "
                        f"{e}"
                    )

    passes, malformed = (
        load_observations()
    )

    if not passes.empty:

        daily = build_daily(
            passes
        )

        monthly = build_monthly(
            daily
        )

        seasonal = build_seasonal(
            daily
        )

        annual = build_annual(
            daily
        )

        passes_out = (
            passes.copy()
        )

        passes_out["date"] = (
            passes_out["date"]
            .dt.date
            .astype(str)
        )

        write_pair(
            passes_out,
            DAILY_PASS_CSV,
            DAILY_PASS_PARQUET,
        )

        write_pair(
            daily,
            DAILY_CSV,
            DAILY_PARQUET,
        )

        write_pair(
            monthly,
            MONTHLY_CSV,
            MONTHLY_PARQUET,
        )

        write_pair(
            seasonal,
            SEASONAL_CSV,
            SEASONAL_PARQUET,
        )

        write_pair(
            annual,
            ANNUAL_CSV,
            ANNUAL_PARQUET,
        )

    else:

        daily = pd.DataFrame()
        monthly = pd.DataFrame()
        seasonal = pd.DataFrame()
        annual = pd.DataFrame()

    summary = {
        "generated_utc":
            utc_now(),
        "version":
            VERSION,
        "input_root":
            str(INPUT_ROOT),
        "bbox_wgs84":
            [
                WEST,
                SOUTH,
                EAST,
                NORTH,
            ],
        "qa":
            "retrieval_qual_flag in {0,8}",
        "subset_files_visible":
            len(visible),
        "stable_files":
            len(stable),
        "processed_total":
            len(load_done()),
        "completed_this_run":
            completed,
        "errors_this_run":
            errors,
        "malformed_jsonl_lines":
            malformed,
        "daily_pass_rows":
            len(passes),
        "daily_rows":
            len(daily),
        "monthly_rows":
            len(monthly),
        "seasonal_rows":
            len(seasonal),
        "annual_rows":
            len(annual),
    }

    atomic_json(
        SUMMARY_JSON,
        summary,
    )

    print()
    print("=" * 78)
    print("STEP 27 SUMMARY")
    print(
        "Processed total    :",
        len(load_done()),
    )
    print(
        "Completed this run :",
        completed,
    )
    print(
        "Errors this run    :",
        errors,
    )
    print(
        "Daily pass rows    :",
        len(passes),
    )
    print(
        "Daily rows         :",
        len(daily),
    )
    print(
        "Monthly rows       :",
        len(monthly),
    )
    print(
        "Seasonal rows      :",
        len(seasonal),
    )
    print(
        "Annual rows        :",
        len(annual),
    )
    print("=" * 78)

    if errors:

        print(
            "STEP 27 finished with errors. "
            "Successful files remain checkpointed."
        )

        return 2

    print(
        "✓ STEP 27 incremental run complete. "
        "Run again later to process newly downloaded SMAP subsets."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
