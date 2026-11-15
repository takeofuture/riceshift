#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
30_riceshift_data_preflight.py

Inventory the RiceShift data-preparation outputs before modeling begins.
Produces a concise coverage report; it does not compute recommendations.

Checks:
- existing FOSS4G/CDL/PRISM/MODIS data folders
- new NASA RiceShift folders (POWER/SMAP/GPM/ECOSTRESS)
- rotation-history outputs
- SSURGO outputs
- estimated common analysis windows

The report helps decide which data are actually ready when implementation starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
DATA = PROJECT_DIR / "data"
PROCESSED = PROJECT_DIR / "processed"
OUT = PROCESSED / "30_riceshift_preflight"
OUT.mkdir(parents=True, exist_ok=True)

YEAR_RE = re.compile(r"(20\d{2})")


def count_files(root: Path, suffixes=None) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    files = [p for p in root.rglob("*") if p.is_file()]
    if suffixes:
        suffixes = {s.lower() for s in suffixes}
        files = [p for p in files if p.suffix.lower() in suffixes]
    return len(files), sum(p.stat().st_size for p in files)


def years_in_paths(root: Path) -> list[int]:
    if not root.exists():
        return []
    ys = set()
    for p in root.rglob("*"):
        for m in YEAR_RE.findall(p.name):
            y = int(m)
            if 1980 <= y <= 2030:
                ys.add(y)
        for part in p.parts:
            if part.isdigit() and len(part) == 4:
                y = int(part)
                if 1980 <= y <= 2030:
                    ys.add(y)
    return sorted(ys)


def main() -> int:
    specs = [
        ("MODIS existing", DATA / "nasa_modis", None),
        ("CDL existing", DATA / "usda_cdl_california", {".tif", ".tiff"}),
        ("PRISM existing", DATA / "prism_daily_4km", {".zip"}),
        ("POWER new", DATA / "riceshift_nasa" / "power_daily", {".nc"}),
        ("SMAP new", DATA / "riceshift_nasa" / "smap_california", None),
        ("GPM new", DATA / "riceshift_nasa" / "gpm", None),
        ("ECOSTRESS LST new", DATA / "riceshift_nasa" / "ecostress_lst", None),
        ("ECOSTRESS ET new", DATA / "riceshift_nasa" / "ecostress_et", None),
        ("Rotation history", PROCESSED / "28_riceshift_rotation_history", None),
        ("SSURGO", PROCESSED / "29_riceshift_ssurgo", None),
    ]

    rows = []
    for name, root, suffixes in specs:
        n, b = count_files(root, suffixes)
        ys = years_in_paths(root)
        rows.append({
            "dataset": name,
            "path": str(root),
            "exists": root.exists(),
            "files": n,
            "size_gb": round(b / 1024**3, 3),
            "year_min": min(ys) if ys else None,
            "year_max": max(ys) if ys else None,
            "years_detected": ",".join(map(str, ys)),
        })

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "data_coverage.csv", index=False)

    # Recommended logical windows based on source availability.
    windows = pd.DataFrame([
        {
            "window": "paper_replication",
            "start": 2007,
            "end": 2021,
            "purpose": "Salvato et al. 15-year CDL rotation definitions + SSURGO",
        },
        {
            "window": "extended_rotation_history",
            "start": 2007,
            "end": 2025,
            "purpose": "Long public CDL history / FOSS4G continuity",
        },
        {
            "window": "core_nasa_fusion",
            "start": 2015,
            "end": 2025,
            "purpose": "SMAP + POWER + MODIS/CDL/SSURGO common period",
        },
        {
            "window": "ecostress_optional_not_downloaded",
            "start": 2018,
            "end": 2025,
            "purpose": "Optional ECOSTRESS high-resolution LST/ET; not downloaded in this run",
        },
    ])
    windows.to_csv(OUT / "recommended_windows.csv", index=False)

    summary = {
        "core_window": "2015-2025",
        "paper_window": "2007-2021",
        "ecostress_status": "not downloaded",
        "note": "Coverage is inferred from local file paths; inspect inventories for exact dates.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(df.to_string(index=False))
    print("\nReports:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
