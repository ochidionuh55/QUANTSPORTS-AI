"use client";

import { useMemo, useRef, useState } from "react";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * Storyboard 04 — one model, every supported market.
 *
 * The same joint distribution, partitioned five different ways. Selecting a
 * market highlights the cells that satisfy it and sums them, which is
 * literally how the probability is produced — not an illustration of the idea
 * but the operation itself.
 *
 * This is the argument for the whole architecture: markets derived from one
 * table cannot contradict each other, and a reader can watch that be true.
 */

function poisson(k: number, lambda: number): number {
  let factorial = 1;
  for (let i = 2; i <= k; i += 1) factorial *= i;
  return (Math.exp(-lambda) * lambda ** k) / factorial;
}

const SIZE = 6;
const LAMBDA_HOME = 1.92;
const LAMBDA_AWAY = 0.84;

type Market = {
  key: string;
  label: string;
  outcome: string;
  holds: (home: number, away: number) => boolean;
};

const MARKETS: Market[] = [
  { key: "home", label: "1X2", outcome: "Home", holds: (h, a) => h > a },
  { key: "draw", label: "1X2", outcome: "Draw", holds: (h, a) => h === a },
  {
    key: "dc",
    label: "Double chance",
    outcome: "1X",
    holds: (h, a) => h >= a,
  },
  {
    key: "over",
    label: "Goals",
    outcome: "Over 2.5",
    holds: (h, a) => h + a > 2.5,
  },
  {
    key: "btts",
    label: "Both teams to score",
    outcome: "Yes",
    holds: (h, a) => h > 0 && a > 0,
  },
  {
    key: "combo",
    label: "Result or goals",
    outcome: "Home or Over 2.5",
    holds: (h, a) => h > a || h + a > 2.5,
  },
];

export function MarketDerivation() {
  const [selected, setSelected] = useState(MARKETS[0]);
  const container = useRef<HTMLDivElement>(null);
  const visible = useInView(container);
  const reducedMotion = useReducedMotion();

  const cells = useMemo(() => {
    const grid: { home: number; away: number; p: number }[] = [];
    for (let home = 0; home < SIZE; home += 1) {
      for (let away = 0; away < SIZE; away += 1) {
        grid.push({
          home,
          away,
          p: poisson(home, LAMBDA_HOME) * poisson(away, LAMBDA_AWAY),
        });
      }
    }
    return grid;
  }, []);

  const total = cells
    .filter((cell) => selected.holds(cell.home, cell.away))
    .reduce((sum, cell) => sum + cell.p, 0);

  const included = cells.filter((cell) =>
    selected.holds(cell.home, cell.away),
  ).length;

  return (
    <div ref={container} className="grid gap-10 lg:grid-cols-[1fr_1.1fr] lg:gap-16">
      <div>
        <p className="mono-label">One model</p>
        <h2 className="mt-6 text-display font-semibold text-ink">
          Every supported market,
          <br />
          from the same table.
        </h2>
        <p className="mt-7 max-w-prose leading-relaxed text-ink-muted">
          Select a market. The cells that satisfy it light up, and their sum is
          the probability we publish. That is the whole operation — no separate
          model per market, and therefore no way for two of them to disagree.
        </p>

        <div className="mt-8 flex flex-wrap gap-2">
          {MARKETS.map((market) => {
            const active = market.key === selected.key;
            return (
              <button
                key={market.key}
                type="button"
                onClick={() => setSelected(market)}
                className={[
                  "rounded-pill border px-4 py-2 text-sm transition-all duration-base ease-quant",
                  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald",
                  active
                    ? "border-ink bg-ink text-white"
                    : "border-line bg-white text-ink-muted hover:border-line-strong hover:text-ink",
                ].join(" ")}
              >
                {market.outcome}
              </button>
            );
          })}
        </div>

        <div className="mt-9 border-t border-line pt-7">
          <p className="mono-label">{selected.label}</p>
          <div className="mt-2 flex items-baseline gap-4">
            <span className="tabular font-mono text-display font-medium text-emerald-deep">
              {(total * 100).toFixed(1)}%
            </span>
            <span className="text-sm text-ink-faint">
              {included} of {cells.length} scorelines
            </span>
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-line bg-white p-5 shadow-float sm:p-8">
        <div className="mb-5 flex items-baseline justify-between">
          <span className="mono-label">Joint distribution</span>
          <span className="tabular font-mono text-[11px] text-ink-faint">
            λH {LAMBDA_HOME.toFixed(2)} · λA {LAMBDA_AWAY.toFixed(2)}
          </span>
        </div>

        <div
          className="grid gap-1.5"
          style={{ gridTemplateColumns: `repeat(${SIZE}, minmax(0, 1fr))` }}
        >
          {cells.map((cell) => {
            const included = selected.holds(cell.home, cell.away);
            const settled = visible || reducedMotion;
            return (
              <div
                key={`${cell.home}-${cell.away}`}
                className="relative aspect-square rounded-xs"
                style={{
                  // Cells outside the market fade rather than vanish, so the
                  // reader keeps sight of the whole distribution and can see
                  // what the market is a slice of.
                  background: included
                    ? `rgba(5,184,92,${Number((0.18 + (cell.p / 0.14) * 0.74).toFixed(3))})`
                    : "rgba(227,239,233,0.55)",
                  opacity: settled ? 1 : 0,
                  transform: settled ? "scale(1)" : "scale(0.8)",
                  transitionProperty: "background, opacity, transform",
                  transitionDuration: reducedMotion ? "0ms" : "420ms",
                  transitionTimingFunction: "cubic-bezier(0.22,1,0.36,1)",
                }}
              >
                <span
                  className="tabular absolute inset-0 grid place-items-center font-mono text-[9px]"
                  style={{
                    color: included
                      ? cell.p > 0.06
                        ? "#fff"
                        : "#006B45"
                      : "#8C9A95",
                  }}
                >
                  {cell.home}–{cell.away}
                </span>
              </div>
            );
          })}
        </div>

        <p className="mt-5 text-xs leading-relaxed text-ink-faint">
          Highlighted cells satisfy <span className="text-ink">{selected.outcome}</span>.
          Faded cells remain part of the distribution — the market is a slice of
          it, not a separate calculation.
        </p>
      </div>
    </div>
  );
}
