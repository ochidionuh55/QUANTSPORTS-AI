"use client";

import { useMemo, useRef, useState } from "react";

import { stableAlpha } from "@/lib/format";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * The joint scoreline distribution, drawn from an actual Poisson calculation.
 *
 * This is the product's central idea made visible: every market QUANTSPORT
 * publishes is a sum over these cells. Hovering one shows its probability, and
 * the totals beneath update to show which markets that cell belongs to.
 *
 * It is computed in the browser purely as an illustration of the method. No
 * published probability is ever calculated here — those come from the
 * intelligence layer, so the website and the bot cannot disagree.
 */

function poisson(k: number, lambda: number): number {
  let factorial = 1;
  for (let i = 2; i <= k; i += 1) factorial *= i;
  return (Math.exp(-lambda) * lambda ** k) / factorial;
}

const SIZE = 6;

export function ScoreMatrix({
  lambdaHome = 1.92,
  lambdaAway = 0.84,
}: {
  lambdaHome?: number;
  lambdaAway?: number;
}) {
  const [hovered, setHovered] = useState<[number, number] | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const visible = useInView(container);
  const reducedMotion = useReducedMotion();

  const { cells, peak, markets } = useMemo(() => {
    const grid: { home: number; away: number; p: number }[] = [];
    for (let home = 0; home < SIZE; home += 1) {
      for (let away = 0; away < SIZE; away += 1) {
        grid.push({
          home,
          away,
          p: poisson(home, lambdaHome) * poisson(away, lambdaAway),
        });
      }
    }
    const sum = (test: (h: number, a: number) => boolean) =>
      grid.filter((c) => test(c.home, c.away)).reduce((t, c) => t + c.p, 0);

    const ordered = [...grid].sort((a, b) => b.p - a.p);
    const ranked = grid.map((cell) => ({
      ...cell,
      rank: ordered.findIndex((c) => c.home === cell.home && c.away === cell.away),
    }));

    return {
      cells: ranked,
      peak: Math.max(...grid.map((c) => c.p)),
      markets: {
        home: sum((h, a) => h > a),
        draw: sum((h, a) => h === a),
        away: sum((h, a) => h < a),
        over: sum((h, a) => h + a > 2.5),
        btts: sum((h, a) => h > 0 && a > 0),
      },
    };
  }, [lambdaHome, lambdaAway]);

  const active = hovered
    ? cells.find((c) => c.home === hovered[0] && c.away === hovered[1])
    : null;

  return (
    <div className="relative" ref={container}>
      <div className="mb-5 flex items-baseline justify-between">
        <span className="mono-label">Joint score distribution</span>
        <span className="tabular font-mono text-[11px] text-ink-faint">
          λH {lambdaHome.toFixed(2)} · λA {lambdaAway.toFixed(2)}
        </span>
      </div>

      <div
        className="grid gap-1.5"
        style={{ gridTemplateColumns: `repeat(${SIZE}, minmax(0, 1fr))` }}
        onMouseLeave={() => setHovered(null)}
      >
        {cells.map((cell) => {
          const intensity = cell.p / peak;
          const settled = visible || reducedMotion;
          const isActive =
            hovered?.[0] === cell.home && hovered?.[1] === cell.away;
          return (
            <button
              key={`${cell.home}-${cell.away}`}
              type="button"
              onMouseEnter={() => setHovered([cell.home, cell.away])}
              onFocus={() => setHovered([cell.home, cell.away])}
              aria-label={`${cell.home}-${cell.away}, ${(cell.p * 100).toFixed(1)} percent`}
              className={[
                "relative aspect-square rounded-xs",
                "transition-all duration-fast ease-quant",
                "focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald",
                isActive ? "scale-110 ring-2 ring-emerald" : "",
              ].join(" ")}
              style={{
                // Rounded to a fixed precision so the value the server
                // serialises is byte-identical to the one the browser
                // computes. An unrounded float here is the classic cause of a
                // hydration mismatch.
                background: `rgba(5,184,92,${stableAlpha(0.06 + intensity * 0.82)})`,

                // The distribution assembles itself: cells resolve in order of
                // probability, so the likeliest scorelines arrive first and the
                // reader watches the shape form rather than meeting it whole.
                // Reduced motion gets the finished state immediately.
                opacity: settled ? 1 : 0,
                transform: settled ? "scale(1)" : "scale(0.72)",
                transitionProperty: "opacity, transform, background",
                transitionDuration: reducedMotion ? "0ms" : "520ms",
                transitionTimingFunction: "cubic-bezier(0.22,1,0.36,1)",
                transitionDelay: reducedMotion
                  ? "0ms"
                  : `${Math.round(cell.rank * 18)}ms`,
              }}
            >
              <span
                className="tabular absolute inset-0 grid place-items-center font-mono text-[9px]"
                style={{ color: intensity > 0.45 ? "#fff" : "#5B6B66" }}
              >
                {cell.home}–{cell.away}
              </span>
            </button>
          );
        })}
      </div>

      <div className="mt-5 h-[3.25rem]">
        {active ? (
          <div className="animate-rise">
            <div className="tabular font-mono text-2xl font-medium text-ink">
              {(active.p * 100).toFixed(1)}%
            </div>
            <div className="mt-0.5 text-xs text-ink-muted">
              {active.home}–{active.away} · counts toward{" "}
              {[
                active.home > active.away
                  ? "Home"
                  : active.home === active.away
                    ? "Draw"
                    : "Away",
                active.home + active.away > 2.5 ? "Over 2.5" : "Under 2.5",
                active.home > 0 && active.away > 0 ? "BTTS" : "No BTTS",
              ].join(" · ")}
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-x-6 gap-y-1.5">
            {[
              ["P(H)", markets.home],
              ["P(D)", markets.draw],
              ["P(A)", markets.away],
              ["O2.5", markets.over],
              ["BTTS", markets.btts],
            ].map(([label, value]) => (
              <span key={label as string} className="font-mono text-[11px]">
                <span className="text-ink-faint">{label as string}</span>{" "}
                <span className="tabular text-ink">
                  {((value as number) * 100).toFixed(1)}%
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      <p className="mt-4 max-w-sm text-xs leading-relaxed text-ink-faint">
        Every supported market is a sum over these cells. Internally consistent
        by design — no two can contradict each other.
      </p>
    </div>
  );
}
