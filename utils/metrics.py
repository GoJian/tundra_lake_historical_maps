"""Lake morphometric metrics from a binary segmentation mask.

Given a boolean lake mask plus its affine transform and CRS (as produced by the
segmentation step on a satellite composite or a warped historic COG), compute
per-lake and aggregate metrics:

  * lake_count            number of distinct water bodies
  * total_area_m2         summed lake area
  * per-lake area_m2, perimeter_m
  * fractal_dimension     box-counting dimension of each lake's boundary
  * size distribution     histogram of lake areas

Areas/perimeters are measured in a local equal-area/UTM CRS (not EPSG:3857,
whose area is inflated ~6x at 66 deg N), so numbers are physically meaningful.
The fractal dimension is computed in pixel space (dimensionless, so CRS-invariant).

Depends only on numpy/scipy/rasterio/shapely/geopandas (all in the `geo` env).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Dict, Any, List

import numpy as np
from scipy import ndimage as ndi

import rasterio.features
from rasterio.transform import Affine
from shapely.geometry import shape
import geopandas as gpd


ALL_METRICS = (
    "lake_count",
    "total_area_m2",
    "area_m2",
    "perimeter_m",
    "fractal_dimension",
    "size_distribution",
)


def box_count_dimension(boundary: np.ndarray, n_sizes: int = 8) -> float:
    """Minkowski-Bouligand (box-counting) dimension of a boolean boundary image.

    Returns NaN if the boundary is too small to fit a reliable line.
    """
    Z = np.asarray(boundary, dtype=bool)
    if Z.sum() < 8:
        return float("nan")
    min_dim = min(Z.shape)
    if min_dim < 8:
        return float("nan")

    # Box sizes as powers of two, from 2 px up to ~half the smaller dimension.
    max_k = int(np.floor(np.log2(min_dim // 2))) if min_dim >= 4 else 1
    sizes = [2 ** k for k in range(1, max_k + 1)][:n_sizes]
    if len(sizes) < 3:
        return float("nan")

    counts = []
    for s in sizes:
        S = np.add.reduceat(
            np.add.reduceat(Z, np.arange(0, Z.shape[0], s), axis=0),
            np.arange(0, Z.shape[1], s), axis=1,
        )
        counts.append(int(np.count_nonzero(S)))

    counts = np.asarray(counts, dtype=float)
    sizes_a = np.asarray(sizes, dtype=float)
    good = counts > 0
    if good.sum() < 3:
        return float("nan")
    # log N(s) = -D log(s) + c  ->  D = -slope
    slope = np.polyfit(np.log(sizes_a[good]), np.log(counts[good]), 1)[0]
    return float(-slope)


def _lake_boundary(lake_mask: np.ndarray) -> np.ndarray:
    """1-px inner boundary of a boolean blob."""
    eroded = ndi.binary_erosion(lake_mask, border_value=0)
    return lake_mask & ~eroded


def _metric_crs(gdf: gpd.GeoDataFrame, metric_crs: Optional[str]) -> gpd.GeoDataFrame:
    """Reproject polygons to an equal-area/UTM CRS for honest area/perimeter."""
    if metric_crs is not None:
        return gdf.to_crs(metric_crs)
    try:
        utm = gdf.estimate_utm_crs()
        return gdf.to_crs(utm)
    except Exception:
        # Fall back to a Lambert azimuthal equal-area centred on the data.
        c = gdf.to_crs(4326).union_all().centroid
        laea = f"+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m +no_defs"
        return gdf.to_crs(laea)


def lake_metrics(
    mask: np.ndarray,
    transform: Affine,
    crs: Any,
    *,
    min_area_m2: float = 0.0,
    connectivity: int = 8,
    fractal_min_pixels: int = 400,
    simplify_px: float = 0.0,
    metrics: Optional[Sequence[str]] = None,
    metric_crs: Optional[str] = None,
    size_bins: Optional[Sequence[float]] = None,
    return_polygons: bool = False,
) -> Dict[str, Any]:
    """Compute lake metrics from a binary mask.

    Parameters
    ----------
    mask : 2D array, truthy where water/lake.
    transform, crs : georeferencing of ``mask`` (rasterio Affine + CRS).
    min_area_m2 : drop lakes smaller than this (noise filter).
    connectivity : 4 or 8 for connected-component labelling.
    fractal_min_pixels : only compute fractal D for lakes at least this many px.
    simplify_px : Douglas-Peucker tolerance (in pixels) applied before measuring
        area/perimeter, to reduce the pixel "staircase" that inflates raster
        perimeters (~27% for a disk). 0 = faithful to the mask. Fractal
        dimension always uses the raw pixel boundary, so it is unaffected.
    metrics : subset of ALL_METRICS to compute (default: all).
    metric_crs : CRS for area/perimeter (default: auto UTM).
    return_polygons : also return a GeoDataFrame of lake polygons (source CRS).
    """
    want = set(metrics) if metrics is not None else set(ALL_METRICS)
    mask = np.asarray(mask).astype(bool)

    out: Dict[str, Any] = {}
    if not mask.any():
        out.update(lake_count=0, total_area_m2=0.0, per_lake=[], size_distribution=None)
        if return_polygons:
            out["polygons"] = gpd.GeoDataFrame(geometry=[], crs=crs)
        return out

    # --- vectorize each connected water body in source CRS ---
    structure = ndi.generate_binary_structure(2, 2 if connectivity == 8 else 1)
    labels, n = ndi.label(mask, structure=structure)
    geoms, lab_ids = [], []
    for geom, val in rasterio.features.shapes(
        labels.astype(np.int32), mask=mask, transform=transform, connectivity=connectivity
    ):
        geoms.append(shape(geom))
        lab_ids.append(int(val))
    gdf = gpd.GeoDataFrame({"label": lab_ids}, geometry=geoms, crs=crs)
    # dissolve any multi-part rings sharing a label (holes handled by shapes())
    gdf = gdf.dissolve(by="label", as_index=False)

    # --- honest area/perimeter in a metric CRS ---
    mgdf = _metric_crs(gdf, metric_crs)
    if simplify_px and simplify_px > 0:
        px_size = float(np.hypot(transform.a, transform.b))  # source px size in CRS units
        # scale tolerance into the metric CRS using the polygon's area ratio
        ratio = np.sqrt(max(mgdf.geometry.area.sum(), 1e-9) / max(gdf.geometry.area.sum(), 1e-9))
        mgdf = mgdf.copy()
        mgdf["geometry"] = mgdf.geometry.simplify(simplify_px * px_size * ratio,
                                                  preserve_topology=True)
    gdf["area_m2"] = mgdf.geometry.area.values
    gdf["perimeter_m"] = mgdf.geometry.length.values

    if min_area_m2 > 0:
        keep = gdf["area_m2"] >= min_area_m2
        gdf = gdf[keep].reset_index(drop=True)
    if gdf.empty:
        out.update(lake_count=0, total_area_m2=0.0, per_lake=[], size_distribution=None)
        if return_polygons:
            out["polygons"] = gdf
        return out

    # --- per-lake fractal dimension (pixel space, dimensionless) ---
    if "fractal_dimension" in want:
        fds = []
        for lab in gdf["label"]:
            lm = labels == lab
            if lm.sum() < fractal_min_pixels:
                fds.append(float("nan"))
                continue
            ys, xs = np.where(lm)
            sub = lm[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            fds.append(box_count_dimension(_lake_boundary(sub)))
        gdf["fractal_dimension"] = fds

    # --- assemble output ---
    per_cols = ["label", "area_m2", "perimeter_m"]
    if "fractal_dimension" in want:
        per_cols.append("fractal_dimension")
    per_lake = gdf[per_cols].sort_values("area_m2", ascending=False).to_dict("records")

    out["lake_count"] = int(len(gdf))
    out["total_area_m2"] = float(gdf["area_m2"].sum())
    out["per_lake"] = per_lake

    if "size_distribution" in want:
        areas = gdf["area_m2"].to_numpy()
        if size_bins is None:
            # log-spaced bins spanning the observed range
            lo = max(areas.min(), 1.0)
            hi = max(areas.max(), lo * 10)
            size_bins = np.logspace(np.log10(lo), np.log10(hi), 12)
        counts, edges = np.histogram(areas, bins=size_bins)
        out["size_distribution"] = {"bin_edges_m2": edges.tolist(),
                                    "counts": counts.tolist()}

    if "fractal_dimension" in want:
        fd = gdf["fractal_dimension"].to_numpy()
        fd = fd[np.isfinite(fd)]
        out["mean_fractal_dimension"] = float(fd.mean()) if fd.size else float("nan")

    if return_polygons:
        out["polygons"] = gdf

    return out
