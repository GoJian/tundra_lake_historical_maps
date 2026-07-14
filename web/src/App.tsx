import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import MapView, { BASEMAPS } from "./components/MapView";
import ResultsPanel, { MetricKey } from "./components/ResultsPanel";
import { api, BBox, ExtractResult } from "./api";

const YAMAL: BBox = [64, 63, 90, 74];
const METRIC_LABELS: [MetricKey, string][] = [
  ["area", "Area"], ["perimeter", "Perimeter"], ["fractal", "Fractal dimension"], ["size_dist", "Size distribution"],
];

export default function App() {
  const [basemap, setBasemap] = useState("USGS Imagery");
  const [roi, setRoi] = useState<BBox | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [datetime, setDatetime] = useState("2023-06-15/2023-09-10");
  const [sensor, setSensor] = useState("sentinel-2");
  const [minArea, setMinArea] = useState(2000);
  const [strategy, setStrategy] = useState("seeded");

  const [extract, setExtract] = useState<ExtractResult | null>(null);
  const [sheetIdx, setSheetIdx] = useState(0);
  const [histOpacity, setHistOpacity] = useState(0.85);
  const [compOpacity, setCompOpacity] = useState(1);
  const [jobId, setJobId] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<Set<MetricKey>>(new Set(["area", "perimeter", "fractal", "size_dist"]));

  // footprints for the whole region (loaded once)
  const footprints = useQuery({ queryKey: ["footprints"], queryFn: () => api.footprints(YAMAL) });

  const extractMut = useMutation({
    mutationFn: () => api.extract({ bbox: roi!, datetime, sensor }),
    onSuccess: (r) => { setExtract(r); setSheetIdx(0); },
  });

  const segMut = useMutation({
    mutationFn: () => api.segment({ bbox: roi!, datetime, sensor, strategy, min_area_m2: minArea }),
    onSuccess: (r) => setJobId(r.job_id),
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => {
      const st = q.state.data?.status;
      return st === "done" || st === "error" ? false : 1500;
    },
  });

  const sheets = useMemo(
    () => (extract?.historic_sheets || []).slice().sort((a, b) => (a.year || 0) - (b.year || 0)),
    [extract],
  );
  const sheet = sheets[Math.min(sheetIdx, Math.max(0, sheets.length - 1))] || null;
  const result = job.data?.status === "done" ? job.data.result : null;
  const lakes = result?.lakes_geojson || null;

  const toggleMetric = (k: MetricKey) =>
    setMetrics((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });

  const onBboxInput = (i: number, v: number) => {
    const b: BBox = roi ? ([...roi] as BBox) : [69, 69.9, 69.3, 70.1];
    b[i] = v; setRoi(b);
  };

  return (
    <div className={`app${result ? " with-results" : ""}`}>
      <aside className="sidebar">
        <div className="brand"><span className="dot" /><h1>Tundra Map Portal</h1></div>
        <div className="sub">Historic maps × modern satellite — Arctic lake change</div>

        <div className="group">
          <label className="h">Basemap</label>
          <select value={basemap} onChange={(e) => setBasemap(e.target.value)}>
            {Object.keys(BASEMAPS).map((b) => <option key={b}>{b}</option>)}
          </select>
        </div>

        <div className="group">
          <label className="h">Region of interest</label>
          <button className={drawing ? "primary" : ""} onClick={() => setDrawing((d) => !d)}>
            {drawing ? "◼ Drawing — drag on map" : "▭ Draw rectangle"}
          </button>
          <div className="row">
            <label className="field">lon min<input type="number" step="0.01" value={roi?.[0] ?? ""} onChange={(e) => onBboxInput(0, +e.target.value)} /></label>
            <label className="field">lat min<input type="number" step="0.01" value={roi?.[1] ?? ""} onChange={(e) => onBboxInput(1, +e.target.value)} /></label>
          </div>
          <div className="row">
            <label className="field">lon max<input type="number" step="0.01" value={roi?.[2] ?? ""} onChange={(e) => onBboxInput(2, +e.target.value)} /></label>
            <label className="field">lat max<input type="number" step="0.01" value={roi?.[3] ?? ""} onChange={(e) => onBboxInput(3, +e.target.value)} /></label>
          </div>
        </div>

        <div className="group">
          <label className="h">Imagery</label>
          <div className="row">
            <label className="field">Sensor
              <select value={sensor} onChange={(e) => setSensor(e.target.value)}>
                <option value="sentinel-2">Sentinel-2 (10 m)</option>
                <option value="landsat">Landsat (30 m)</option>
              </select>
            </label>
          </div>
          <label className="field">Date range
            <input value={datetime} onChange={(e) => setDatetime(e.target.value)} />
          </label>
          <button className="primary" disabled={!roi || extractMut.isPending} onClick={() => extractMut.mutate()}>
            {extractMut.isPending ? <><span className="spinner" />Extracting…</> : "Extract imagery + maps"}
          </button>
          {extractMut.isError && <div className="status err">{String(extractMut.error).slice(0, 160)}</div>}
        </div>

        {extract && (
          <>
            <div className="group">
              <label className="h">Satellite composite {extract.n_scenes ? `(${extract.n_scenes} scenes)` : ""}</label>
              <div className="slider-row">
                <span className="hint">opacity</span>
                <input type="range" min={0} max={1} step={0.05} value={compOpacity} onChange={(e) => setCompOpacity(+e.target.value)} />
                <span className="val">{compOpacity.toFixed(2)}</span>
              </div>
            </div>

            {sheets.length > 0 && (
              <div className="group">
                <label className="h">Historic sheet — {sheet?.year ?? "—"}</label>
                <div className="slider-row">
                  <input type="range" min={0} max={sheets.length - 1} step={1} value={sheetIdx} onChange={(e) => setSheetIdx(+e.target.value)} />
                  <span className="val">{sheetIdx + 1}/{sheets.length}</span>
                </div>
                <div className="hint">{sheet?.stem}</div>
                <div className="slider-row">
                  <span className="hint">opacity</span>
                  <input type="range" min={0} max={1} step={0.05} value={histOpacity} onChange={(e) => setHistOpacity(+e.target.value)} />
                  <span className="val">{histOpacity.toFixed(2)}</span>
                </div>
              </div>
            )}

            <div className="group">
              <label className="h">Lake detection (SAM)</label>
              <div className="row">
                <label className="field">Strategy
                  <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                    <option value="seeded">Seeded (MNDWI + SAM)</option>
                    <option value="auto">Auto (SAM + filter)</option>
                  </select>
                </label>
                <label className="field">Min area m²
                  <input type="number" step="500" value={minArea} onChange={(e) => setMinArea(+e.target.value)} />
                </label>
              </div>
              <button className="primary" disabled={!roi || segMut.isPending || job.data?.status === "running" || job.data?.status === "queued"}
                onClick={() => { setJobId(null); segMut.mutate(); }}>
                {job.data && (job.data.status === "running" || job.data.status === "queued")
                  ? <><span className="spinner" />Segmenting…</> : "Detect lakes"}
              </button>
              {job.data?.status === "running" && <div className="status run">Running SAM on GPU…</div>}
              {job.data?.status === "error" && <div className="status err">{String(job.data.error).slice(0, 200)}</div>}
              <div className="checks">
                {METRIC_LABELS.map(([k, label]) => (
                  <label key={k}><input type="checkbox" checked={metrics.has(k)} onChange={() => toggleMetric(k)} />{label}</label>
                ))}
              </div>
            </div>
          </>
        )}

        <div className="hint" style={{ marginTop: "auto" }}>
          Hover a footprint to list overlapping historic sheets. Fill colour = datum status.
        </div>
        <div className="legend">
          <span><i style={{ background: "#38bdf8" }} />native WGS84</span>
          <span><i style={{ background: "#a78bfa" }} />datum-shifted</span>
          <span><i style={{ background: "#22d3ee" }} />detected lake</span>
        </div>
      </aside>

      <div className="map-wrap">
        <MapView
          basemap={basemap}
          footprints={footprints.data || null}
          roi={roi}
          drawing={drawing}
          onRoiChange={(b) => { setRoi(b); setDrawing(false); }}
          compositeTileUrl={extract?.tile_url || null}
          compositeOpacity={compOpacity}
          historicSheet={sheet}
          historicOpacity={histOpacity}
          lakes={lakes}
        />
        {footprints.data && (
          <div className="map-overlay">{footprints.data.features?.length ?? 0} historic sheets loaded</div>
        )}
      </div>

      {result && <ResultsPanel result={result} metrics={metrics} />}
    </div>
  );
}
