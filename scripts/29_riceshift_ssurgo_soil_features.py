#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
29_riceshift_ssurgo_soil_features.py

Download and prepare official USDA-NRCS SSURGO soil data for the Sacramento
Valley using Soil Data Access (SDA) web services.

Paper alignment
---------------
Salvato et al. (2024) fused CDL land-use history with SSURGO soils and used:
  chemical: pH, electrical conductivity (EC), sodium adsorption ratio (SAR)
  physical: texture, saturated hydraulic conductivity (Ksat),
            linear extensibility
  categorical: soil order, irrigated capability class, restrictive layers

For numeric horizon properties, the paper calculated a depth-weighted mean over
the top 30 cm of the soil profile. This script implements that same preparation.

Workflow
--------
1) Download MapunitPoly geometry from SDA WFS, tiled over Sacramento Valley AOI.
2) Collect mukey values.
3) Query SDA tabular service for component + horizon attributes in batches.
4) Select the dominant component per map unit (largest comppct_r).
5) Compute depth-weighted top-30-cm means for numeric horizon properties.
6) Save:
      processed/29_riceshift_ssurgo/
        ssurgo_mapunits.gpkg
        ssurgo_horizons_raw.parquet
        ssurgo_mapunit_top30cm.parquet
        ssurgo_mapunits_with_top30cm.gpkg

Notes
-----
- Official Soil Data Access services are used; no scraping.
- WFS requests are tiled because SDA limits spatial response volume.
- This is soil data preparation only. No rotation recommendation is produced.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests

PROJECT_DIR = Path(__file__).resolve().parent
OUT_ROOT = PROJECT_DIR / "processed" / "29_riceshift_ssurgo"
MANIFEST_DIR = OUT_ROOT / "_manifests"

BBOX = (-123.20, 38.00, -120.70, 40.70)  # west,south,east,north
WFS = "https://SDMDataAccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
POST_REST = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest"

# Horizon numeric properties to depth-weight over 0-30 cm.
NUMERIC_PROPS = (
    "ph1to1h2o_r",
    "ec_r",
    "sar_r",
    "ksat_r",
    "lep_r",
    "sandtotal_r",
    "silttotal_r",
    "claytotal_r",
    "om_r",
)


def ensure_dirs() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def frange(a: float, b: float, step: float):
    x = a
    while x < b - 1e-12:
        yield x
        x += step


def wfs_url(bbox: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox
    # WFS 1.1.0 BBOX shortcut. EPSG:4326 response.
    params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": "MapunitPoly",
        "SRSNAME": "EPSG:4326",
        "OUTPUTFORMAT": "GML2",
        "BBOX": f"{west},{south},{east},{north}",
    }
    return requests.Request("GET", WFS, params=params).prepare().url


def download_mapunits(tile_deg: float = 0.5):
    try:
        import geopandas as gpd
    except Exception as e:
        raise RuntimeError("pip install geopandas pyogrio") from e

    west, south, east, north = BBOX
    pieces = []
    errors = []

    for x0 in frange(west, east, tile_deg):
        for y0 in frange(south, north, tile_deg):
            bb = (x0, y0, min(x0 + tile_deg, east), min(y0 + tile_deg, north))
            url = wfs_url(bb)
            name = f"mapunit_{x0:.2f}_{y0:.2f}.gml".replace("-", "m")
            fp = OUT_ROOT / "_wfs_tiles" / name
            fp.parent.mkdir(parents=True, exist_ok=True)

            try:
                if not fp.exists() or fp.stat().st_size == 0:
                    print("[WFS]", bb)
                    r = requests.get(url, timeout=180)
                    r.raise_for_status()
                    fp.write_bytes(r.content)
                    time.sleep(0.2)
                g = gpd.read_file(fp)
                if not g.empty:
                    pieces.append(g)
            except Exception as e:
                errors.append({"bbox": bb, "url": url, "error": repr(e)})
                print("WFS ERROR", bb, repr(e))

    if errors:
        (MANIFEST_DIR / "wfs_errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
    if not pieces:
        raise RuntimeError("No SSURGO mapunit polygons downloaded.")

    gdf = pd.concat(pieces, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=pieces[0].crs)

    # USDA SSURGO WFS/GML may arrive with latitude/longitude axis order.
    # Detect the California case where X looks like latitude (~38-41)
    # and Y looks like longitude (~-123 to -120), then swap X/Y.
    minx, miny, maxx, maxy = gdf.total_bounds

    if (
        20.0 <= minx <= 60.0
        and 20.0 <= maxx <= 60.0
        and -140.0 <= miny <= -100.0
        and -140.0 <= maxy <= -100.0
    ):
        from shapely.ops import transform as shapely_transform

        def _swap_xy(geom):
            if geom is None or geom.is_empty:
                return geom

            def _swap(x, y, z=None):
                if z is None:
                    return (y, x)
                return (y, x, z)

            return shapely_transform(_swap, geom)

        print("[CRS FIX] Detected latitude/longitude axis reversal.")
        print("[CRS FIX] BEFORE:", gdf.total_bounds)

        gdf["geometry"] = gdf.geometry.apply(_swap_xy)

        print("[CRS FIX] AFTER :", gdf.total_bounds)

    # SSURGO WFS coordinates are geographic WGS84.
    # Explicitly assign CRS because the WFS/GML source may omit usable CRS metadata.
    gdf = gdf.set_crs("EPSG:4326", allow_override=True)

    print("[CRS FIX] CRS   :", gdf.crs)
    # Normalize common field-name variants.
    low = {c.lower(): c for c in gdf.columns}
    mukey_col = low.get("mukey") or low.get("mapunitkey")
    if not mukey_col:
        raise RuntimeError(f"Could not locate mukey field. Columns: {list(gdf.columns)}")
    if mukey_col != "mukey":
        gdf = gdf.rename(columns={mukey_col: "mukey"})
    gdf["mukey"] = gdf["mukey"].astype(str)
    gdf = gdf.drop_duplicates(subset=["mukey", "geometry"]).reset_index(drop=True)
    out = OUT_ROOT / "ssurgo_mapunits.gpkg"
    gdf.to_file(out, layer="mapunits", driver="GPKG")
    print("Mapunits:", len(gdf), "->", out)
    return gdf


def chunks(seq: list[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def sda_query(sql: str) -> pd.DataFrame:
    r = requests.post(
        POST_REST,
        json={"query": sql, "format": "JSON+COLUMNNAME"},
        timeout=300,
    )
    r.raise_for_status()
    obj = r.json()
    table = obj.get("Table", [])
    if not table:
        return pd.DataFrame()
    # JSON+COLUMNNAME: first row contains column names.
    cols = table[0]
    rows = table[1:]
    return pd.DataFrame(rows, columns=cols)


def fetch_horizons(mukeys: list[str], batch_size: int = 250) -> pd.DataFrame:
    frames = []
    for i, batch in enumerate(chunks(mukeys, batch_size), start=1):
        vals = ",".join(f"'{str(x).replace(chr(39), chr(39)*2)}'" for x in batch)
        sql = f"""
        SELECT
            mu.mukey,
            mu.muname,
            c.cokey,
            c.compname,
            c.comppct_r,
            c.taxorder,
            c.irrcapcl,
            (SELECT TOP 1 cr.reskind FROM corestrictions cr WHERE cr.cokey = c.cokey AND cr.reskind IS NOT NULL ORDER BY cr.resdept_r) AS reskind,
            (SELECT MIN(cr.resdept_r) FROM corestrictions cr WHERE cr.cokey = c.cokey AND cr.reskind IS NOT NULL) AS resdept_r,
            ch.chkey,
            ch.hzname,
            ch.hzdept_r,
            ch.hzdepb_r,
            ch.ph1to1h2o_r,
            ch.ec_r,
            ch.sar_r,
            ch.ksat_r,
            ch.lep_r,
            ch.sandtotal_r,
            ch.silttotal_r,
            ch.claytotal_r,
            ch.om_r
        FROM mapunit mu
        INNER JOIN component c ON mu.mukey = c.mukey
        INNER JOIN chorizon ch ON c.cokey = ch.cokey
        WHERE mu.mukey IN ({vals})
        ORDER BY mu.mukey, c.comppct_r DESC, c.cokey, ch.hzdept_r
        """
        print(f"[SDA] batch {i}")
        df = sda_query(sql)
        if not df.empty:
            frames.append(df)
        time.sleep(0.2)
    if not frames:
        raise RuntimeError("No SSURGO horizon rows returned.")
    out = pd.concat(frames, ignore_index=True)
    return out


def as_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def dominant_component_top30(df: pd.DataFrame) -> pd.DataFrame:
    numeric_cols = [
        "comppct_r", "resdept_r", "hzdept_r", "hzdepb_r",
        *NUMERIC_PROPS,
    ]
    df = as_numeric(df.copy(), numeric_cols)

    # Dominant component = largest component percentage per mapunit.
    comp_rank = (
        df[["mukey", "cokey", "comppct_r"]]
        .drop_duplicates()
        .sort_values(["mukey", "comppct_r"], ascending=[True, False])
    )
    dominant = comp_rank.drop_duplicates("mukey")[["mukey", "cokey"]]
    d = df.merge(dominant, on=["mukey", "cokey"], how="inner")

    rows = []
    for mukey, g in d.groupby("mukey", sort=False):
        g = g.sort_values("hzdept_r")
        first = g.iloc[0]

        # Horizon overlap thickness with [0,30] cm.
        top = g["hzdept_r"].clip(lower=0)
        bottom = g["hzdepb_r"].clip(upper=30)
        thickness = (bottom - top).clip(lower=0)
        valid_depth = thickness > 0

        row = {
            "mukey": str(mukey),
            "muname": first.get("muname"),
            "cokey_dominant": first.get("cokey"),
            "compname_dominant": first.get("compname"),
            "comppct_r": first.get("comppct_r"),
            "taxorder": first.get("taxorder"),
            "irrcapcl": first.get("irrcapcl"),
            "reskind": first.get("reskind"),
            "resdept_r_cm": first.get("resdept_r"),
            "profile_depth_observed_cm": float(thickness[valid_depth].sum()),
        }

        for prop in NUMERIC_PROPS:
            vals = pd.to_numeric(g[prop], errors="coerce")
            mask = valid_depth & vals.notna()
            if mask.any():
                w = thickness[mask].astype(float)
                row[f"{prop}_0_30cm"] = float(np.average(vals[mask].astype(float), weights=w))
            else:
                row[f"{prop}_0_30cm"] = np.nan
        rows.append(row)

    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-deg", type=float, default=0.5)
    p.add_argument("--batch-size", type=int, default=250)
    p.add_argument("--reuse-mapunits", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    gpkg = OUT_ROOT / "ssurgo_mapunits.gpkg"
    if args.reuse_mapunits and gpkg.exists():
        import geopandas as gpd
        gdf = gpd.read_file(gpkg, layer="mapunits")
    else:
        gdf = download_mapunits(args.tile_deg)

    mukeys = sorted(set(gdf["mukey"].astype(str)))
    print("Unique mukeys:", len(mukeys))

    raw = fetch_horizons(mukeys, args.batch_size)
    raw.to_parquet(OUT_ROOT / "ssurgo_horizons_raw.parquet", index=False)
    raw.to_csv(OUT_ROOT / "ssurgo_horizons_raw.csv", index=False)

    top30 = dominant_component_top30(raw)
    top30.to_parquet(OUT_ROOT / "ssurgo_mapunit_top30cm.parquet", index=False)
    top30.to_csv(OUT_ROOT / "ssurgo_mapunit_top30cm.csv", index=False)

    # Join soil features back to polygons.
    gdf["mukey"] = gdf["mukey"].astype(str)
    joined = gdf.merge(top30, on="mukey", how="left")
    joined.to_file(
        OUT_ROOT / "ssurgo_mapunits_with_top30cm.gpkg",
        layer="mapunits_top30cm",
        driver="GPKG",
    )

    summary = {
        "bbox": BBOX,
        "mapunit_polygons": int(len(gdf)),
        "unique_mukeys": int(len(mukeys)),
        "top30_rows": int(len(top30)),
        "numeric_properties": list(NUMERIC_PROPS),
        "method": "dominant component; horizon-thickness-weighted 0-30 cm",
    }
    (MANIFEST_DIR / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("DONE:", OUT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
