"""Georeferencing QA report + footprint re-export for Tundra historic maps.

Parses every OziExplorer ``.map`` file under a base folder, builds datum-corrected
WGS84 footprints, and reports how confidently each sheet was georeferenced. This
is the acceptance check for the datum fix: it proves every sheet received a known,
deliberate datum treatment (no silent WGS84 fallbacks) and that the Pulkovo 1942
shift is applied uniformly rather than to some sheets and not others.

Run:
    python -m utils.qa_report --base map/Yamal-Nenets --export

Outputs (with --export):
    <base>/map_footprints_wgs84.gpkg   (footprints + datum + datum_status + year)
    <base>/map_qa_report.csv           (per-sheet QA rows)
"""

from __future__ import annotations

import os
import sys
import math
import argparse
from typing import Optional

import pandas as pd
import geopandas as gpd

# Support both "python -m utils.qa_report" and "python utils/qa_report.py"
try:
    from utils.metadata_collector import MetadataCollector
    from utils.crs_normalizer import (
        normalize_footprints,
        DATUM_STATUS_UNKNOWN,
        DATUM_STATUS_SHIFTED,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils.metadata_collector import MetadataCollector
    from utils.crs_normalizer import (
        normalize_footprints,
        DATUM_STATUS_UNKNOWN,
        DATUM_STATUS_SHIFTED,
    )


def _first_corner_lonlat(corners) -> Optional[tuple]:
    if not corners:
        return None
    k = min(corners.keys())
    c = corners.get(k, {})
    if "lon" in c and "lat" in c:
        return float(c["lon"]), float(c["lat"])
    return None


def _shift_meters(row) -> Optional[float]:
    """Distance (m) between a footprint's first vertex and its raw (unshifted)
    MMPLL corner. Quantifies the datum correction that was applied."""
    raw = _first_corner_lonlat(row.get("corners"))
    if raw is None or row.geometry is None or row.geometry.is_empty:
        return None
    rlon, rlat = raw
    try:
        glon, glat = list(row.geometry.exterior.coords)[0]
    except Exception:
        return None
    de = (glon - rlon) * 111320.0 * math.cos(math.radians(rlat))
    dn = (glat - rlat) * 111320.0
    return math.hypot(de, dn)


def build_footprints(base_dir: str) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    collector = MetadataCollector(base_dir=base_dir, verbose=False)
    df = collector.collect_all_map_metadata(base_dir)
    df = collector.fill_missing_metadata(df)  # fills year/scale/index (datum already parsed)
    footprints = normalize_footprints(df, datum_col="datum", corners_col="corners")
    return df, footprints


def report(base_dir: str, export: bool = False) -> gpd.GeoDataFrame:
    df, fp = build_footprints(base_dir)
    fp = fp.copy()
    fp["shift_m"] = fp.apply(_shift_meters, axis=1)

    n_maps = len(df)
    n_placed = len(fp)
    n_dropped = n_maps - n_placed

    print("=" * 68)
    print(f"Georeferencing QA — {base_dir}")
    print("=" * 68)
    print(f"  .map files parsed : {n_maps}")
    print(f"  footprints placed : {n_placed}")
    print(f"  dropped (no valid geometry) : {n_dropped}")
    print()
    print("  datum_status breakdown:")
    status_counts = fp["datum_status"].value_counts(dropna=False)
    for status, count in status_counts.items():
        print(f"    {str(status):18s} {count:5d}")
    print()
    print("  applied datum shift (m) by status:")
    for status, sub in fp.groupby("datum_status"):
        s = sub["shift_m"].dropna()
        if len(s):
            print(
                f"    {str(status):18s} n={len(s):4d}  "
                f"min={s.min():6.1f}  median={s.median():6.1f}  max={s.max():6.1f}"
            )

    # Acceptance checks
    n_unknown = int(status_counts.get(DATUM_STATUS_UNKNOWN, 0))
    print()
    print("  ACCEPTANCE CHECKS:")
    ok_placed = n_placed >= n_maps  # expect all sheets to place
    ok_known = n_unknown == 0
    # Shifted sheets should actually move (a real datum transform, not a null op)
    shifted = fp[fp["datum_status"] == DATUM_STATUS_SHIFTED]["shift_m"].dropna()
    ok_shift = bool(len(shifted)) and float(shifted.min()) > 1.0
    print(f"    [{'PASS' if ok_placed else 'FAIL'}] all sheets placed ({n_placed}/{n_maps})")
    print(f"    [{'PASS' if ok_known else 'FAIL'}] no unknown-datum fallbacks ({n_unknown})")
    print(
        f"    [{'PASS' if ok_shift else 'FAIL'}] shifted sheets received a real datum shift "
        f"(min {float(shifted.min()) if len(shifted) else 0:.1f} m)"
    )

    if n_unknown:
        print("\n  Sheets needing review (unknown_fallback):")
        for mf in fp.loc[fp["datum_status"] == DATUM_STATUS_UNKNOWN, "map_file"].head(20):
            print(f"    - {os.path.basename(str(mf))}")

    if export:
        _export(base_dir, fp)

    return fp


def _export(base_dir: str, fp: gpd.GeoDataFrame) -> None:
    gpkg_path = os.path.join(base_dir, "map_footprints_wgs84.gpkg")
    csv_path = os.path.join(base_dir, "map_qa_report.csv")

    # Keep only serializable columns for the GPKG (drop nested dict/list columns).
    keep = [
        c
        for c in ["map_file", "gif_file", "year", "scale", "index", "datum",
                  "datum_status", "projection", "image_width", "image_height",
                  "shift_m", "geometry"]
        if c in fp.columns
    ]
    out = fp[keep].copy()
    out.to_file(gpkg_path, layer="footprints", driver="GPKG")
    print(f"\n  Wrote footprints -> {gpkg_path} ({len(out)} features)")

    csv_cols = [c for c in keep if c != "geometry"]
    fp[csv_cols].to_csv(csv_path, index=False)
    print(f"  Wrote QA table   -> {csv_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Georeferencing QA + footprint re-export")
    ap.add_argument("--base", default="map/Yamal-Nenets", help="Base folder with .map files")
    ap.add_argument("--export", action="store_true", help="Write GPKG + CSV outputs")
    args = ap.parse_args()
    report(args.base, export=args.export)


if __name__ == "__main__":
    main()
