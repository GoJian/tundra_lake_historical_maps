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
from typing import List, Optional

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
    return f"/tiles/cog/tiles/{TILE_TMS}/{{z}}/{{x}}/{{y}}?url={path}"


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


def _composite_path(req: ExtractReq) -> str:
    key = hashlib.md5(
        f"{req.bbox}|{req.datetime}|{req.sensor}|{req.res}".encode()).hexdigest()[:12]
    return os.path.join(config.COMPOSITE_DIR, f"{req.sensor}_{key}.tif")


@app.post("/roi/extract", dependencies=[Depends(require_key)])
def extract(req: ExtractReq):
    """Build (or reuse) a cloud-free composite COG for the ROI and list the
    overlapping historic sheets, so the client can layer them with a time slider."""
    from utils import imagery
    lon0, lat0, lon1, lat1 = req.bbox
    if _area_km2(lon0, lat0, lon1, lat1) > config.MAX_ROI_KM2:
        raise HTTPException(413, f"ROI exceeds {config.MAX_ROI_KM2} km2 limit")

    cpath = _composite_path(req)
    if not os.path.exists(cpath):
        comp = imagery.composite(tuple(req.bbox), req.datetime, sensor=req.sensor,
                                 res=req.res)
        imagery.write_cog(comp, cpath, bands=("red", "green", "blue"))
        n_scenes = comp.n_scenes
    else:
        n_scenes = None

    # rescale reflectance*10000 (~0-3000 over land) to 0-255 for natural colour
    return {"composite_id": os.path.basename(cpath),
            "composite_cog": cpath,
            "tile_url": tile_url(cpath) + "&rescale=0,3000",
            "n_scenes": n_scenes,
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


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def job_status(job_id: str):
    job: Optional[Job] = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"job_id": job.id, "status": job.status, "error": job.error,
            "result": job.result}
