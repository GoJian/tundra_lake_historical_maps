// Typed client for the Tundra Portal API. In dev, Vite proxies these paths to
// the FastAPI backend, so relative URLs (including the tile_url templates the
// API returns) work directly.

export type BBox = [number, number, number, number]; // lon_min, lat_min, lon_max, lat_max

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

// URL prefix the app is served under (Vite's `base`: "/tundra/" in a sub-path
// deployment, "/" in dev). API requests are prefixed so they hit the
// reverse-proxied backend rather than the site root. Trailing slash stripped.
const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

function headers(json = false): HeadersInit {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  if (API_KEY) h["X-API-Key"] = API_KEY;
  return h;
}

async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

export interface HistoricSheet {
  stem: string;
  year: number | null;
  tile_url: string;
}

export interface SatelliteFrame {
  label: string;          // e.g. "2021", "2023-Q3", "2023-07"
  datetime: string;       // "YYYY-MM-DD/YYYY-MM-DD"
  tile_url: string | null; // null when the step had no clear scenes
  n_scenes: number | null;
}

export interface ExtractResult {
  composite_id: string | null;
  composite_cog: string | null;
  tile_url: string | null;
  n_scenes: number | null;
  cadence: string;                    // none | annual | seasonal | monthly
  satellite_frames: SatelliteFrame[]; // populated when cadence != none
  historic_sheets: HistoricSheet[];
}

export interface SegmentSummary {
  lake_count: number;
  total_area_m2: number;
  mean_fractal_dimension?: number;
  size_distribution?: { bin_edges_m2: number[]; counts: number[] };
}

export interface SegmentResult {
  summary: SegmentSummary;
  n_scenes: number;
  lakes_geojson: GeoJSON.FeatureCollection;
}

// One cadence time-step's segmentation outcome. `summary`/`lakes_geojson` are
// null for a step with no usable scenes (error carries the reason).
export interface SeriesEntry {
  label: string;
  datetime: string;
  n_scenes: number;
  summary: SegmentSummary | null;
  lakes_geojson?: GeoJSON.FeatureCollection | null; // present only in the final result
  error?: string | null;
}

export interface TimeseriesResult {
  cadence: string;
  series: SeriesEntry[];
}

// Live progress published while a time-series run is in flight.
export interface JobProgress {
  done: number;
  total: number;
  current: string;
  elapsed_s: number;
  eta_s: number | null;
  series: { label: string; n_scenes: number; summary: SegmentSummary | null }[];
}

export interface JobState<R = SegmentResult> {
  job_id: string;
  status: "queued" | "running" | "done" | "error";
  error: string | null;
  result: R | null;
  progress?: JobProgress | null;
}

export interface ExtractReq {
  bbox: BBox;
  datetime: string;
  sensor: string;
  res?: number;
  cadence?: string; // none | annual | seasonal | monthly
}
export interface SegmentReq extends ExtractReq {
  strategy?: string;
  water_index?: string;
  min_area_m2?: number;
  simplify_px?: number;
  model_type?: string;
}

export const api = {
  footprints: (bbox: BBox) =>
    jfetch<GeoJSON.FeatureCollection>(`${BASE}/footprints?bbox=${bbox.join(",")}`, {
      headers: headers(),
    }),

  extract: (req: ExtractReq) =>
    jfetch<ExtractResult>(`${BASE}/roi/extract`, {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(req),
    }),

  segment: (req: SegmentReq) =>
    jfetch<{ job_id: string }>(`${BASE}/segment`, {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(req),
    }),

  segmentTimeseries: (req: SegmentReq) =>
    jfetch<{ job_id: string; total: number }>(`${BASE}/segment/timeseries`, {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(req),
    }),

  job: <R = SegmentResult>(id: string) =>
    jfetch<JobState<R>>(`${BASE}/jobs/${id}`, { headers: headers() }),
};

export const fmtArea = (m2: number) =>
  m2 >= 1e6 ? `${(m2 / 1e6).toFixed(2)} km²` : `${Math.round(m2).toLocaleString()} m²`;
export const fmtLen = (m: number) =>
  m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
