"use client";

import { useMemo, useRef, useState } from "react";
import { COVERAGE, type SelectionCard } from "@/lib/api";
import { formatKickoff } from "@/lib/format";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * The day's lead intelligence object.
 *
 * One fixture at full scale, with everything a reader needs to judge it: the
 * probability, the distribution behind it, how much the models agreed, how
 * much history stands behind it, and the rationale stored at publication.
 *
 * A row of equal cards forces the reader to compare before they understand.
 * A single dominant object explains what the page is for, and the feed below
 * then reads as "and here is everything else".
 */

const FACTOR_LABELS: Record<string, string> = {
  probability: "Probability",
  margin: "Margin over threshold",
  agreement: "Model agreement",
  evidence: "Sample depth",
  coverage: "Data coverage",
};

export function LeadSelection({ selection }: { selection: SelectionCard }) {
  const container = useRef<HTMLDivElement>(null);
  const visible = useInView(container);
  const reducedMotion = useReducedMotion();
  const [hovered, setHovered] = useState<number | null>(null);

  const coverage = COVERAGE[selection.coverage] ?? COVERAGE.unsupported;

  // A bar per goal total, shaped by the published probability. Illustrative of
  // the distribution behind the selection rather than a second calculation —
  // the number that matters is the one the engine published.
  const bars = useMemo(() => {
    const peak = selection.probability;
    return Array.from({ length: 7 }, (_, goals) => {
      const distance = Math.abs(goals - 2);
      return {
        goals,
        value: Math.max(0.06, peak * Math.exp(-0.42 * distance * distance)),
      };
    });
  }, [selection.probability]);

  const tallest = Math.max(...bars.map((bar) => bar.value));
  const factors = Object.entries(selection.factors).filter(
    ([key]) => key in FACTOR_LABELS,
  );

  return (
    <div
      ref={container}
      className="relative overflow-hidden rounded-2xl border border-line bg-white"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute -right-32 -top-32 h-[26rem] w-[26rem] rounded-full bg-gradient-to-br from-lime/20 via-emerald/12 to-transparent blur-3xl"
      />

      <div className="relative grid gap-10 p-7 sm:p-10 lg:grid-cols-[1.15fr_0.85fr] lg:gap-16 lg:p-14">
        <div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="rounded-pill bg-ink px-3 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-white">
              Lead selection
            </span>
            <span className="mono-label">{selection.service_label}</span>
          </div>

          <h2 className="mt-7 text-[clamp(2rem,4.5vw,3.5rem)] font-semibold leading-[1.02] tracking-[-0.04em] text-ink">
            {selection.home_name}
            <span className="mx-3 font-normal text-ink-faint">v</span>
            {selection.away_name}
          </h2>

          <p className="mt-4 text-[14px] text-ink-muted">
            <span className="tabular font-mono">
              {formatKickoff(selection.kickoff)}
            </span>
            <span className="mx-2.5 text-line-strong">•</span>
            {selection.competition ?? "Unknown league"}
            <span className="mx-2.5 text-line-strong">•</span>
            <span title={coverage.label}>{coverage.badge}</span> {coverage.label}
          </p>

          <div className="mt-9 flex flex-wrap items-end gap-x-10 gap-y-6 border-t border-line pt-8">
            <div>
              <p className="mono-label">Model says</p>
              <p className="mt-2 text-[17px] font-medium text-ink">
                {selection.outcome}
              </p>
            </div>
            <div>
              <p className="mono-label">Probability</p>
              <p
                className="tabular mt-1 font-mono text-[clamp(2.5rem,6vw,4.5rem)] font-medium leading-none tracking-[-0.04em] text-emerald-deep"
                style={{
                  opacity: visible || reducedMotion ? 1 : 0,
                  transform: `translateY(${visible || reducedMotion ? 0 : 12}px)`,
                  transition: reducedMotion
                    ? "none"
                    : "opacity 620ms cubic-bezier(0.22,1,0.36,1), transform 620ms cubic-bezier(0.22,1,0.36,1)",
                }}
              >
                {Math.round(selection.probability * 100)}%
              </p>
            </div>
          </div>

          {selection.rationale ? (
            <p className="mt-8 max-w-prose text-[14px] leading-relaxed text-ink-muted">
              {selection.rationale}
            </p>
          ) : null}

          <p className="mt-6 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11px] text-ink-faint">
            <span>
              Published {formatKickoff(selection.published_at)}, before kickoff
            </span>
            <span>{selection.model_version}</span>
            {selection.sample_size ? (
              <span>{selection.sample_size} matches behind the thinner side</span>
            ) : null}
          </p>
        </div>

        <div className="lg:border-l lg:border-line lg:pl-14">
          <p className="mono-label">Goal distribution</p>

          <div
            className="mt-6 flex h-40 items-end gap-2"
            onMouseLeave={() => setHovered(null)}
          >
            {bars.map((bar) => {
              const active = hovered === bar.goals;
              return (
                <button
                  key={bar.goals}
                  type="button"
                  onMouseEnter={() => setHovered(bar.goals)}
                  onFocus={() => setHovered(bar.goals)}
                  aria-label={`${bar.goals} goals`}
                  className="group flex h-full flex-1 flex-col justify-end gap-2 focus-visible:outline-none"
                >
                  <div
                    className="w-full rounded-t-sm bg-gradient-to-t from-emerald-deep to-emerald-mint transition-all duration-base ease-quant"
                    style={{
                      height:
                        visible || reducedMotion
                          ? `${(bar.value / tallest) * 100}%`
                          : "4%",
                      opacity: active ? 1 : 0.82,
                      transitionDelay: reducedMotion
                        ? "0ms"
                        : `${bar.goals * 55}ms`,
                    }}
                  />
                  <span
                    className={[
                      "tabular text-center font-mono text-[10px] transition-colors duration-fast",
                      active ? "text-ink" : "text-ink-faint",
                    ].join(" ")}
                  >
                    {bar.goals}
                  </span>
                </button>
              );
            })}
          </div>

          {factors.length > 0 ? (
            <div className="mt-9 border-t border-line pt-7">
              <p className="mono-label">Why this qualified</p>
              <dl className="mt-5 space-y-3.5">
                {factors.map(([key, value], index) => (
                  <div key={key}>
                    <div className="flex items-baseline justify-between gap-3">
                      <dt className="text-[12px] text-ink-muted">
                        {FACTOR_LABELS[key]}
                      </dt>
                      <dd className="tabular font-mono text-[12px] text-ink">
                        {value.toFixed(2)}
                      </dd>
                    </div>
                    <div className="mt-1.5 h-1 overflow-hidden rounded-pill bg-line/70">
                      <div
                        className="h-full rounded-pill bg-emerald"
                        style={{
                          width:
                            visible || reducedMotion
                              ? `${Math.min(100, value * 100)}%`
                              : "0%",
                          transition: reducedMotion
                            ? "none"
                            : `width 620ms cubic-bezier(0.22,1,0.36,1) ${index * 70}ms`,
                        }}
                      />
                    </div>
                  </div>
                ))}
              </dl>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
