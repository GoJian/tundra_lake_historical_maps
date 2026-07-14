# st_app_historical_map.py

# Streamlit app: Overlap-aware historical map coverage (MapLibre/Mapbox + Plotly)
# - Reads all KMLs under a base folder (including multi-layer KMLs)
# - Builds "cells" with uniform overlap using Shapely polygonize in EPSG:3857 (meters)
# - Hover lists ALL overlapping layer names/files at the cursor (newline separated)
# - Legend hidden; translucent fills visually darken with overlap; supports interior holes

import os
import sys
import fiona
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.ops import unary_union, polygonize
from shapely.geometry import box

# shapely.geometry Polygon/MultiPolygon not needed directly; using geom_type checks instead
import plotly.graph_objects as go
import streamlit as st
from typing import cast
import time

# Make the repo-root `utils` package importable when run as `streamlit run streamlit_app/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# For generating/reading preprocessed footprints
from utils.metadata_collector import MetadataCollector
from utils.crs_normalizer import normalize_footprints


# ---------------- Streamlit UI ----------------
st.set_page_config(page_title="Historical Map Viewer", layout="wide")

# Initialize persistent session state to survive reruns (e.g., after drawing)
st.session_state.setdefault("built", False)
st.session_state.setdefault("combined_gdf", None)
st.session_state.setdefault("cells_gdf", None)
st.session_state.setdefault("selection_bbox", None)
st.session_state.setdefault("drawn_bbox_pending", None)

st.title("🗺️ Historical Map Viewer — Yamal-Nenets Region")

with st.sidebar:
    st.header("Settings")

    base_folder = st.text_input(
        "Base folder containing .kml files",
        value="map/Yamal-Nenets",
        help="All .kml files under this folder will be loaded recursively.",
    )

    # Map appearance
    st.session_state.setdefault("combined_gdf", None)
    st.session_state.setdefault("cells_gdf", None)
    st.session_state.setdefault("selection_bbox", None)
    with st.expander("Map appearance", expanded=False):
        base_choice = st.selectbox(
            "Basemap",
            options=[
                "USGS Imagery (no token)",
                "open-street-map",
                "carto-positron",
                "dark",
                "light",
            ],
            index=0,
            help=(
                "All listed styles work without tokens. USGS Imagery uses raster tiles, the others are MapLibre styles."
            ),
        )

        zoom = st.slider("Zoom", 1, 12, 3)
        center_lat = st.number_input("Center latitude", value=66.5, format="%.6f")
        center_lon = st.number_input("Center longitude", value=75.0, format="%.6f")

    with st.expander("Styling", expanded=False):
        fill_alpha = st.slider(
            "Fill alpha (0.05-0.5)", min_value=0.05, max_value=0.5, value=0.20, step=0.01
        )
        trace_opacity = st.slider(
            "Trace opacity (0.2-1.0)", min_value=0.2, max_value=1.0, value=0.65, step=0.05
        )
        line_width = st.slider("Outline width (px)", 0, 4, 1)

    with st.expander("Performance", expanded=False):
        simplify_tol = st.number_input(
            "Geometry simplify tolerance (meters, Web Mercator)",
            min_value=0.0,
            value=0.0,
            step=10.0,
            help=(
                "Optionally simplify polygons before polygonizing to speed things up. "
                "Measured in meters because the topology ops run in EPSG:3857. (0 = no simplify)."
            ),
        )

        min_cell_area = st.number_input(
            "Minimum cell area to keep (m²)",
            min_value=0.0,
            value=5.0,
            step=5.0,
            help="Filter out tiny sliver cells after polygonization (area in square meters).",
        )

        max_cells_to_render = st.number_input(
            "Max cells to render (0 = no limit)",
            min_value=0,
            value=5000,
            step=500,
            help="Limit how many cells are drawn to keep the browser responsive. Rendering tens of thousands of polygons can stall the UI.",
        )

        render_only_selection = st.checkbox(
            "Render only within selection bbox (if set)",
            value=True,
            help="When enabled and a selection bbox exists, only cells intersecting the bbox are drawn.",
        )

        show_diagnostics = st.checkbox(
            "Show diagnostics (timings, counts)",
            value=False,
            help="Print step-by-step timings and object sizes to identify bottlenecks.",
        )

    # Choose data source (hidden by default like other controls)
    with st.expander("Data source", expanded=False):
        data_source = st.selectbox(
            "Data source",
            options=["Footprints (faster)", "KML folder (slow)"],
            index=0,
            help="Prefer the processed footprints dataset when available; the KML scan is slower.",
        )

    run_btn = st.button("Build / Rebuild Overlap Map", type="primary")

    st.subheader("Selection")
    selection_method = st.selectbox(
        "Selection method", options=["None", "Manual bounding box", "Center + span (km)"]
    )
    clear_sel = st.button("Clear selection", key="clear_selection")

    manual_bbox = None
    center_span = None
    if selection_method == "Manual bounding box":
        col_a, col_b = st.columns(2)
        with col_a:
            lon_min = st.number_input("Lon min", value=center_lon - 1.0, format="%.6f")
            lat_min = st.number_input("Lat min", value=center_lat - 0.5, format="%.6f")
        with col_b:
            lon_max = st.number_input("Lon max", value=center_lon + 1.0, format="%.6f")
            lat_max = st.number_input("Lat max", value=center_lat + 0.5, format="%.6f")
        if lon_max < lon_min:
            st.warning("Lon max is less than Lon min; swapping on apply.")
        if lat_max < lat_min:
            st.warning("Lat max is less than Lat min; swapping on apply.")
        manual_bbox = (lon_min, lat_min, lon_max, lat_max)
        if st.button("Apply manual bbox", key="apply_manual_bbox"):
            a, b, c, d = manual_bbox
            lon_min2, lon_max2 = (a, c) if a <= c else (c, a)
            lat_min2, lat_max2 = (b, d) if b <= d else (d, b)
            st.session_state["selection_bbox"] = (lon_min2, lat_min2, lon_max2, lat_max2)

    elif selection_method == "Center + span (km)":
        col_a, col_b = st.columns(2)
        with col_a:
            span_x_km = st.number_input("Width (km)", value=50.0, min_value=0.1)
        with col_b:
            span_y_km = st.number_input("Height (km)", value=50.0, min_value=0.1)
        center_span = (span_x_km, span_y_km)
        if st.button("Apply center/span", key="apply_center_span"):
            km_per_deg_lat = 111.32
            km_per_deg_lon = 111.32 * np.cos(np.deg2rad(center_lat))
            dlon = (span_x_km / 2.0) / max(km_per_deg_lon, 1e-6)
            dlat = (span_y_km / 2.0) / km_per_deg_lat
            st.session_state["selection_bbox"] = (
                center_lon - dlon,
                center_lat - dlat,
                center_lon + dlon,
                center_lat + dlat,
            )

st.caption(
    "Tip: Hover anywhere to see all overlapping map layers. Use the Selection panel to draw a bbox."
)

if clear_sel:
    st.session_state.pop("selection_bbox", None)
    st.session_state.pop("drawn_bbox_pending", None)


@st.cache_data(show_spinner=False)
def load_kmls(folder: str) -> gpd.GeoDataFrame | None:
    """Walk a folder, read every .kml (all layers), combine as GeoDataFrame in EPSG:4326."""
    gdf_list = []
    for root, _, files in os.walk(folder):
        for file in files:
            if not file.lower().endswith(".kml"):
                continue
            file_path = os.path.join(root, file)
            try:
                layers = fiona.listlayers(file_path)
            except Exception:
                # Skip unreadable KML
                continue

            for layer in layers:
                try:
                    gdf = gpd.read_file(file_path, layer=layer)
                    # Drop empties
                    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
                    if gdf.empty:
                        continue
                    # Ensure finite bounds
                    if not np.isfinite(gdf.total_bounds).all():
                        continue

                    gdf = gdf.copy()
                    gdf["source_file"] = os.path.basename(file_path)
                    gdf["layer"] = str(layer)
                    gdf_list.append(gdf)
                except Exception as e:
                    print(f"[read fail] {file_path} / layer '{layer}': {e}", file=sys.stderr)
                    continue

    if not gdf_list:
        return None

    combined = gpd.GeoDataFrame(pd.concat(gdf_list, ignore_index=True), crs=gdf_list[0].crs)

    # Default to WGS84 if CRS is missing (common with KML)
    if combined.crs is None:
        combined.set_crs(epsg=4326, inplace=True)

    # Reproject to WGS84
    if combined.crs and combined.crs.to_epsg() != 4326:
        combined = combined.to_crs(epsg=4326)

    # Explode multipart and keep only polygons
    exploded: gpd.GeoDataFrame = cast(gpd.GeoDataFrame, combined.explode(index_parts=False))
    exploded = gpd.GeoDataFrame(exploded, crs=combined.crs)
    filtered: gpd.GeoDataFrame = cast(
        gpd.GeoDataFrame,
        exploded[exploded.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy(),
    )
    filtered.reset_index(drop=True, inplace=True)
    return filtered


@st.cache_data(show_spinner=False)
def load_or_build_footprints(base_dir: str) -> gpd.GeoDataFrame | None:
    """Load preprocessed footprints from GPKG; if missing, generate from .map metadata.

    Ensures columns: geometry (EPSG:4326), source_file, layer, year (filled when possible).
    """
    gpkg_path = os.path.join(base_dir, "map_footprints_wgs84.gpkg")

    def _ensure_overlay_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        # Ensure CRS
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)

        # source_file and layer from map_file if available
        if "source_file" not in gdf.columns:
            if "map_file" in gdf.columns:
                gdf["source_file"] = gdf["map_file"].apply(lambda p: os.path.basename(str(p)))
            else:
                gdf["source_file"] = ""
        if "layer" not in gdf.columns:
            if "map_file" in gdf.columns:
                gdf["layer"] = gdf["map_file"].apply(
                    lambda p: os.path.splitext(os.path.basename(str(p)))[0]
                )
            else:
                gdf["layer"] = "footprint"

        # Fill year values opportunistically from path if missing
        if "year" in gdf.columns and "map_file" in gdf.columns:
            mask_missing = gdf["year"].isna() | gdf["year"].isin(["", None])
            if bool(mask_missing.any()):
                gdf.loc[mask_missing, "year"] = gdf.loc[mask_missing, "map_file"].apply(
                    lambda p: MetadataCollector.extract_year_from_path(str(p))
                )
        return gdf

    # Try loading existing GPKG (prefer layer name 'footprints' if present)
    if os.path.isfile(gpkg_path):
        try:
            try:
                gdf = gpd.read_file(gpkg_path, layer="footprints")
            except Exception:
                gdf = gpd.read_file(gpkg_path)
            if gdf is not None and not gdf.empty:
                return _ensure_overlay_columns(gdf)
        except Exception:
            pass

    # Generate from .map files and save
    try:
        collector = MetadataCollector(base_dir=base_dir, verbose=False)
        df_raw = collector.collect_all_map_metadata(base_dir)
        df_filled = collector.fill_missing_metadata(df_raw)
        footprints = normalize_footprints(
            df_filled, datum_col="datum", corners_col="corners", target="EPSG:4326"
        )
        if footprints is None or footprints.empty:
            return None
        footprints = _ensure_overlay_columns(footprints)
        # Save to GPKG
        try:
            footprints.to_file(gpkg_path, layer="footprints", driver="GPKG")
        except Exception:
            # Best-effort save; continue even if write fails
            pass
        return footprints
    except Exception:
        return None


def _build_hover_text(row: pd.Series) -> str:
    """
    Build hover text for a cell row.
    Expects row['layer_list'] and row['file_list'] to be iterables or missing.
    Produces multi-line hover text using HTML <br> for Plotly.
    """

    def _to_list(val):
        if val is None:
            return []
        try:
            if pd.isna(val):
                return []
        except Exception:
            pass
        try:
            return list(val)
        except Exception:
            return [str(val)]

    # Prefer preformatted layer info lines (from footprints attributes) if available
    info_lines = _to_list(row.get("layer_info_list"))
    layers = _to_list(row.get("layer_list")) if not info_lines else []
    overlap = int(row.get("overlap_count") or 0)

    lines = [f"<b>Overlaps: {overlap}</b>"]
    if info_lines:
        lines.extend(str(x) for x in info_lines)
    elif layers:
        lines.extend(str(x) for x in layers)
    return "<br>".join(lines)


@st.cache_data(show_spinner=False)
def build_overlap_cells(
    _combined_gdf: gpd.GeoDataFrame,
    simplify_tolerance: float = 0.0,
    min_cell_area: float = 0.0,
) -> gpd.GeoDataFrame:
    """
    Create 'cells' with uniform overlap by polygonizing unified boundaries.
    Topology operations run in EPSG:3857 (meters):
      - simplify_tolerance: meters
      - min_cell_area: square meters
    Returns a GeoDataFrame (EPSG:4326) with: geometry, layer_list, file_list, overlap_count, hover_text
    """
    # Project to planar CRS for robust/fast topology ops
    work = _combined_gdf.to_crs(3857)

    geoms = work.geometry
    if simplify_tolerance > 0.0:
        geoms = geoms.simplify(simplify_tolerance, preserve_topology=True)

    # Union of boundaries, then polygonize into 'atomic' cells
    merged_boundaries = unary_union(geoms.boundary)
    cells = list(polygonize(merged_boundaries))
    if not cells:
        return gpd.GeoDataFrame(
            {
                "geometry": gpd.GeoSeries([], crs="EPSG:4326"),
                "layer_list": pd.Series(dtype=object),
                "file_list": pd.Series(dtype=object),
                "overlap_count": pd.Series(dtype=int),
                "hover_text": pd.Series(dtype=str),
            },
            geometry="geometry",
            crs="EPSG:4326",
        )

    if clear_sel:
        st.session_state.pop("selection_bbox", None)
        st.session_state.pop("drawn_bbox_pending", None)

    cells_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries(cells, crs=work.crs))

    # Optional: filter out tiny sliver cells by area (m^2)
    if min_cell_area > 0.0:
        cells_gdf["area_m2"] = cells_gdf.area
        cells_gdf = cells_gdf[cells_gdf["area_m2"] > float(min_cell_area)].copy()
        cells_gdf.drop(columns=["area_m2"], inplace=True)

    # Spatial join to figure out which original polygons intersect each cell
    left_gdf = cast(gpd.GeoDataFrame, cells_gdf)
    # Include optional attributes (e.g., year) from footprints to enrich hover text
    right_cols = ["layer", "source_file", "geometry"]
    if "year" in work.columns:
        right_cols.insert(2, "year")  # keep geometry last
    right_gdf = cast(gpd.GeoDataFrame, work[right_cols])
    hits = gpd.sjoin(
        left_gdf,
        right_gdf,
        how="left",
        predicate="intersects",
    )

    # Build per-hit hover line before grouping, using available attributes
    try:

        def _fmt_line(rec: pd.Series) -> str:
            lyr = str(rec.get("layer", "")).strip()
            yr = rec.get("year", None)
            src = rec.get("source_file", "")
            bits = [lyr] if lyr else []
            try:
                if yr is not None and pd.notna(yr) and str(yr) != "":
                    # cast numeric years cleanly
                    if isinstance(yr, (int, np.integer)):
                        bits[-1] = f"{bits[-1]} ({int(yr)})" if bits else f"({int(yr)})"
                    else:
                        # handle floats-as-years like 1987.0
                        try:
                            y_int = int(yr)
                            if float(yr) == float(y_int):
                                bits[-1] = f"{bits[-1]} ({y_int})" if bits else f"({y_int})"
                            else:
                                bits[-1] = f"{bits[-1]} ({yr})" if bits else f"({yr})"
                        except Exception:
                            bits[-1] = f"{bits[-1]} ({yr})" if bits else f"({yr})"
            except Exception:
                pass
            try:
                if src and pd.notna(src):
                    base = os.path.basename(str(src))
                    bits.append(base)
            except Exception:
                pass
            return " — ".join(bits) if bits else ""

        hits["__hover_line"] = hits.apply(_fmt_line, axis=1)
    except Exception:
        hits["__hover_line"] = hits["layer"].astype(str)

    # Aggregate per cell
    agg = (
        hits.groupby(hits.index)
        .agg(
            layer_list=("layer", lambda s: sorted({str(x) for x in s if pd.notna(x)})),
            file_list=("source_file", lambda s: sorted({str(x) for x in s if pd.notna(x)})),
            layer_info_list=(
                "__hover_line",
                lambda s: [x for x in sorted({str(x) for x in s if pd.notna(x) and str(x) != ""})],
            ),
            overlap_count=("layer", lambda s: int(s.notna().sum())),
        )
        .reset_index()
    )

    cells_gdf = cells_gdf.join(agg.set_index("index"))
    cells_gdf = cells_gdf[cells_gdf["overlap_count"].fillna(0) > 0].copy()

    # Build hover text
    cells_gdf["hover_text"] = cells_gdf.apply(_build_hover_text, axis=1)

    # Return to WGS84 for Plotly
    return cells_gdf.to_crs(4326)


def make_figure(
    cells_gdf: gpd.GeoDataFrame,
    center_lat: float,
    center_lon: float,
    zoom: int,
    base_choice: str,
    fill_alpha: float,
    trace_opacity: float,
    line_width: int,
    selection_bbox=None,
    dragmode: str = "zoom",
) -> go.Figure:
    """Build Plotly MapLibre figure using go.Scattermap and MapLibre layout."""
    fig = go.Figure()

    fill_rgba = f"rgba(0, 150, 255, {fill_alpha:.3f})"
    line_rgba = "rgba(0, 90, 200, 0.8)"

    # Precompute representative points for interior hover targets
    pt_lons = []
    pt_lats = []
    pt_texts = []

    for _, row in cells_gdf.iterrows():
        geom = row.geometry
        if geom.is_empty:
            continue
        # representative_point() guarantees the point lies within the polygon (not on a hole)
        try:
            rp = geom.representative_point()
            pt_lons.append(float(rp.x))
            pt_lats.append(float(rp.y))
            pt_texts.append(row.get("hover_text", ""))
        except Exception:
            pass
        polys = (
            [geom]
            if geom.geom_type == "Polygon"
            else list(geom.geoms) if geom.geom_type == "MultiPolygon" else []
        )
        for poly in polys:
            x, y = poly.exterior.coords.xy
            fig.add_trace(
                go.Scattermap(
                    lon=list(x),
                    lat=list(y),
                    mode="lines",
                    fill="toself",
                    fillcolor=fill_rgba,
                    opacity=trace_opacity,
                    line=dict(width=line_width, color=line_rgba),
                    hoverinfo="text",
                    hovertext=row.get("hover_text", ""),
                    showlegend=False,
                )
            )
            for ring in getattr(poly, "interiors", []):
                xi, yi = ring.coords.xy
                fig.add_trace(
                    go.Scattermap(
                        lon=list(xi),
                        lat=list(yi),
                        mode="lines",
                        fill="toself",
                        fillcolor="rgba(0,0,0,0)",
                        opacity=trace_opacity,
                        line=dict(width=line_width, color=line_rgba),
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )

    if base_choice == "USGS Imagery (no token)":
        # Use empty sourceattribution to avoid the default on-map overlay; add a subtle custom annotation instead
        fig.update_layout(
            map_style="white-bg",
            map_layers=[
                {
                    "below": "traces",
                    "sourcetype": "raster",
                    "sourceattribution": "",  # moved to a subtle annotation below
                    "source": [
                        "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
                    ],
                }
            ],
            map_center=dict(lat=center_lat, lon=center_lon),
            map_zoom=zoom,
            showlegend=False,
        )
        # Add a small, semi-transparent attribution in the bottom-right corner
        fig.update_layout(
            annotations=[
                dict(
                    text="Imagery © USGS",
                    xref="paper",
                    yref="paper",
                    x=0.995,
                    y=0.005,
                    xanchor="right",
                    yanchor="bottom",
                    showarrow=False,
                    font=dict(size=10, color="rgba(0,0,0,0.65)"),
                    bgcolor="rgba(255,255,255,0.5)",
                    borderpad=2,
                )
            ]
        )
    else:
        fig.update_layout(
            map=dict(
                style=base_choice,
                center=dict(lat=center_lat, lon=center_lon),
                zoom=zoom,
            ),
            showlegend=False,
        )

    # Draw selection rectangle if present (as a red outline)
    if selection_bbox:
        lon_min, lat_min, lon_max, lat_max = selection_bbox
        rect_lon = [lon_min, lon_max, lon_max, lon_min, lon_min]
        rect_lat = [lat_min, lat_min, lat_max, lat_max, lat_min]
        fig.add_trace(
            go.Scattermap(
                lon=rect_lon,
                lat=rect_lat,
                mode="lines",
                line=dict(color="red", width=3),
                fill="none",
                hoverinfo="skip",
                showlegend=False,
                name="selection-bbox",
            )
        )

    # Add a nearly invisible markers trace as interior hover targets
    # This enables hover anywhere inside the cell fill area, not just on edges/vertices
    if pt_lons and pt_lats:
        fig.add_trace(
            go.Scattermap(
                lon=pt_lons,
                lat=pt_lats,
                mode="markers",
                marker=dict(size=14, color="rgba(0,0,0,0)"),
                opacity=0.01,  # keep slightly >0 so hover still triggers reliably
                hoverinfo="text",
                hovertext=pt_texts,
                showlegend=False,
                name="hover-targets",
            )
        )

    fig.update_layout(
        hoverlabel=dict(align="left"),
        margin=dict(l=0, r=0, t=40, b=20),
        title="Historical Map Overlaps (Hover for all layers)",
        width=1000,
        height=1000,
        dragmode=dragmode,
    )
    return fig


# (Removed Scattermapbox compatibility: using MapLibre-only rendering)


def make_boundaries_figure(
    gdf: gpd.GeoDataFrame,
    center_lat: float,
    center_lon: float,
    zoom: int,
    base_choice: str,
    trace_opacity: float,
    line_width: int,
) -> go.Figure:
    """Fast boundaries-only figure: draw all polygon exteriors as a single line trace.
    Uses None-separated coordinate segments to keep trace count low for performance.
    """
    fig = go.Figure()

    line_rgba = "rgba(0, 90, 200, 0.95)"
    all_lon = []
    all_lat = []

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = (
            [geom]
            if geom.geom_type == "Polygon"
            else list(geom.geoms) if geom.geom_type == "MultiPolygon" else []
        )
        for poly in polys:
            x, y = poly.exterior.coords.xy
            all_lon.extend(list(x))
            all_lat.extend(list(y))
            all_lon.append(None)
            all_lat.append(None)

    fig.add_trace(
        go.Scattermap(
            lon=all_lon,
            lat=all_lat,
            mode="lines",
            line=dict(width=line_width, color=line_rgba),
            opacity=trace_opacity,
            hoverinfo="skip",
            showlegend=False,
            name="boundaries",
        )
    )

    if base_choice == "USGS Imagery (no token)":
        fig.update_layout(
            map_style="white-bg",
            map_layers=[
                {
                    "below": "traces",
                    "sourcetype": "raster",
                    "sourceattribution": "",
                    "source": [
                        "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
                    ],
                }
            ],
            map_center=dict(lat=center_lat, lon=center_lon),
            map_zoom=zoom,
            showlegend=False,
        )
        fig.update_layout(
            annotations=[
                dict(
                    text="Imagery © USGS",
                    xref="paper",
                    yref="paper",
                    x=0.995,
                    y=0.005,
                    xanchor="right",
                    yanchor="bottom",
                    showarrow=False,
                    font=dict(size=10, color="rgba(0,0,0,0.65)"),
                    bgcolor="rgba(255,255,255,0.5)",
                    borderpad=2,
                )
            ]
        )
    else:
        fig.update_layout(
            map=dict(
                style=base_choice,
                center=dict(lat=center_lat, lon=center_lon),
                zoom=zoom,
            ),
            showlegend=False,
        )

    fig.update_layout(
        hoverlabel=dict(align="left"),
        margin=dict(l=0, r=0, t=40, b=20),
        title="Historical Map Boundaries",
        width=1000,
        height=1000,
    )
    return fig


# ---------------- Build action (compute once, persist in session) ----------------
if run_btn:
    # Quick path validation before heavy work
    if not os.path.isdir(base_folder):
        st.error(f"Base folder not found: {base_folder}")
    else:
        t0 = time.perf_counter()
        if 'data_source' not in locals():
            # Default safely to footprints if older session without the new control
            data_source = "Footprints (faster)"
        if data_source == "KML folder (slow)":
            with st.spinner("Loading KMLs…"):
                combined_gdf = load_kmls(base_folder)
        else:
            with st.spinner("Loading footprints (GPKG)…"):
                combined_gdf = load_or_build_footprints(base_folder)
        t1 = time.perf_counter()
        if combined_gdf is None or combined_gdf.empty:
            if data_source == "KML folder (slow)":
                st.error(
                    "No valid geometries found from KMLs. Check your folder path and KML files."
                )
            else:
                st.error("No valid geometries found. Ensure GPKG exists or .map files are present.")
        else:
            st.session_state["combined_gdf"] = combined_gdf
            st.session_state["built"] = True
            if show_diagnostics:
                st.write(f"Timings: load data = {t1 - t0:.2f}s")

# ---------------- Render (persists across reruns) ----------------
if st.session_state.get("built") and st.session_state.get("combined_gdf") is not None:
    combined_gdf = cast(gpd.GeoDataFrame, st.session_state.get("combined_gdf"))

    # Summary of input data
    if not combined_gdf.empty:
        n_features = len(combined_gdf)
        n_files = (
            combined_gdf["source_file"].nunique()
            if "source_file" in combined_gdf.columns
            else n_features
        )
        n_layers = combined_gdf["layer"].nunique() if "layer" in combined_gdf.columns else 1
        col1, col2, col3 = st.columns(3)
        col1.metric("Geometries", f"{n_features:,}")
        col2.metric("Maps", f"{n_files:,}")
        col3.metric("Layers", f"{n_layers:,}")

    # Build and show boundaries-only figure
    t_fig0 = time.perf_counter()
    gdf_for_render: gpd.GeoDataFrame = combined_gdf
    if simplify_tol and float(simplify_tol) > 0.0:
        try:
            gdf_for_render = cast(gpd.GeoDataFrame, gdf_for_render.to_crs(3857))
            gdf_for_render["geometry"] = gdf_for_render.geometry.simplify(
                float(simplify_tol), preserve_topology=True
            )
            gdf_for_render = cast(gpd.GeoDataFrame, gdf_for_render.to_crs(4326))
        except Exception:
            pass
    if (
        max_cells_to_render
        and int(max_cells_to_render) > 0
        and len(gdf_for_render) > int(max_cells_to_render)
    ):
        gdf_for_render = cast(
            gpd.GeoDataFrame, gdf_for_render.head(int(max_cells_to_render)).copy()
        )

    fig = make_boundaries_figure(
        gdf=gdf_for_render,
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        base_choice=base_choice,
        trace_opacity=trace_opacity,
        line_width=line_width,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": True})
    t_fig1 = time.perf_counter()
    if show_diagnostics:
        st.write(
            f"Render: polygons drawn = {len(gdf_for_render):,} · figure build+render = {t_fig1 - t_fig0:.2f}s"
        )
else:
    st.info("Configure options in the sidebar, then click **Build / Rebuild Overlap Map**.")
