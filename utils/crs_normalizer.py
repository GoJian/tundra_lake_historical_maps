from typing import Dict, Any, Optional, Tuple, List

import geopandas as gpd
from shapely.geometry import Polygon
from pyproj import CRS, Transformer
from pyproj.transformer import AreaOfInterest, TransformerGroup


# --- Datum mapping heuristics -------------------------------------------------

KNOWN_DATUM_ALIASES = {
    # normalized_key: (human_label, EPSG code or WKT)
    "wgs84": ("WGS 84", "EPSG:4326"),
    "wgs 84": ("WGS 84", "EPSG:4326"),
    "wgs_84": ("WGS 84", "EPSG:4326"),
    "pulkovo 1942": ("Pulkovo 1942", "EPSG:4284"),
    "pulkovo 1942 (2)": (
        "Pulkovo 1942",
        "EPSG:4284",
    ),  # treat as 4284 unless specific variant known
    "pulkovo 1942(83)": ("Pulkovo 1942(83)", "EPSG:4179"),
}


def _normalize_key(s: Optional[str]) -> str:
    return (s or "").strip().lower()


# datum_status values describe how confidently a footprint was georeferenced:
#   "native_wgs84"     - datum is WGS 84; no shift needed, coordinates trusted as-is
#   "shifted"          - datum recognized as non-WGS84 (e.g. Pulkovo 1942); a real
#                        datum transformation to WGS84 was applied
#   "unknown_fallback" - datum unrecognized; coordinates were assumed WGS84 WITHOUT
#                        a shift. These are suspect and must be reviewed (they are the
#                        exact failure mode that produced ~105 m overlay errors).
DATUM_STATUS_NATIVE = "native_wgs84"
DATUM_STATUS_SHIFTED = "shifted"
DATUM_STATUS_UNKNOWN = "unknown_fallback"


def resolve_datum(datum: Optional[str]) -> Tuple[CRS, str]:
    """
    Map a datum string (from metadata) to a pyproj CRS and a status label.

    Unlike a bare "default to WGS84", this distinguishes a genuine WGS 84 datum
    from an unrecognized datum that merely fell back to WGS 84 (which silently
    skips the datum shift). The status lets callers flag/QA the suspect ones.
    """
    key = _normalize_key(datum)

    def _status_for(crs: CRS) -> str:
        try:
            return DATUM_STATUS_NATIVE if crs.to_epsg() == 4326 else DATUM_STATUS_SHIFTED
        except Exception:
            return DATUM_STATUS_SHIFTED

    # simple lookup first
    if key in KNOWN_DATUM_ALIASES:
        _, code = KNOWN_DATUM_ALIASES[key]
        crs = CRS.from_user_input(code)
        return crs, _status_for(crs)

    # try parsing by name directly via CRS
    if key:
        try:
            crs = CRS.from_user_input(datum)
            if crs.is_geographic or crs.is_geocentric:
                return crs, _status_for(crs)
        except Exception:
            pass

    # No usable datum: assume WGS84 but flag it as an unverified fallback.
    return CRS.from_epsg(4326), DATUM_STATUS_UNKNOWN


def datum_to_crs(datum: Optional[str]) -> CRS:
    """Map a datum string to a pyproj CRS (WGS84 if unknown). Thin wrapper over
    resolve_datum for backward compatibility; prefer resolve_datum when you also
    need the datum_status."""
    crs, _ = resolve_datum(datum)
    return crs


# --- Transformer selection ----------------------------------------------------


def best_transformer(
    src: CRS, dst: CRS, bbox: Optional[Tuple[float, float, float, float]] = None
) -> Transformer:
    """
    Create a Transformer, optionally guiding with an area-of-interest (lonmin, latmin, lonmax, latmax)
    so that pyproj selects the most accurate pipeline (e.g., grid-based or 7-parameter) available.
    Always uses XY (lon, lat) order.
    """
    aoi = None
    if bbox is not None:
        lonmin, latmin, lonmax, latmax = map(float, bbox)
        aoi = AreaOfInterest(
            west_lon_degree=lonmin,
            south_lat_degree=latmin,
            east_lon_degree=lonmax,
            north_lat_degree=latmax,
        )

    # Prefer TransformerGroup: it enumerates only the *available* pipelines
    # (skipping ops whose grids are missing) ranked best-first, so we
    # deterministically pick the most accurate real datum transform. This
    # avoids Transformer.from_crs's silent fallback to a null/"ballpark"
    # transform (which would skip the datum shift and reintroduce ~105 m error).
    try:
        tg = TransformerGroup(src, dst, always_xy=True, area_of_interest=aoi)
        if tg.transformers:
            return tg.transformers[0]
    except Exception:
        pass

    # Last resort: let pyproj choose (may be a low-accuracy pipeline).
    try:
        return Transformer.from_crs(src, dst, always_xy=True, area_of_interest=aoi)
    except Exception:
        return Transformer.from_crs(src, dst, always_xy=True)


# --- Footprint construction and normalization --------------------------------


def corners_to_polygon(corners: Dict[int, Dict[str, Any]]) -> Optional[Polygon]:
    """
    Build a polygon from corners dict like {1:{lat,lon}, 2:{lat,lon}, 3:{lat,lon}, 4:{lat,lon}}.
    Returns a valid Shapely Polygon or None if insufficient corners.
    Corner order is sorted by key; if that's not correct for some maps, callers can pre-order.
    """
    if not corners:
        return None
    # Collect (lon, lat) pairs
    pts: List[Tuple[float, float]] = []
    for k in sorted(corners.keys()):
        pt = corners.get(k, {})
        if 'lon' in pt and 'lat' in pt:
            pts.append((float(pt['lon']), float(pt['lat'])))
    if len(pts) < 3:
        return None
    # close ring if needed
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)  # try fix self-intersections
        return poly
    except Exception:
        return None


def normalize_footprints(
    df, datum_col: str = 'datum', corners_col: str = 'corners', target: str = 'EPSG:4326'
) -> gpd.GeoDataFrame:
    """
    Given a pandas DataFrame with per-map 'datum' and 'corners', build a GeoDataFrame of
    footprints in a unified target CRS (default WGS84/EPSG:4326).

    - Interprets 'corners' as geographic lon/lat in the source datum's geographic CRS.
    - Applies the best available transformation to the target CRS.
    - Returns a GeoDataFrame with geometry in target CRS and carries original metadata columns.
    """
    import pandas as pd

    dst = CRS.from_user_input(target)
    rows = []
    for _, row in df.iterrows():
        corners = row.get(corners_col)
        poly = corners_to_polygon(corners)
        if poly is None:
            continue
        src_geog, status = resolve_datum(row.get(datum_col))

        # AOI bbox to help pick accurate transform
        minx, miny, maxx, maxy = poly.bounds
        tf = best_transformer(src_geog, dst, bbox=(minx, miny, maxx, maxy))

        # transform all coordinates
        xs, ys = zip(*list(poly.exterior.coords))
        tx, ty = tf.transform(xs, ys)
        tpoly = Polygon(zip(tx, ty))
        rows.append({**row.to_dict(), 'datum_status': status, 'geometry': tpoly})

    gdf = gpd.GeoDataFrame(rows, geometry='geometry', crs=dst)
    return gdf
