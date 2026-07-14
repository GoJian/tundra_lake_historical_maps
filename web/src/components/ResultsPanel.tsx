import { useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ScatterChart, Scatter, CartesianGrid, ZAxis,
} from "recharts";
import type { SegmentResult } from "../api";
import { fmtArea, fmtLen } from "../api";

const ACCENT = "#22d3ee";
const GRID = "#334155";
const MUTED = "#94a3b8";

interface LakeRow {
  label: number;
  area_m2: number;
  perimeter_m: number;
  fractal_dimension: number | null;
}

export type MetricKey = "area" | "perimeter" | "fractal" | "size_dist";

export default function ResultsPanel({
  result, metrics,
}: {
  result: SegmentResult;
  metrics: Set<MetricKey>;
}) {
  const rows: LakeRow[] = useMemo(
    () => (result.lakes_geojson.features || []).map((f) => f.properties as unknown as LakeRow),
    [result],
  );
  const s = result.summary;

  const sizeData = useMemo(() => {
    const sd = s.size_distribution;
    if (!sd) return [];
    return sd.counts.map((c, i) => ({
      bin: `${(sd.bin_edges_m2[i] / 1e4).toFixed(1)}`,
      count: c,
    }));
  }, [s]);

  const scatter = useMemo(
    () => rows.filter((r) => r.fractal_dimension != null)
      .map((r) => ({ area: r.area_m2 / 1e6, D: r.fractal_dimension as number })),
    [rows],
  );

  const tooltipStyle = { background: "#1e293b", border: `1px solid ${GRID}`, borderRadius: 8, color: "#e2e8f0", fontSize: 12 };

  return (
    <div className="results">
      <h2>Lake metrics</h2>
      <div className="stat-grid">
        <div className="stat"><div className="n">{s.lake_count}</div><div className="k">lakes</div></div>
        <div className="stat"><div className="n">{(s.total_area_m2 / 1e6).toFixed(2)}</div><div className="k">km² total</div></div>
        {s.mean_fractal_dimension != null && (
          <div className="stat"><div className="n">{s.mean_fractal_dimension.toFixed(3)}</div><div className="k">mean fractal D</div></div>
        )}
        <div className="stat"><div className="n">{result.n_scenes}</div><div className="k">scenes used</div></div>
      </div>

      {metrics.has("size_dist") && sizeData.length > 0 && (
        <div className="chart-card">
          <h3>Lake size distribution (count per area bin, ha)</h3>
          <ResponsiveContainer width="100%" height={150}>
            <BarChart data={sizeData} margin={{ top: 4, right: 6, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} strokeDasharray="2 2" vertical={false} />
              <XAxis dataKey="bin" tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} />
              <YAxis tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "#ffffff10" }} />
              <Bar dataKey="count" fill={ACCENT} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {metrics.has("fractal") && scatter.length > 0 && (
        <div className="chart-card">
          <h3>Boundary fractal dimension vs lake area (km²)</h3>
          <ResponsiveContainer width="100%" height={150}>
            <ScatterChart margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} strokeDasharray="2 2" />
              <XAxis type="number" dataKey="area" name="area" tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} />
              <YAxis type="number" dataKey="D" name="D" domain={["auto", "auto"]} tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} />
              <ZAxis range={[30, 30]} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ stroke: MUTED }} />
              <Scatter data={scatter} fill={ACCENT} fillOpacity={0.7} />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="chart-card">
        <h3>Largest lakes</h3>
        <table className="lakes">
          <thead>
            <tr>
              <th>#</th>
              {metrics.has("area") && <th>Area</th>}
              {metrics.has("perimeter") && <th>Perimeter</th>}
              {metrics.has("fractal") && <th>D</th>}
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 12).map((r, i) => (
              <tr key={r.label}>
                <td>{i + 1}</td>
                {metrics.has("area") && <td>{fmtArea(r.area_m2)}</td>}
                {metrics.has("perimeter") && <td>{fmtLen(r.perimeter_m)}</td>}
                {metrics.has("fractal") && <td>{r.fractal_dimension?.toFixed(3) ?? "—"}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
