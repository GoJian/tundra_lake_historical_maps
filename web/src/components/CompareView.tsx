import type { SatelliteFrame } from "../api";

// Turn a frame's XYZ tile-URL template into a static preview image of the whole
// composite COG. Each composite is built for the ROI extent, so the preview is
// exactly the region of interest for that time step.
const previewUrl = (tileUrl: string, size = 760) =>
  tileUrl.replace(/\/tiles\/WebMercatorQuad\/\{z\}\/\{x\}\/\{y\}/, `/preview/${size}x${size}.png`);

function Pane({
  frames, idx, side, onPick,
}: {
  frames: SatelliteFrame[];
  idx: number;
  side: string;
  onPick: (i: number) => void;
}) {
  const f = frames[idx];
  return (
    <figure className="cmp-pane">
      <figcaption>
        <span className="cmp-side">{side}</span>
        <select value={idx} onChange={(e) => onPick(+e.target.value)}>
          {frames.map((fr, i) => <option key={i} value={i}>{fr.label}</option>)}
        </select>
      </figcaption>
      {f?.tile_url
        ? <img src={previewUrl(f.tile_url)} alt={`Composite ${f.label}`} loading="lazy" />
        : <div className="cmp-nodata">No clear scenes for {f?.label ?? "—"}</div>}
    </figure>
  );
}

export default function CompareView({
  frames, aIdx, bIdx, onA, onB, onClose,
}: {
  frames: SatelliteFrame[];
  aIdx: number;
  bIdx: number;
  onA: (i: number) => void;
  onB: (i: number) => void;
  onClose: () => void;
}) {
  return (
    <div className="compare">
      <div className="compare-head">
        <b>Compare satellite imagery — {frames[aIdx]?.label} vs {frames[bIdx]?.label}</b>
        <button className="ghost" onClick={onClose}>✕ Close</button>
      </div>
      <div className="compare-body">
        <Pane frames={frames} idx={aIdx} side="A" onPick={onA} />
        <Pane frames={frames} idx={bIdx} side="B" onPick={onB} />
      </div>
      <div className="hint compare-foot">
        Same region, {frames.length} time steps available. Each image is the extracted composite for that period.
      </div>
    </div>
  );
}
