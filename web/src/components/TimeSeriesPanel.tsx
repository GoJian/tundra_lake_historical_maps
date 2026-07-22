import { useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, ReferenceLine,
} from "recharts";
import type { JobProgress, TimeseriesResult } from "../api";

const ACCENT = "#22d3ee";
const ACCENT2 = "#a78bfa";
const GRID = "#334155";
const MUTED = "#94a3b8";

const fmtEta = (s: number | null | undefined) =>
  s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;

interface Point {
  label: string;
  count: number | null;
  areaKm2: number | null;
  fractal: number | null;
}

export default function TimeSeriesPanel({
  status, progress, result, cadence, selectedLabel, onPickLabel,
}: {
  status: "queued" | "running" | "done" | "error";
  progress: JobProgress | null | undefined;
  result: TimeseriesResult | null;
  cadence: string;
  selectedLabel: string | null;
  onPickLabel: (label: string) => void;
}) {
  // final result carries the full series; while running we chart the live partial
  const series = result?.series ?? progress?.series ?? [];
  const data: Point[] = useMemo(
    () => series.map((e) => ({
      label: e.label,
      count: e.summary?.lake_count ?? null,
      areaKm2: e.summary ? e.summary.total_area_m2 / 1e6 : null,
      fractal: e.summary?.mean_fractal_dimension ?? null,
    })),
    [series],
  );
  const hasFractal = data.some((d) => d.fractal != null && !Number.isNaN(d.fractal));

  const done = progress?.done ?? (result ? data.length : 0);
  const total = progress?.total ?? data.length;
  // prefer the server's overall pct (includes within-frame progress) so the bar
  // moves during a long single step instead of sitting at 0%
  const pct = progress?.pct ?? (total ? Math.round((done / total) * 100) : 0);
  const running = status === "running" || status === "queued";

  const tooltipStyle = { background: "#1e293b", border: `1px solid ${GRID}`, borderRadius: 8, color: "#e2e8f0", fontSize: 12 };
  const axisX = <XAxis dataKey="label" tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} interval="preserveStartEnd" />;
  const marker = selectedLabel
    ? <ReferenceLine x={selectedLabel} stroke={MUTED} strokeDasharray="3 3" />
    : null;

  const onClick = (e: any) => { const l = e?.activeLabel; if (l) onPickLabel(String(l)); };

  return (
    <div className="results">
      <h2>Lake change over time</h2>

      <div className="ts-progress">
        <div className="ts-progress-head">
          <span>
            {running
              ? (progress?.message ?? `Segmenting ${done}/${total}…`)
              : status === "error" ? "Run failed" : `Done · ${total} time steps`}
          </span>
          {running && <span className="hint">~{fmtEta(progress?.eta_s)} left</span>}
        </div>
        <div className="progress-track">
          <div className={`progress-fill${running ? " anim" : ""}`} style={{ width: `${pct}%` }} />
        </div>
        {running && progress?.elapsed_s != null && (
          <div className="hint">elapsed {fmtEta(progress.elapsed_s)}</div>
        )}
      </div>

      {data.length > 0 && (
        <>
          <div className="chart-card">
            <h3>Lake count per {cadence === "annual" ? "year" : cadence === "monthly" ? "month" : "quarter"}</h3>
            <ResponsiveContainer width="100%" height={150}>
              <LineChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -18 }} onClick={onClick}>
                <CartesianGrid stroke={GRID} strokeDasharray="2 2" vertical={false} />
                {axisX}
                <YAxis tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} cursor={{ stroke: MUTED }} />
                {marker}
                <Line type="monotone" dataKey="count" stroke={ACCENT} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="chart-card">
            <h3>Total lake area (km²) over time</h3>
            <ResponsiveContainer width="100%" height={150}>
              <LineChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -6 }} onClick={onClick}>
                <CartesianGrid stroke={GRID} strokeDasharray="2 2" vertical={false} />
                {axisX}
                <YAxis tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} width={48} />
                <Tooltip contentStyle={tooltipStyle} cursor={{ stroke: MUTED }} formatter={(v: any) => [`${Number(v).toFixed(2)} km²`, "area"]} />
                {marker}
                <Line type="monotone" dataKey="areaKm2" stroke={ACCENT} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {hasFractal && (
            <div className="chart-card">
              <h3>Mean boundary fractal D over time</h3>
              <ResponsiveContainer width="100%" height={150}>
                <LineChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -6 }} onClick={onClick}>
                  <CartesianGrid stroke={GRID} strokeDasharray="2 2" vertical={false} />
                  {axisX}
                  <YAxis domain={["auto", "auto"]} tick={{ fill: MUTED, fontSize: 10 }} stroke={GRID} width={48} />
                  <Tooltip contentStyle={tooltipStyle} cursor={{ stroke: MUTED }} formatter={(v: any) => [Number(v).toFixed(3), "D"]} />
                  {marker}
                  <Line type="monotone" dataKey="fractal" stroke={ACCENT2} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
          <div className="hint">Click a chart point to jump the map to that time step.</div>
        </>
      )}
    </div>
  );
}
