import { polyline } from '@/lib/view';

/** A tiny line of readings. Describes, never judges: no coloured thresholds. */
export function Sparkline({ points, width = 260, height = 60, label }: {
  points: { t: number; v: number }[]; width?: number; height?: number; label: string;
}) {
  if (points.length === 0) return <span className="muted">No {label} readings yet.</span>;
  const last = points[points.length - 1];
  return (
    <div>
      <svg width={width} height={height} role="img" aria-label={`${label} over time`}>
        <polyline fill="none" stroke="#2f5d8a" strokeWidth="2" points={polyline(points, width, height)} />
      </svg>
      <div className="muted">{label}: latest {Math.round(last.v * 10) / 10} ({points.length} readings)</div>
    </div>
  );
}
