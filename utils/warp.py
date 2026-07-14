"""Warp historic OziExplorer map scans (GIF) into datum-corrected COGs.

Each ``.map`` file carries ground-control points (pixel <-> lon/lat in the map's
own datum) plus the four sheet corners. We attach those as GCPs in the source
datum's geographic CRS (e.g. Pulkovo 1942 = EPSG:4284), then warp the GIF to
EPSG:3857 with a thin-plate-spline transform. GDAL/PROJ applies the datum shift
during the warp, so the resulting Cloud-Optimized GeoTIFFs align with the
datum-corrected footprints produced by :mod:`utils.crs_normalizer`.

The GCP-based model is projection-agnostic: it handles both the Transverse
Mercator and Polyconic sheets uniformly (the GCPs encode the geometry regardless
of the declared projection).

Requires the GDAL command-line tools (``gdal_translate``, ``gdalwarp``) from the
``geo`` conda env. Run:

    python -m utils.warp --map <file.map>                 # single sheet
    python -m utils.warp --base map/Yamal-Nenets --batch  # all sheets
"""

from __future__ import annotations

import os
import sys
import csv
import shutil
import argparse
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:
    from utils.metadata_collector import MetadataCollector
    from utils.crs_normalizer import resolve_datum, normalize_footprints
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils.metadata_collector import MetadataCollector
    from utils.crs_normalizer import resolve_datum, normalize_footprints


# Locate GDAL CLI in the active env (fall back to PATH).
def _gdal_tool(name: str) -> str:
    env_bin = os.path.join(os.path.dirname(sys.executable), name)
    return env_bin if os.path.exists(env_bin) else name


GDAL_TRANSLATE = _gdal_tool("gdal_translate")
GDALWARP = _gdal_tool("gdalwarp")


@dataclass
class WarpResult:
    map_file: str
    ok: bool
    cog_path: Optional[str] = None
    datum: Optional[str] = None
    datum_status: Optional[str] = None
    src_epsg: Optional[int] = None
    n_gcps: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    error: str = ""


def _gif_path(map_file: str, info: dict) -> Optional[str]:
    """Resolve the .gif referenced by a .map, tolerating case differences."""
    if not info.get("gif_file"):
        return None
    d = os.path.dirname(map_file)
    cand = os.path.join(d, info["gif_file"])
    if os.path.exists(cand):
        return cand
    # case-insensitive fallback
    base = os.path.basename(cand).lower()
    for fn in os.listdir(d):
        if fn.lower() == base:
            return os.path.join(d, fn)
    return None


def build_gcps(info: dict) -> List[Tuple[float, float, float, float]]:
    """Collect (pixel_x, pixel_y, lon, lat) GCPs from control points + corners.

    De-duplicates by pixel location so the corner records don't double-count
    points already present as control points.
    """
    seen = set()
    gcps: List[Tuple[float, float, float, float]] = []

    def add(x, y, lon, lat):
        if None in (x, y, lon, lat):
            return
        key = (round(float(x), 1), round(float(y), 1))
        if key in seen:
            return
        seen.add(key)
        gcps.append((float(x), float(y), float(lon), float(lat)))

    for p in info.get("control_points", []) or []:
        add(p.get("x"), p.get("y"), p.get("lon"), p.get("lat"))
    for _, c in (info.get("corners") or {}).items():
        add(c.get("x"), c.get("y"), c.get("lon"), c.get("lat"))
    return gcps


def _write_neatline_cutline(info: dict, path: str) -> bool:
    """Write the datum-corrected sheet neatline (4 MMPLL corners) as a GeoJSON
    polygon in EPSG:4326, for use as a gdalwarp cutline. Returns False if the
    corners can't form a valid footprint."""
    import pandas as pd
    try:
        fp = normalize_footprints(pd.DataFrame([info]))
        if fp.empty or fp.iloc[0].geometry is None or fp.iloc[0].geometry.is_empty:
            return False
        fp.iloc[[0]][["geometry"]].to_file(path, driver="GeoJSON")
        return True
    except Exception:
        return False


def warp_sheet(
    map_file: str,
    out_dir: str,
    dst_srs: str = "EPSG:3857",
    resampling: str = "bilinear",
    overwrite: bool = False,
    crop_to_neatline: bool = True,
    warp_threads: str = "ALL_CPUS",
) -> WarpResult:
    info = MetadataCollector.parse_detailed_map_file(map_file)
    res = WarpResult(map_file=map_file, ok=False, datum=info.get("datum"))

    gif = _gif_path(map_file, info)
    if gif is None:
        res.error = "gif not found"
        return res

    src_crs, status = resolve_datum(info.get("datum"))
    res.datum_status = status
    epsg = src_crs.to_epsg()
    if epsg is None:
        res.error = "source datum has no EPSG code"
        return res
    res.src_epsg = epsg

    gcps = build_gcps(info)
    res.n_gcps = len(gcps)
    if len(gcps) < 3:
        res.error = f"insufficient GCPs ({len(gcps)})"
        return res

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(map_file))[0]
    cog_path = os.path.join(out_dir, stem + ".tif")
    res.cog_path = cog_path
    if os.path.exists(cog_path) and not overwrite:
        res.ok = True  # already done
        return res

    vrt_path = os.path.join(out_dir, stem + ".gcp.vrt")
    cut_path = os.path.join(out_dir, stem + ".cut.geojson")
    tmp_cog = cog_path + ".tmp.tif"

    # 1) attach GCPs + expand palette to RGB via a lightweight VRT
    translate_cmd = [
        GDAL_TRANSLATE, "-q", "-of", "VRT", "-expand", "rgb",
        "-a_srs", f"EPSG:{epsg}",
    ]
    for (px, py, lon, lat) in gcps:
        translate_cmd += ["-gcp", f"{px}", f"{py}", f"{lon}", f"{lat}"]
    translate_cmd += [gif, vrt_path]

    # 2) TPS warp to target CRS (applies datum shift), alpha for out-of-sheet
    warp_cmd = [
        GDALWARP, "-q", "-overwrite", "-tps",
        "-s_srs", f"EPSG:{epsg}", "-t_srs", dst_srs,
        "-r", resampling, "-dstalpha", "-multi", "-wo", f"NUM_THREADS={warp_threads}",
    ]
    # Crop to the sheet neatline so the map collar (graticule labels, legend)
    # doesn't overlap neighbouring sheets when mosaicking.
    use_cut = crop_to_neatline and _write_neatline_cutline(info, cut_path)
    if use_cut:
        warp_cmd += ["-cutline", cut_path, "-cutline_srs", "EPSG:4326",
                     "-crop_to_cutline"]
    warp_cmd += [
        "-of", "COG",
        "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
        "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=BILINEAR",
        vrt_path, tmp_cog,
    ]

    try:
        subprocess.run(translate_cmd, check=True, capture_output=True, text=True)
        subprocess.run(warp_cmd, check=True, capture_output=True, text=True)
        os.replace(tmp_cog, cog_path)
        res.ok = True
    except subprocess.CalledProcessError as e:
        res.error = (e.stderr or e.stdout or str(e)).strip().splitlines()[-1][:300]
        if os.path.exists(tmp_cog):
            os.remove(tmp_cog)
    finally:
        for p in (vrt_path, cut_path):
            if os.path.exists(p):
                os.remove(p)

    # record output dimensions
    if res.ok:
        try:
            import rasterio
            with rasterio.open(cog_path) as ds:
                res.width, res.height = ds.width, ds.height
        except Exception:
            pass
    return res


def _warp_worker(args):
    mf, out_dir, overwrite, warp_threads = args
    return warp_sheet(mf, out_dir, overwrite=overwrite, warp_threads=warp_threads)


def batch(base_dir: str, out_dir: str, overwrite: bool = False, limit: int = 0,
          jobs: int = 1) -> None:
    map_files = []
    for root, _, files in os.walk(base_dir):
        for fn in files:
            if fn.lower().endswith(".map"):
                map_files.append(os.path.join(root, fn))
    map_files.sort()
    if limit:
        map_files = map_files[:limit]

    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "warp_manifest.csv")
    # With multiple workers, cap per-warp threads so we don't oversubscribe cores.
    warp_threads = "ALL_CPUS" if jobs <= 1 else str(max(1, (os.cpu_count() or 4) // jobs))
    total = len(map_files)
    n_ok = n_fail = 0

    with open(manifest_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["map_file", "ok", "cog_path", "datum", "datum_status",
                    "src_epsg", "n_gcps", "width", "height", "error"])

        def record(i, r):
            nonlocal n_ok, n_fail
            w.writerow([r.map_file, r.ok, r.cog_path, r.datum, r.datum_status,
                        r.src_epsg, r.n_gcps, r.width, r.height, r.error])
            fh.flush()
            n_ok += int(r.ok)
            n_fail += int(not r.ok)
            print(f"[{i}/{total}] {'OK ' if r.ok else 'FAIL'} {os.path.basename(r.map_file)}"
                  + ("" if r.ok else f"  <- {r.error}"), flush=True)

        work = [(mf, out_dir, overwrite, warp_threads) for mf in map_files]
        if jobs <= 1:
            for i, args in enumerate(work, 1):
                record(i, _warp_worker(args))
        else:
            from concurrent.futures import ProcessPoolExecutor, as_completed
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                futs = {ex.submit(_warp_worker, a): a for a in work}
                for i, fut in enumerate(as_completed(futs), 1):
                    record(i, fut.result())

    print(f"\nDone: {n_ok} ok, {n_fail} failed of {total}. Manifest -> {manifest_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Warp historic map GIFs to datum-corrected COGs")
    ap.add_argument("--map", help="Single .map file to warp")
    ap.add_argument("--base", default="map/Yamal-Nenets", help="Base folder for --batch")
    ap.add_argument("--out", default="data/historic_cog/Yamal-Nenets", help="Output COG dir")
    ap.add_argument("--batch", action="store_true", help="Warp all sheets under --base")
    ap.add_argument("--overwrite", action="store_true", help="Re-warp existing COGs")
    ap.add_argument("--limit", type=int, default=0, help="Limit #sheets (testing)")
    ap.add_argument("--jobs", type=int, default=1, help="Parallel warp workers")
    args = ap.parse_args()

    if not shutil.which(GDAL_TRANSLATE) and not os.path.exists(GDAL_TRANSLATE):
        sys.exit("gdal_translate not found; activate the geo env")

    if args.batch:
        batch(args.base, args.out, overwrite=args.overwrite, limit=args.limit,
              jobs=args.jobs)
    elif args.map:
        r = warp_sheet(args.map, args.out, overwrite=args.overwrite)
        print(r)
    else:
        ap.error("provide --map <file> or --batch")


if __name__ == "__main__":
    main()
