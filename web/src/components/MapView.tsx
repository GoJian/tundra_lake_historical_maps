import { useEffect, useRef } from "react";
import maplibregl, { Map as MLMap, LngLat } from "maplibre-gl";
import type { BBox, HistoricSheet } from "../api";

export const BASEMAPS: Record<string, string[]> = {
  "USGS Imagery": [
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
  ],
  OpenStreetMap: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
  "Carto Dark": ["https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"],
  "Carto Light": ["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
};

const toAbs = (u: string) => (u.startsWith("http") ? u : window.location.origin + u);

interface Props {
  basemap: string;
  footprints: GeoJSON.FeatureCollection | null;
  roi: BBox | null;
  drawing: boolean;
  onRoiChange: (b: BBox) => void;
  compositeTileUrl: string | null;
  compositeOpacity: number;
  historicSheet: HistoricSheet | null;
  historicOpacity: number;
  lakes: GeoJSON.FeatureCollection | null;
}

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
const rectFC = (b: BBox): GeoJSON.FeatureCollection => ({
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      properties: {},
      geometry: {
        type: "Polygon",
        coordinates: [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]]],
      },
    },
  ],
});

export default function MapView(props: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const ready = useRef(false);
  const draw = useRef<{ on: boolean; start: LngLat | null }>({ on: false, start: null });
  const cb = useRef(props.onRoiChange);
  cb.current = props.onRoiChange;

  // create the map once
  useEffect(() => {
    if (!el.current || map.current) return;
    const m = new maplibregl.Map({
      container: el.current,
      style: {
        version: 8,
        sources: {
          basemap: { type: "raster", tiles: BASEMAPS[props.basemap], tileSize: 256 },
        },
        layers: [{ id: "basemap", type: "raster", source: "basemap" }],
      },
      center: [70, 68], // Yamal-Nenets
      zoom: 4.3,
    });
    map.current = m;
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

    m.on("load", () => {
      // data sources
      for (const id of ["composite", "historic"]) {
        m.addSource(id, { type: "raster", tiles: [`${window.location.origin}/__none__/{z}/{x}/{y}`], tileSize: 256 });
      }
      m.addSource("footprints", { type: "geojson", data: EMPTY });
      m.addSource("lakes", { type: "geojson", data: EMPTY });
      m.addSource("roi", { type: "geojson", data: EMPTY });

      m.addLayer({ id: "composite", type: "raster", source: "composite", layout: { visibility: "none" }, paint: { "raster-opacity": props.compositeOpacity } });
      m.addLayer({ id: "historic", type: "raster", source: "historic", layout: { visibility: "none" }, paint: { "raster-opacity": props.historicOpacity } });
      m.addLayer({
        id: "footprints-fill", type: "fill", source: "footprints",
        paint: {
          "fill-color": ["match", ["get", "datum_status"], "native_wgs84", "#38bdf8", "shifted", "#a78bfa", "#94a3b8"],
          "fill-opacity": 0.12,
        },
      });
      m.addLayer({ id: "footprints-line", type: "line", source: "footprints", paint: { "line-color": "#64748b", "line-width": 0.6 } });
      m.addLayer({ id: "lakes-fill", type: "fill", source: "lakes", paint: { "fill-color": "#22d3ee", "fill-opacity": 0.35 } });
      m.addLayer({ id: "lakes-line", type: "line", source: "lakes", paint: { "line-color": "#22d3ee", "line-width": 1.4 } });
      m.addLayer({ id: "roi-line", type: "line", source: "roi", paint: { "line-color": "#f87171", "line-width": 2, "line-dasharray": [2, 1] } });

      // hover popup listing the historic sheet(s) under the cursor
      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
      m.on("mousemove", "footprints-fill", (e) => {
        if (draw.current.on) return;
        const feats = m.queryRenderedFeatures(e.point, { layers: ["footprints-fill"] });
        if (!feats.length) return;
        m.getCanvas().style.cursor = "pointer";
        const lines = feats.slice(0, 8).map((f) => {
          const p = f.properties || {};
          return `${p.stem}${p.year ? ` (${p.year})` : ""} — <span class="pill">${p.datum_status}</span>`;
        });
        popup.setLngLat(e.lngLat).setHTML(`<b>${feats.length} sheet(s)</b><br>${lines.join("<br>")}`).addTo(m);
      });
      m.on("mouseleave", "footprints-fill", () => {
        if (!draw.current.on) m.getCanvas().style.cursor = "";  // keep the draw cursor while drawing
        popup.remove();
      });

      // rectangle-drag ROI selection
      m.on("mousedown", (e) => {
        if (!draw.current.on) return;
        e.preventDefault();
        draw.current.start = e.lngLat;
      });
      m.on("mousemove", (e) => {
        if (!draw.current.on || !draw.current.start) return;
        const s = draw.current.start;
        const b: BBox = [Math.min(s.lng, e.lngLat.lng), Math.min(s.lat, e.lngLat.lat), Math.max(s.lng, e.lngLat.lng), Math.max(s.lat, e.lngLat.lat)];
        (m.getSource("roi") as maplibregl.GeoJSONSource).setData(rectFC(b));
      });
      m.on("mouseup", (e) => {
        if (!draw.current.on || !draw.current.start) return;
        const s = draw.current.start;
        const b: BBox = [Math.min(s.lng, e.lngLat.lng), Math.min(s.lat, e.lngLat.lat), Math.max(s.lng, e.lngLat.lng), Math.max(s.lat, e.lngLat.lat)];
        draw.current.start = null;
        if (Math.abs(b[2] - b[0]) > 1e-4 && Math.abs(b[3] - b[1]) > 1e-4) cb.current(b);
      });

      ready.current = true;
      m.fire("tundra-ready");
    });
    return () => { m.remove(); map.current = null; ready.current = false; };
  }, []);

  const withMap = (fn: (m: MLMap) => void) => {
    const m = map.current;
    if (!m) return;
    if (ready.current) fn(m);
    else m.once("tundra-ready", () => fn(m));
  };

  // basemap
  useEffect(() => { withMap((m) => (m.getSource("basemap") as maplibregl.RasterTileSource)?.setTiles(BASEMAPS[props.basemap])); }, [props.basemap]);
  // footprints
  useEffect(() => { withMap((m) => (m.getSource("footprints") as maplibregl.GeoJSONSource)?.setData(props.footprints || EMPTY)); }, [props.footprints]);
  // lakes
  useEffect(() => { withMap((m) => (m.getSource("lakes") as maplibregl.GeoJSONSource)?.setData(props.lakes || EMPTY)); }, [props.lakes]);
  // roi rectangle (external changes, e.g. bbox inputs)
  useEffect(() => { withMap((m) => (m.getSource("roi") as maplibregl.GeoJSONSource)?.setData(props.roi ? rectFC(props.roi) : EMPTY)); }, [props.roi]);

  // composite tile layer
  useEffect(() => {
    withMap((m) => {
      if (props.compositeTileUrl) {
        (m.getSource("composite") as maplibregl.RasterTileSource).setTiles([toAbs(props.compositeTileUrl)]);
        m.setLayoutProperty("composite", "visibility", "visible");
      } else m.setLayoutProperty("composite", "visibility", "none");
    });
  }, [props.compositeTileUrl]);
  useEffect(() => { withMap((m) => m.setPaintProperty("composite", "raster-opacity", props.compositeOpacity)); }, [props.compositeOpacity]);
  // zoom to the ROI once a composite is extracted, so the imagery + lakes are
  // visible. Fit once per ROI — stepping the satellite time-slider swaps the
  // composite tiles but must not re-fit the view on every step.
  const lastFit = useRef<string>("");
  useEffect(() => {
    withMap((m) => {
      if (props.compositeTileUrl && props.roi) {
        const key = props.roi.join(",");
        if (key !== lastFit.current) {
          lastFit.current = key;
          m.fitBounds([[props.roi[0], props.roi[1]], [props.roi[2], props.roi[3]]], { padding: 60, duration: 700 });
        }
      }
    });
  }, [props.compositeTileUrl]);

  // historic sheet tile layer
  useEffect(() => {
    withMap((m) => {
      if (props.historicSheet) {
        (m.getSource("historic") as maplibregl.RasterTileSource).setTiles([toAbs(props.historicSheet.tile_url)]);
        m.setLayoutProperty("historic", "visibility", "visible");
      } else m.setLayoutProperty("historic", "visibility", "none");
    });
  }, [props.historicSheet]);
  useEffect(() => { withMap((m) => m.setPaintProperty("historic", "raster-opacity", props.historicOpacity)); }, [props.historicOpacity]);

  // toggle drag-pan when entering/leaving draw mode. The ready-to-draw crosshair
  // is applied via the `drawing` class on the container (CSS !important beats the
  // inline cursor MapLibre's own drag handlers set), so it can't flicker away.
  useEffect(() => {
    withMap((m) => {
      draw.current.on = props.drawing;
      if (props.drawing) m.dragPan.disable();
      else { m.dragPan.enable(); m.getCanvas().style.cursor = ""; }
    });
  }, [props.drawing]);

  return <div className={`map${props.drawing ? " drawing" : ""}`} ref={el} />;
}
