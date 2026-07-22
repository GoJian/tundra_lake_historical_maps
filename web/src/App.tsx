import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import MapView, { BASEMAPS } from "./components/MapView";
import ResultsPanel, { MetricKey } from "./components/ResultsPanel";
import TimeSeriesPanel from "./components/TimeSeriesPanel";
import { api, BBox, ExtractResult, ExtractProgress, TimeseriesResult } from "./api";

const fmtEta = (s: number | null | undefined) =>
  s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;

const YAMAL: BBox = [64, 63, 90, 74];
const METRIC_LABELS: [MetricKey, string][] = [
  ["area", "Area"], ["perimeter", "Perimeter"], ["fractal", "Fractal dimension"], ["size_dist", "Size distribution"],
];
const CADENCES: [string, string][] = [
  ["none", "None — single composite"], ["annual", "Annual"],
  ["seasonal", "Seasonal (quarterly)"], ["monthly", "Monthly"],
];

export default function App() {
  const [basemap, setBasemap] = useState("USGS Imagery");
  const [roi, setRoi] = useState<BBox | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [datetime, setDatetime] = useState("2023-06-15/2023-09-10");
  const [sensor, setSensor] = useState("sentinel-2");
  const [cadence, setCadence] = useState("none");
  const [minArea, setMinArea] = useState(2000);
  const [strategy, setStrategy] = useState("seeded");

  const [extract, setExtract] = useState<ExtractResult | null>(null);
  const [extractJobId, setExtractJobId] = useState<string | null>(null);
  const [sheetIdx, setSheetIdx] = useState(0);
  const [satFrameIdx, setSatFrameIdx] = useState(0);
  const [histOpacity, setHistOpacity] = useState(0.85);
  const [compOpacity, setCompOpacity] = useState(1);
  const [jobId, setJobId] = useState<string | null>(null);
  const [tsJobId, setTsJobId] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<Set<MetricKey>>(new Set(["area", "perimeter", "fractal", "size_dist"]));

  // footprints for the whole region (loaded once)
  const footprints = useQuery({ queryKey: ["footprints"], queryFn: () => api.footprints(YAMAL) });

  const extractMut = useMutation({
    mutationFn: (c: string) => api.extract({ bbox: roi!, datetime, sensor, cadence: c }),
    onSuccess: (r) => setExtractJobId(r.job_id),
  });

  // Poll the extract job for staged progress (STAC search vs. per-scene
  // compositing) and the eventual ExtractResult. Fast poll for a smooth bar.
  const extractJob = useQuery({
    queryKey: ["extractjob", extractJobId],
    queryFn: () => api.job<ExtractResult, ExtractProgress>(extractJobId!),
    enabled: !!extractJobId,
    refetchInterval: (q) => {
      const st = q.state.data?.status;
      return st === "done" || st === "error" ? false : 700;
    },
  });

  // apply the result once the composite build completes
  useEffect(() => {
    if (extractJob.data?.status === "done" && extractJob.data.result) {
      setExtract(extractJob.data.result);
      setSheetIdx(0); setSatFrameIdx(0);
    }
  }, [extractJob.data?.status]);

  const exStatus = extractJob.data?.status;
  const extractProgress = extractJob.data?.progress ?? null;
  const extracting = extractMut.isPending ||
    (!!extractJobId && exStatus !== "done" && exStatus !== "error");
  const extractError = extractMut.isError
    ? String(extractMut.error)
    : exStatus === "error" ? extractJob.data?.error ?? "extract failed" : null;

  // Flip the satellite time-granularity. Re-runs extract with the new cadence if
  // we've already extracted once (composites are cached server-side, so toggling
  // back to a cadence already built is near-instant).
  const onCadence = (c: string) => {
    setCadence(c); setSatFrameIdx(0);
    if (roi && extract) { setExtractJobId(null); extractMut.mutate(c); }
  };

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

  const tsMut = useMutation({
    mutationFn: (c: string) =>
      api.segmentTimeseries({ bbox: roi!, datetime, sensor, cadence: c, strategy, min_area_m2: minArea }),
    onSuccess: (r) => setTsJobId(r.job_id),
  });

  // Poll the time-series job often so the progress bar / ETA update smoothly.
  const tsJob = useQuery({
    queryKey: ["tsjob", tsJobId],
    queryFn: () => api.job<TimeseriesResult>(tsJobId!),
    enabled: !!tsJobId,
    refetchInterval: (q) => {
      const st = q.state.data?.status;
      return st === "done" || st === "error" ? false : 800;
    },
  });

  const sheets = useMemo(
    () => (extract?.historic_sheets || []).slice().sort((a, b) => (a.year || 0) - (b.year || 0)),
    [extract],
  );
  const sheet = sheets[Math.min(sheetIdx, Math.max(0, sheets.length - 1))] || null;

  // satellite time-series frames (empty when cadence = none)
  const frames = extract?.satellite_frames || [];
  const satFrame = frames[Math.min(satFrameIdx, Math.max(0, frames.length - 1))] || null;
  const stepping = !!extract && extract.cadence !== "none";
  const satTileUrl = !extract ? null : stepping ? satFrame?.tile_url ?? null : extract.tile_url;
  const satScenes = !extract ? null : stepping ? satFrame?.n_scenes ?? null : extract.n_scenes;

  const result = job.data?.status === "done" ? job.data.result : null;

  // time-series segmentation: a run in flight or complete
  const tsActive = !!tsJobId;
  const tsResult = tsJob.data?.status === "done" ? tsJob.data.result : null;
  const tsEntry = tsResult ? tsResult.series[Math.min(satFrameIdx, tsResult.series.length - 1)] : null;

  // map lake overlay: the selected frame's lakes when a time-series run is done,
  // otherwise the single whole-range segmentation result.
  const lakes = tsResult ? (tsEntry?.lakes_geojson ?? null) : (result?.lakes_geojson || null);

  // jump the satellite slider (and thus the map) to a label picked in a chart
  const pickLabel = (label: string) => {
    const i = frames.findIndex((f) => f.label === label);
    if (i >= 0) setSatFrameIdx(i);
  };

  const toggleMetric = (k: MetricKey) =>
    setMetrics((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });

  const onBboxInput = (i: number, v: number) => {
    const b: BBox = roi ? ([...roi] as BBox) : [69, 69.9, 69.3, 70.1];
    b[i] = v; setRoi(b);
  };

  return (
    <div className={`app${result || tsActive ? " with-results" : ""}`}>
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
          <button className="primary" disabled={!roi || extracting}
            onClick={() => { setExtractJobId(null); extractMut.mutate(cadence); }}>
            {extracting ? <><span className="spinner" />Extracting…</> : "Extract imagery + maps"}
          </button>
          {extracting && (
            <div className="ts-progress" style={{ marginTop: 4 }}>
              <div className="ts-progress-head">
                <span>{extractProgress?.message ?? "Starting…"}</span>
                {extractProgress?.eta_s != null && <span className="hint">~{fmtEta(extractProgress.eta_s)} left</span>}
              </div>
              <div className="progress-track">
                <div className="progress-fill anim" style={{ width: `${extractProgress?.pct ?? 4}%` }} />
              </div>
            </div>
          )}
          {extractError && <div className="status err">{extractError.slice(0, 160)}</div>}
        </div>

        {extract && (
          <>
            <div className="group">
              <label className="h">Satellite over time {satScenes ? `(${satScenes} scenes)` : ""}</label>
              <label className="field">Step by
                <select value={cadence} onChange={(e) => onCadence(e.target.value)} disabled={extracting}>
                  {CADENCES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </label>
              {stepping && frames.length > 0 && (
                <>
                  <div className="slider-row">
                    <input type="range" min={0} max={frames.length - 1} step={1} value={Math.min(satFrameIdx, frames.length - 1)} onChange={(e) => setSatFrameIdx(+e.target.value)} />
                    <span className="val">{Math.min(satFrameIdx, frames.length - 1) + 1}/{frames.length}</span>
                  </div>
                  <div className="hint">
                    {satFrame?.label}
                    {satFrame && satFrame.tile_url === null ? " — no clear scenes" : ""}
                  </div>
                </>
              )}
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
                onClick={() => { setTsJobId(null); setJobId(null); segMut.mutate(); }}>
                {job.data && (job.data.status === "running" || job.data.status === "queued")
                  ? <><span className="spinner" />Segmenting…</> : "Detect lakes"}
              </button>
              {job.data?.status === "running" && <div className="status run">Running SAM on GPU…</div>}
              {job.data?.status === "error" && <div className="status err">{String(job.data.error).slice(0, 200)}</div>}

              {stepping && frames.length > 0 && (
                <>
                  <button className="primary" style={{ marginTop: 8 }}
                    disabled={!roi || tsJob.data?.status === "running" || tsJob.data?.status === "queued"}
                    onClick={() => { setJobId(null); setTsJobId(null); tsMut.mutate(cadence); }}>
                    {tsJob.data && (tsJob.data.status === "running" || tsJob.data.status === "queued")
                      ? <><span className="spinner" />Segmenting {tsJob.data.progress?.done ?? 0}/{tsJob.data.progress?.total ?? frames.length}…</>
                      : `Detect over time (${frames.length} step${frames.length === 1 ? "" : "s"})`}
                  </button>
                  <div className="hint">
                    Runs SAM on each time step and charts the change. Each step takes a while (SAM on the GPU).
                  </div>
                  {frames.length === 1 && (
                    <div className="hint" style={{ color: "var(--accent-2)" }}>
                      Only 1 time step for this range — widen the date range (or pick a finer cadence) to see a trend.
                    </div>
                  )}
                  {tsMut.isError && <div className="status err">{String(tsMut.error).slice(0, 200)}</div>}
                </>
              )}
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
          compositeTileUrl={satTileUrl || null}
          compositeOpacity={compOpacity}
          historicSheet={sheet}
          historicOpacity={histOpacity}
          lakes={lakes}
        />
        {footprints.data && (
          <div className="map-overlay">{footprints.data.features?.length ?? 0} historic sheets loaded</div>
        )}
      </div>

      {tsActive
        ? <TimeSeriesPanel
            status={tsJob.data?.status ?? "queued"}
            progress={tsJob.data?.progress}
            result={tsResult}
            cadence={cadence}
            selectedLabel={satFrame?.label ?? null}
            onPickLabel={pickLabel}
          />
        : result && <ResultsPanel result={result} metrics={metrics} />}
    </div>
  );
}
