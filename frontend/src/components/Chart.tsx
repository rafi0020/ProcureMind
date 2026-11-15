import type { ChartSpec } from "@/lib/types";

const W = 720, H = 260, PAD = { l: 56, r: 12, t: 12, b: 34 };

function ticks(min: number, max: number): number[] {
  const span = max - min || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / 4)));
  const nice = [1, 2, 5, 10].map((m) => m * step).find((s) => span / s <= 5) ?? step * 10;
  const out: number[] = [];
  for (let v = Math.ceil(min / nice) * nice; v <= max + 1e-9; v += nice) out.push(v);
  return out;
}

function short(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(v % 1e3 ? 1 : 0)}K`;
  if (a > 0 && a < 1) return `${(v * 100).toFixed(0)}%`;
  return String(Math.round(v * 100) / 100);
}

/** Draws a chart specification produced by the agent's `chart` tool, or built from API data. */
export function Chart({ spec }: { spec: ChartSpec }) {
  const values = spec.series.flatMap((s) => s.data).filter((v): v is number => typeof v === "number");
  if (!values.length || !spec.labels.length) return <p className="muted">No data to chart.</p>;
  const n = spec.labels.length;
  const waterfall = spec.kind === "waterfall";
  let running = 0;
  const steps = waterfall ? (spec.series[0].data.map((v) => { const from = running; running += v ?? 0; return [from, running]; }) as [number, number][]) : [];
  const all = waterfall ? steps.flat() : values;
  const max = Math.max(0, ...all), min = Math.min(spec.kind === "line" ? Math.min(...all) : 0, ...all);
  const pad = (max - min) * 0.08 || 1;
  const lo = min < 0 || spec.kind === "line" ? min - pad : 0, hi = max + pad;
  const x = (i: number) => PAD.l + ((i + 0.5) * (W - PAD.l - PAD.r)) / n;
  const y = (v: number) => PAD.t + ((hi - v) * (H - PAD.t - PAD.b)) / (hi - lo);
  const band = (W - PAD.l - PAD.r) / n;
  const every = Math.ceil(n / 12);
  return (
    <figure className="grid gap-2 m-0">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={spec.title}>
        {ticks(lo, hi).map((t) => (
          <g key={t}>
            <line className="axis" x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
            <text x={PAD.l - 8} y={y(t) + 4} textAnchor="end">{short(t)}</text>
          </g>
        ))}
        {spec.labels.map((label, i) => i % every === 0 && (
          <text key={i} x={x(i)} y={H - 12} textAnchor="middle">{label.length > 10 ? label.slice(0, 10) : label}</text>
        ))}
        {spec.kind === "line" && spec.series.map((s, si) => (
          <path key={s.name} className={`line s${si % 4}`}
            d={s.data.map((v, i) => (v == null ? "" : `${i === 0 || s.data[i - 1] == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join(" ")} />
        ))}
        {spec.kind === "bar" && spec.series.map((s, si) => {
          const w = Math.min(46, (band * 0.72) / spec.series.length);
          return s.data.map((v, i) => v != null && (
            <rect key={`${si}-${i}`} className={v < 0 ? "neg" : `s${si % 4}`} x={x(i) - (w * spec.series.length) / 2 + si * w} width={w - 2}
              y={Math.min(y(v), y(0))} height={Math.max(1, Math.abs(y(v) - y(0)))} rx={2} />
          ));
        })}
        {waterfall && steps.map(([from, to], i) => (
          <rect key={i} className={to < from ? "neg" : "s0"} x={x(i) - Math.min(46, band * 0.6) / 2} width={Math.min(46, band * 0.6)}
            y={Math.min(y(from), y(to))} height={Math.max(1, Math.abs(y(from) - y(to)))} rx={2} />
        ))}
      </svg>
      {spec.series.length > 1 && (
        <figcaption className="legend">
          {spec.series.map((s, si) => (<span key={s.name}><i className={`s${si % 4}`} />{s.name}</span>))}
        </figcaption>
      )}
    </figure>
  );
}
