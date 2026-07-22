"""Tundra historic-map portal API.

FastAPI service tying together the datum-corrected historic footprints/COGs
(Phase 1), the satellite imagery layer, SAM lake segmentation, and morphometric
metrics (Phase 2). titiler is mounted for dynamic COG tiles (historic sheets and
satellite composites), so the React frontend can render everything as map layers.

Run (from repo root, geo env):
    TUNDRA_API_KEY=changeme uvicorn services.api.main:app --host 0.0.0.0 --port 8000
Docs at /docs.
"""

from __future__ import annotations

import os
import sys
import hashlib
import datetime as dt
from typing import List, Optional, Tuple

# Make the repo-root `utils` package importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import geopandas as gpd
from shapely.geometry import box as shp_box
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from titiler.core.factory import TilerFactory

from services.api import config
from services.api.jobs import STORE, Job

app = FastAPI(title="Tundra Portal API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# --- dynamic COG tiles (historic sheets + satellite composites) ---
cog = TilerFactory(router_prefix="/tiles/cog")
app.include_router(cog.router, prefix="/tiles/cog", tags=["tiles"])

# Web-Mercator XYZ template for a COG at `path` (titiler 2.x requires the TMS id).
TILE_TMS = "WebMercatorQuad"


def tile_url(path: str) -> str:
    return f"{config.URL_PREFIX}/tiles/cog/tiles/{TILE_TMS}/{{z}}/{{x}}/{{y}}?url={path}"


# --- auth ---
def require_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# --- footprints (loaded once) ---
_FOOTPRINTS: Optional[gpd.GeoDataFrame] = None


def footprints() -> gpd.GeoDataFrame:
    global _FOOTPRINTS
    if _FOOTPRINTS is None:
        gdf = gpd.read_file(config.FOOTPRINTS_GPKG, layer="footprints")
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        gdf["stem"] = gdf["map_file"].apply(
            lambda p: os.path.splitext(os.path.basename(str(p)))[0])
        gdf["cog"] = gdf["stem"].apply(
            lambda s: os.path.join(config.HISTORIC_COG_DIR, s + ".tif"))
        _FOOTPRINTS = gdf
    return _FOOTPRINTS


def _bbox_parts(bbox: str):
    try:
        lon0, lat0, lon1, lat1 = (float(v) for v in bbox.split(","))
    except Exception:
        raise HTTPException(422, "bbox must be 'lon_min,lat_min,lon_max,lat_max'")
    return lon0, lat0, lon1, lat1


def _area_km2(lon0, lat0, lon1, lat1) -> float:
    lat = (lat0 + lat1) / 2
    return abs(lon1 - lon0) * 111.32 * np.cos(np.deg2rad(lat)) * abs(lat1 - lat0) * 111.32


# --- schemas ---
class ExtractReq(BaseModel):
    bbox: List[float] = Field(..., description="[lon_min,lat_min,lon_max,lat_max]")
    datetime: str = "2023-06-15/2023-09-10"
    sensor: str = "sentinel-2"
    res: Optional[int] = None
    cadence: str = Field(
        "none",
        description="Time-slice the satellite view into steps: "
        "none|annual|seasonal|monthly.")


class SegmentReq(ExtractReq):
    strategy: str = "seeded"
    water_index: str = "mndwi"
    min_area_m2: float = 2000.0
    simplify_px: float = 1.0
    model_type: str = "vit_h"


@app.get("/health")
def health():
    return {"status": "ok",
            "footprints": os.path.exists(config.FOOTPRINTS_GPKG),
            "historic_cog_dir": os.path.isdir(config.HISTORIC_COG_DIR)}


@app.get("/footprints", dependencies=[Depends(require_key)])
def get_footprints(bbox: str = Query(..., description="lon_min,lat_min,lon_max,lat_max"),
                   year_min: Optional[int] = None, year_max: Optional[int] = None):
    """Historic sheet footprints intersecting bbox, as GeoJSON."""
    lon0, lat0, lon1, lat1 = _bbox_parts(bbox)
    gdf = footprints()
    sel = gdf[gdf.intersects(shp_box(lon0, lat0, lon1, lat1))].copy()
    if year_min is not None:
        sel = sel[sel["year"].fillna(0).astype(float) >= year_min]
    if year_max is not None:
        sel = sel[sel["year"].fillna(9999).astype(float) <= year_max]
    sel["cog_exists"] = sel["cog"].apply(os.path.exists)
    cols = ["stem", "year", "scale", "datum", "datum_status", "cog", "cog_exists", "geometry"]
    return sel[[c for c in cols if c in sel.columns]].__geo_interface__


def _historic_sheets(lon0, lat0, lon1, lat1):
    gdf = footprints()
    sel = gdf[gdf.intersects(shp_box(lon0, lat0, lon1, lat1))]
    out = []
    for _, r in sel.iterrows():
        if not os.path.exists(r["cog"]):
            continue
        out.append({"stem": r["stem"],
                    "year": None if r.get("year") is None else int(float(r["year"]))
                    if str(r.get("year")) not in ("", "nan", "None") else None,
                    "tile_url": tile_url(r["cog"])})
    return out


def _composite_path(req: ExtractReq, datetime: Optional[str] = None) -> str:
    dtr = datetime or req.datetime
    key = hashlib.md5(
        f"{req.bbox}|{dtr}|{req.sensor}|{req.res}".encode()).hexdigest()[:12]
    return os.path.join(config.COMPOSITE_DIR, f"{req.sensor}_{key}.tif")


def _parse_range(s: str) -> Tuple[dt.date, dt.date]:
    try:
        a, b = s.split("/")
        return dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    except Exception:
        raise HTTPException(422, "datetime must be 'YYYY-MM-DD/YYYY-MM-DD'")


def _clip(d: dt.date, lo: dt.date, hi: dt.date) -> dt.date:
    return max(lo, min(hi, d))


def _windows(cadence: str, start: dt.date, end: dt.date) -> List[Tuple[str, str]]:
    """Slice [start, end] into (label, 'YYYY-MM-DD/YYYY-MM-DD') time steps.

    annual   — reuse the range's month-day window once per calendar year spanned
               (same season year-over-year), labelled by year.
    monthly  — one step per calendar month, clipped to the range.
    seasonal — one step per calendar quarter (Q1-Q4), clipped to the range.
    """
    if cadence == "annual":
        out = []
        for y in range(start.year, end.year + 1):
            try:
                s, e = start.replace(year=y), end.replace(year=y)
            except ValueError:                     # Feb 29 in a non-leap year
                continue
            if e < s:                              # season crosses New Year
                e = e.replace(year=y + 1)
            out.append((str(y), f"{s.isoformat()}/{e.isoformat()}"))
        return out
    if cadence == "monthly":
        out, y, m = [], start.year, start.month
        while (y, m) <= (end.year, end.month):
            first = dt.date(y, m, 1)
            nxt = dt.date(y + m // 12, m % 12 + 1, 1)
            s, e = _clip(first, start, end), _clip(nxt - dt.timedelta(days=1), start, end)
            out.append((f"{y}-{m:02d}", f"{s.isoformat()}/{e.isoformat()}"))
            y, m = nxt.year, nxt.month
        return out
    if cadence == "seasonal":
        out, y, q = [], start.year, (start.month - 1) // 3 + 1
        while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
            qstart = dt.date(y, (q - 1) * 3 + 1, 1)
            nxt = dt.date(y + (q * 3) // 12, (q * 3) % 12 + 1, 1)
            s, e = _clip(qstart, start, end), _clip(nxt - dt.timedelta(days=1), start, end)
            out.append((f"{y}-Q{q}", f"{s.isoformat()}/{e.isoformat()}"))
            y, q = (y + 1, 1) if q == 4 else (y, q + 1)
        return out
    return []


def _satellite_frame(req: ExtractReq, label: str, dtr: str) -> dict:
    """Build (or reuse) the composite for one time step. A step with no clear
    scenes yields tile_url=None so the client can show a 'no data' slider stop."""
    from utils import imagery
    cpath = _composite_path(req, dtr)
    n_scenes = None
    if not os.path.exists(cpath):
        try:
            comp = imagery.composite(tuple(req.bbox), dtr, sensor=req.sensor, res=req.res)
        except ValueError:
            return {"label": label, "datetime": dtr, "tile_url": None, "n_scenes": 0}
        imagery.write_cog(comp, cpath, bands=("red", "green", "blue"))
        n_scenes = comp.n_scenes
    return {"label": label, "datetime": dtr,
            "tile_url": tile_url(cpath) + "&rescale=0,3000", "n_scenes": n_scenes}


@app.post("/roi/extract", dependencies=[Depends(require_key)])
def extract(req: ExtractReq):
    """Build (or reuse) cloud-free composite COG(s) for the ROI and list the
    overlapping historic sheets, so the client can layer them with time sliders.

    cadence="none" returns one composite over the whole range (top-level
    tile_url). annual|seasonal|monthly instead return a `satellite_frames` series
    the client steps through over time."""
    from utils import imagery
    lon0, lat0, lon1, lat1 = req.bbox
    if _area_km2(lon0, lat0, lon1, lat1) > config.MAX_ROI_KM2:
        raise HTTPException(413, f"ROI exceeds {config.MAX_ROI_KM2} km2 limit")

    cadence = (req.cadence or "none").lower()
    if cadence not in ("none", "annual", "seasonal", "monthly"):
        raise HTTPException(422, "cadence must be none|annual|seasonal|monthly")

    frames: List[dict] = []
    tile: Optional[str] = None
    composite_id: Optional[str] = None
    composite_cog: Optional[str] = None
    n_scenes: Optional[int] = None

    if cadence == "none":
        cpath = _composite_path(req)
        if not os.path.exists(cpath):
            comp = imagery.composite(tuple(req.bbox), req.datetime, sensor=req.sensor,
                                     res=req.res)
            imagery.write_cog(comp, cpath, bands=("red", "green", "blue"))
            n_scenes = comp.n_scenes
        # rescale reflectance*10000 (~0-3000 over land) to 0-255 for natural colour
        tile = tile_url(cpath) + "&rescale=0,3000"
        composite_id, composite_cog = os.path.basename(cpath), cpath
    else:
        start, end = _parse_range(req.datetime)
        wins = _windows(cadence, start, end)
        if not wins:
            raise HTTPException(422, "date range yields no time steps for this cadence")
        if len(wins) > config.MAX_SATELLITE_FRAMES:
            raise HTTPException(
                413, f"{len(wins)} time steps exceed the "
                f"{config.MAX_SATELLITE_FRAMES}-frame limit; narrow the date range "
                "or use a coarser cadence")
        frames = [_satellite_frame(req, label, dtr) for label, dtr in wins]

    return {"composite_id": composite_id,
            "composite_cog": composite_cog,
            "tile_url": tile,
            "n_scenes": n_scenes,
            "cadence": cadence,
            "satellite_frames": frames,
            "historic_sheets": _historic_sheets(lon0, lat0, lon1, lat1)}


def _run_segment(req: SegmentReq):
    from utils import imagery
    from utils.segment import segment_water
    from utils.metrics import lake_metrics

    comp = imagery.composite(tuple(req.bbox), req.datetime, sensor=req.sensor, res=req.res)
    prior = imagery.water_mask(comp, req.water_index, 0.0)
    rgb = comp.rgb(stretch=0.28)
    seg = segment_water(rgb, water_prior=prior, strategy=req.strategy,
                        model_type=req.model_type)
    m = lake_metrics(seg.mask, comp.transform, comp.crs, min_area_m2=req.min_area_m2,
                     simplify_px=req.simplify_px, return_polygons=True)
    polys: gpd.GeoDataFrame = m.pop("polygons")
    polys = polys.to_crs(4326)
    # attach per-lake metrics to polygons for the frontend
    per = {row["label"]: row for row in m["per_lake"]}
    polys["area_m2"] = polys["label"].map(lambda k: per.get(k, {}).get("area_m2"))
    polys["perimeter_m"] = polys["label"].map(lambda k: per.get(k, {}).get("perimeter_m"))
    polys["fractal_dimension"] = polys["label"].map(
        lambda k: per.get(k, {}).get("fractal_dimension"))
    return {"summary": {k: m[k] for k in m if k not in ("per_lake",)},
            "n_scenes": comp.n_scenes,
            "lakes_geojson": polys[["label", "area_m2", "perimeter_m",
                                    "fractal_dimension", "geometry"]].__geo_interface__}


@app.post("/segment", dependencies=[Depends(require_key)])
def segment(req: SegmentReq):
    """Kick off an async lake-segmentation job (SAM is GPU-serialised)."""
    lon0, lat0, lon1, lat1 = req.bbox
    if _area_km2(lon0, lat0, lon1, lat1) > config.MAX_ROI_KM2:
        raise HTTPException(413, f"ROI exceeds {config.MAX_ROI_KM2} km2 limit")
    job = STORE.create()
    STORE.run(job, lambda j: _run_segment(req), serialize_gpu=True)
    return {"job_id": job.id, "status": job.status}


def _run_timeseries(job: Job, req: SegmentReq, wins: List[Tuple[str, str]]):
    """Segment each cadence time-step in turn, publishing per-frame progress and
    an ETA so the client can render a progress bar and a change-over-time chart."""
    import time
    total = len(wins)
    t0 = time.time()

    def _snapshot(done: int, current: str, series: List[dict]):
        elapsed = time.time() - t0
        eta = (elapsed / done) * (total - done) if done else None
        # light series (drop geojson) so polling stays cheap while running
        job.progress = {
            "done": done, "total": total, "current": current,
            "elapsed_s": round(elapsed, 1),
            "eta_s": round(eta, 1) if eta is not None else None,
            "series": [{"label": s["label"], "n_scenes": s["n_scenes"],
                        "summary": s["summary"]} for s in series],
        }

    _snapshot(0, wins[0][0], [])
    series: List[dict] = []
    for i, (label, dtr) in enumerate(wins):
        job.progress = {**job.progress, "current": label}  # mark in-flight
        try:
            r = _run_segment(req.model_copy(update={"datetime": dtr}))
            entry = {"label": label, "datetime": dtr, "n_scenes": r["n_scenes"],
                     "summary": r["summary"], "lakes_geojson": r["lakes_geojson"],
                     "error": None}
        except Exception as e:  # noqa: BLE001 — a cloudy/empty step shouldn't abort the run
            entry = {"label": label, "datetime": dtr, "n_scenes": 0, "summary": None,
                     "lakes_geojson": None, "error": str(e)[:200]}
        series.append(entry)
        _snapshot(i + 1, label, series)
    return {"cadence": req.cadence, "series": series}


@app.post("/segment/timeseries", dependencies=[Depends(require_key)])
def segment_timeseries(req: SegmentReq):
    """Kick off a per-time-step segmentation run (one SAM pass per cadence frame)
    so lake metrics can be charted over time. Requires a non-'none' cadence."""
    lon0, lat0, lon1, lat1 = req.bbox
    if _area_km2(lon0, lat0, lon1, lat1) > config.MAX_ROI_KM2:
        raise HTTPException(413, f"ROI exceeds {config.MAX_ROI_KM2} km2 limit")
    cadence = (req.cadence or "none").lower()
    if cadence == "none":
        raise HTTPException(422, "timeseries requires cadence annual|seasonal|monthly")
    if cadence not in ("annual", "seasonal", "monthly"):
        raise HTTPException(422, "cadence must be annual|seasonal|monthly")
    start, end = _parse_range(req.datetime)
    wins = _windows(cadence, start, end)
    if not wins:
        raise HTTPException(422, "date range yields no time steps for this cadence")
    if len(wins) > config.MAX_SATELLITE_FRAMES:
        raise HTTPException(
            413, f"{len(wins)} time steps exceed the "
            f"{config.MAX_SATELLITE_FRAMES}-frame limit; narrow the date range "
            "or use a coarser cadence")
    job = STORE.create()
    STORE.run(job, lambda j: _run_timeseries(j, req, wins), serialize_gpu=True)
    return {"job_id": job.id, "status": job.status, "total": len(wins)}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def job_status(job_id: str):
    job: Optional[Job] = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"job_id": job.id, "status": job.status, "error": job.error,
            "result": job.result, "progress": job.progress}
