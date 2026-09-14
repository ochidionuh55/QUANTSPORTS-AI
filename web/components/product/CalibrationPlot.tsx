"use client";

import { useMemo, useRef, useState } from "react";
import type { ServiceRecord } from "@/lib/api";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * Predicted against observed, as a scatter.
 *
 * The diagonal is perfect calibration. A service on the line delivers what it
 * claims; one below it promises more than it delivers, which is the dangerous
 * direction and therefore the one that must be impossible to miss.
 *
 * This is the most important visual on the site. It is the argument no
 * competitor will copy, because copying it means publishing their own misses.
 */
export function CalibrationPlot({ records }: { records: ServiceRecord[] }) {
  const container = useRef<HTMLDivElement>(null);
  const visible = useInView(container);
  const reducedMotion = useReducedMotion();
  const [hovered, setHovered] = useState<string | null>(null);

  const points = useMemo(
    () =>
      records
        .filter(
          (record) =>
            record.meaningful &&
            record.actual_rate !== null &&
            record.expected_rate !== null,
        )
        .map((record) => ({
          key: record.key,
          label: record.label.replace(/^[^\s]+\s/, ""),
          x: record.expected_rate ?? 0,
          y: record.actual_rate ?? 0,
          gap: record.gap ?? 0,
          settled: record.won + record.lost,
        })),
    [records],
  );

  if (points.length === 0) return null;

  // The plot is drawn over the range the data occupies rather than 0–100, so
  // services clustered between 60% and 90% do not collapse into one corner.
  const values = points.flatMap((point) => [point.x, point.y]);
  const low = Math.max(0, Math.min(...values) - 0.08);
  const high = Math.min(1, Math.max(...values) + 0.08);
  const span = high - low || 1;

  const toX = (value: number) => ((value - low) / span) * 100;
  const toY = (value: number) => 100 - ((value - low) / span) * 100;

  const active = points.find((point) => point.key === hovered);

  return (
    <div ref={container} className="rounded-lg border border-line bg-white p-6 sm:p-9">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="mono-label">Calibration</p>
          <h3 className="mt-3 text-title font-semibold text-ink">
            Do we deliver what we claim?
          </h3>
        </div>
        <span className="tabular font-mono text-[11px] text-ink-faint">
          {points.length} services measured
        </span>
      </div>

      <div className="relative mt-8 aspect-square w-full sm:aspect-[4/3]">
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          className="absolute inset-0 h-full w-full"
          aria-hidden
        >
          <defs>
            <linearGradient id="cal-band" x1="0" y1="1" x2="1" y2="0">
              <stop offset="0%" stopColor="#05B85C" stopOpacity="0.06" />
              <stop offset="100%" stopColor="#A9FF3F" stopOpacity="0.12" />
            </linearGradient>
          </defs>

          {[0.25, 0.5, 0.75].map((step) => (
            <g key={step}>
              <line
                x1={step * 100}
                y1="0"
                x2={step * 100}
                y2="100"
                stroke="#E3EFE9"
                strokeWidth="0.3"
              />
              <line
                x1="0"
                y1={step * 100}
                x2="100"
                y2={step * 100}
                stroke="#E3EFE9"
                strokeWidth="0.3"
              />
            </g>
          ))}

          {/* Honest band: within five points of the diagonal. */}
          <polygon
            points="0,5 95,100 100,100 100,95 5,0 0,0"
            fill="url(#cal-band)"
          />

          <line
            x1="0"
            y1="100"
            x2="100"
            y2="0"
            stroke="#006B45"
            strokeWidth="0.5"
            strokeDasharray="2 2"
            opacity="0.5"
          />
        </svg>

        {points.map((point, index) => {
          const isActive = point.key === hovered;
          const honest = point.gap >= -0.05;
          const size = Math.min(26, 10 + Math.sqrt(point.settled) * 0.6);

          return (
            <button
              key={point.key}
              type="button"
              onMouseEnter={() => setHovered(point.key)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(point.key)}
              onBlur={() => setHovered(null)}
              aria-label={`${point.label}: predicted ${Math.round(point.x * 100)} percent, actual ${Math.round(point.y * 100)} percent`}
              className="absolute rounded-pill focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald"
              style={{
                left: `${toX(point.x)}%`,
                top: `${toY(point.y)}%`,
                width: size,
                height: size,
                marginLeft: -size / 2,
                marginTop: -size / 2,
                background: honest
                  ? "rgba(5,184,92,0.85)"
                  : "rgba(194,65,12,0.85)",
                boxShadow: isActive
                  ? "0 0 0 4px rgba(5,184,92,0.18)"
                  : "0 1px 3px rgba(0,50,35,0.18)",
                opacity: visible || reducedMotion ? 1 : 0,
                transform: `scale(${visible || reducedMotion ? (isActive ? 1.25 : 1) : 0.4})`,
                transitionProperty: "opacity, transform, box-shadow",
                transitionDuration: reducedMotion ? "0ms" : "520ms",
                transitionTimingFunction: "cubic-bezier(0.22,1,0.36,1)",
                transitionDelay: reducedMotion ? "0ms" : `${index * 45}ms`,
              }}
            />
          );
        })}

        <span className="absolute bottom-2 right-3 font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          predicted →
        </span>
        <span className="absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          ↑ actual
        </span>
      </div>

      <div className="mt-6 min-h-[3.5rem] border-t border-line pt-5">
        {active ? (
          <div className="animate-rise">
            <p className="text-[15px] font-medium text-ink">{active.label}</p>
            <p className="tabular mt-1 font-mono text-[13px] text-ink-muted">
              said {(active.x * 100).toFixed(1)}% · delivered{" "}
              {(active.y * 100).toFixed(1)}% ·{" "}
              <span
                className={
                  active.gap >= -0.05 ? "text-emerald-deep" : "text-[#C2410C]"
                }
              >
                {active.gap >= 0 ? "+" : ""}
                {(active.gap * 100).toFixed(1)}%
              </span>{" "}
              · {active.settled} settled
            </p>
          </div>
        ) : (
          <p className="max-w-prose text-[13px] leading-relaxed text-ink-muted">
            On the dashed line means a service delivers exactly what it
            predicts. Below it means overconfident — promising more than it
            returns, which costs a reader money while telling them they are
            winning. Larger dots carry more settled selections.
          </p>
        )}
      </div>
    </div>
  );
}
