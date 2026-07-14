// Typed client for the Tundra Portal API. In dev, Vite proxies these paths to
// the FastAPI backend, so relative URLs (including the tile_url templates the
// API returns) work directly.

export type BBox = [number, number, number, number]; // lon_min, lat_min, lon_max, lat_max

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

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

export interface ExtractResult {
  composite_id: string;
  composite_cog: string;
  tile_url: string;
  n_scenes: number | null;
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

export interface JobState {
  job_id: string;
  status: "queued" | "running" | "done" | "error";
  error: string | null;
  result: SegmentResult | null;
}

export interface ExtractReq {
  bbox: BBox;
  datetime: string;
  sensor: string;
  res?: number;
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
    jfetch<GeoJSON.FeatureCollection>(`/footprints?bbox=${bbox.join(",")}`, {
      headers: headers(),
    }),

  extract: (req: ExtractReq) =>
    jfetch<ExtractResult>("/roi/extract", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(req),
    }),

  segment: (req: SegmentReq) =>
    jfetch<{ job_id: string }>("/segment", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(req),
    }),

  job: (id: string) => jfetch<JobState>(`/jobs/${id}`, { headers: headers() }),
};

export const fmtArea = (m2: number) =>
  m2 >= 1e6 ? `${(m2 / 1e6).toFixed(2)} km²` : `${Math.round(m2).toLocaleString()} m²`;
export const fmtLen = (m: number) =>
  m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
