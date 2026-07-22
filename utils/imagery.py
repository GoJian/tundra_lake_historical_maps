"""Modern satellite imagery for ROIs: STAC search + cloud-free composites.

Uses the Microsoft Planetary Computer STAC API to find Landsat Collection-2 L2
and Sentinel-2 L2A scenes over a region of interest and date range, masks clouds
(Landsat QA_PIXEL bit flags / Sentinel-2 SCL classes), reprojects each scene onto
a common target grid with lazy windowed reads (rasterio WarpedVRT over /vsicurl),
and reduces the time stack to a per-pixel median composite. Also provides NDWI /
MNDWI water-index helpers used to constrain lake segmentation.

Design choice (per project): lightweight rasterio windowed reads, no xarray/dask.

    from utils.imagery import composite, ndwi, mndwi
    comp = composite(bbox=(72.0,66.0,72.5,66.3), datetime="2023-06-01/2023-09-15",
                     sensor="sentinel-2", res=10)
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

import pystac_client
import planetary_computer

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Scene reads are network-bound (/vsicurl over HTTPS), so composite them in
# parallel. Bounded to avoid exhausting sockets / memory on large ROIs.
_COMPOSITE_WORKERS = int(os.environ.get("TUNDRA_COMPOSITE_WORKERS", "8"))

# GDAL options for efficient remote COG reads over /vsicurl.
_GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
)

# Logical band -> STAC asset key, per sensor.
_ASSETS = {
    "landsat": {"collection": "landsat-c2-l2",
                "bands": {"blue": "blue", "green": "green", "red": "red",
                          "nir": "nir08", "swir16": "swir16", "swir22": "swir22"},
                "mask": "qa_pixel",
                "scale": 2.75e-5, "offset": -0.2, "default_res": 30},
    "sentinel-2": {"collection": "sentinel-2-l2a",
                   "bands": {"blue": "B02", "green": "B03", "red": "B04",
                             "nir": "B08", "swir16": "B11", "swir22": "B12"},
                   "mask": "SCL",
                   "scale": 1e-4, "offset": 0.0, "default_res": 10},
}


@dataclass
class Composite:
    bands: Dict[str, np.ndarray]          # logical band -> 2D float32 reflectance
    transform: rasterio.Affine
    crs: CRS
    sensor: str
    n_scenes: int
    datetime: str
    bbox: Tuple[float, float, float, float]

    def stack(self, order: Sequence[str]) -> np.ndarray:
        return np.stack([self.bands[b] for b in order], axis=0)

    def rgb(self, gamma: float = 1.0, stretch: float = 0.3) -> np.ndarray:
        """uint8 H,W,3 true-colour view for SAM / display."""
        r, g, b = self.bands["red"], self.bands["green"], self.bands["blue"]
        arr = np.stack([r, g, b], axis=-1)
        arr = np.clip(arr / stretch, 0, 1) ** gamma
        return (np.nan_to_num(arr) * 255).astype("uint8")


def open_catalog() -> pystac_client.Client:
    return pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)


def search(bbox, datetime, sensor="sentinel-2", max_cloud=20, limit=100) -> List:
    """Return signed STAC items over bbox/date, sorted least-cloudy first."""
    cfg = _ASSETS[sensor]
    cat = open_catalog()
    q = {"eo:cloud_cover": {"lt": max_cloud}}
    items = list(cat.search(collections=[cfg["collection"]], bbox=bbox,
                            datetime=datetime, query=q, max_items=limit).items())
    items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
    return items


def _target_grid(bbox, res, dst_crs="EPSG:3857"):
    """Build (transform, width, height) for bbox at res metres in dst_crs."""
    dst = CRS.from_user_input(dst_crs)
    l, b, r, t = transform_bounds("EPSG:4326", dst, *bbox, densify_pts=21)
    width = max(1, int(round((r - l) / res)))
    height = max(1, int(round((t - b) / res)))
    return from_origin(l, t, res, res), width, height, dst


def _read_band(href, dst_crs, transform, width, height, resampling=Resampling.bilinear):
    with rasterio.open(href) as src:
        with WarpedVRT(src, crs=dst_crs, transform=transform, width=width,
                       height=height, resampling=resampling) as vrt:
            return vrt.read(1).astype("float32")


def _cloud_mask(item, sensor, dst_crs, transform, width, height) -> np.ndarray:
    """Boolean array: True where pixel is CLEAR (keep)."""
    cfg = _ASSETS[sensor]
    href = item.assets[cfg["mask"]].href
    qa = _read_band(href, dst_crs, transform, width, height, Resampling.nearest)
    if sensor == "landsat":
        qa = qa.astype("uint16")
        # QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow
        bad = ((qa >> 1) & 1) | ((qa >> 2) & 1) | ((qa >> 3) & 1) | ((qa >> 4) & 1)
        return bad == 0
    else:  # sentinel-2 SCL: keep 4 veg,5 bare,6 water,7 unclassified,11 snow? keep water+land
        scl = qa.astype("uint8")
        keep = np.isin(scl, [4, 5, 6, 7])  # veg, bare, water, unclassified
        return keep


def _process_scene(it, sensor, dst_crs, transform, width, height, bands, cfg):
    """Read + cloud-mask one scene's bands over the network. Returns a
    ``{band: masked reflectance array}`` dict, or ``None`` if the scene is
    essentially cloudy over the ROI. Runs in a worker thread, so it opens its
    own GDAL environment (rasterio.Env is thread-local)."""
    with rasterio.Env(**_GDAL_ENV):
        clear = _cloud_mask(it, sensor, dst_crs, transform, width, height)
        if clear.mean() < 0.02:              # scene essentially cloudy over ROI
            return None
        out = {}
        for b in bands:
            href = it.assets[cfg["bands"][b]].href
            arr = _read_band(href, dst_crs, transform, width, height)
            arr = arr * cfg["scale"] + cfg["offset"]         # -> reflectance
            arr[~clear] = np.nan
            arr[(arr < -0.1) | (arr > 1.6)] = np.nan           # sanity clip
            out[b] = arr
        return out


def composite(bbox, datetime, sensor="sentinel-2", res=None, dst_crs="EPSG:3857",
              bands=("blue", "green", "red", "nir", "swir16"),
              max_cloud=20, max_items=25, on_progress=None) -> Composite:
    """Cloud-free per-pixel median composite over bbox/date for the given sensor.

    Scenes are read concurrently (``TUNDRA_COMPOSITE_WORKERS`` threads), since the
    work is dominated by HTTPS range reads over /vsicurl.

    ``on_progress``: optional callback invoked with a dict describing the current
    stage — ``{"stage": "search"}`` before the STAC query, then per completed
    scene ``{"stage": "compose", "done": i, "total": n, "used": k}`` — so callers
    can surface a progress bar / ETA over the network-bound work.
    """
    cfg = _ASSETS[sensor]
    res = res or cfg["default_res"]
    transform, width, height, dst = _target_grid(bbox, res, dst_crs)

    if on_progress:
        on_progress({"stage": "search", "done": 0, "total": 0, "used": 0})
    items = search(bbox, datetime, sensor, max_cloud, limit=max_items)
    if not items:
        raise ValueError(f"No {sensor} scenes for bbox={bbox} datetime={datetime} "
                         f"cloud<{max_cloud}")

    stacks = {b: [] for b in bands}
    n_used = 0
    done = 0
    total = len(items)
    workers = max(1, min(_COMPOSITE_WORKERS, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_process_scene, it, sensor, dst, transform, width,
                            height, bands, cfg) for it in items]
        for fut in as_completed(futs):
            done += 1
            try:
                res_bands = fut.result()
            except Exception:
                res_bands = None
            if res_bands is not None:
                for b in bands:
                    stacks[b].append(res_bands[b])
                n_used += 1
            if on_progress:
                on_progress({"stage": "compose", "done": done, "total": total,
                             "used": n_used})

    if n_used == 0:
        raise ValueError("All candidate scenes were too cloudy over the ROI.")

    with np.errstate(all="ignore"):
        med = {b: np.nanmedian(np.stack(stacks[b], 0), 0).astype("float32")
               for b in bands}
    return Composite(med, transform, dst, sensor, n_used, datetime, tuple(bbox))


# --- water indices -----------------------------------------------------------

def _norm_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        return (a - b) / (a + b)


def ndwi(comp: Composite) -> np.ndarray:
    """McFeeters NDWI = (green - nir)/(green + nir); water > 0."""
    return _norm_diff(comp.bands["green"], comp.bands["nir"])


def mndwi(comp: Composite) -> np.ndarray:
    """Modified NDWI = (green - swir16)/(green + swir16); better for turbid lakes."""
    return _norm_diff(comp.bands["green"], comp.bands["swir16"])


def water_mask(comp: Composite, index="mndwi", threshold=0.0) -> np.ndarray:
    idx = mndwi(comp) if index == "mndwi" else ndwi(comp)
    return np.nan_to_num(idx, nan=-1.0) > threshold


def write_cog(comp: Composite, path: str, bands=("red", "green", "blue")) -> str:
    """Write selected composite bands as a COG (reflectance scaled to uint16)."""
    data = np.stack([comp.bands[b] for b in bands], 0)
    data = np.clip(np.nan_to_num(data) * 10000, 0, 65535).astype("uint16")
    profile = dict(driver="COG", dtype="uint16", count=len(bands),
                   height=comp.transform and data.shape[1], width=data.shape[2],
                   crs=comp.crs, transform=comp.transform, compress="DEFLATE",
                   nodata=0)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        dst.descriptions = tuple(bands)
    return path
