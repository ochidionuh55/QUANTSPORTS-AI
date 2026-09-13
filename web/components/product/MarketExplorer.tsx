"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import {
  asPercent,
  searchFixtures,
  type FixtureCard,
  type MarketOption,
} from "@/lib/api";
import { COVERAGE } from "@/lib/api";
import { formatKickoff } from "@/lib/format";

const BANDS = [
  { value: 0.5, label: "50%+" },
  { value: 0.6, label: "60%+" },
  { value: 0.7, label: "70%+" },
  { value: 0.8, label: "80%+" },
];

/**
 * Search-led market discovery.
 *
 * Not a static grid of markets: the question comes first and results follow in
 * the same view, so a reader adjusts the floor and watches the card respond
 * rather than navigating between pages to compare.
 *
 * Counts on each market button come from the same query service that returns
 * the results, so a market advertising twelve fixtures returns twelve when
 * opened. A count computed by a different path is a promise the next screen
 * may not keep.
 */
export function MarketExplorer({
  markets,
  initialMarket,
  initialResults,
}: {
  markets: MarketOption[];
  initialMarket: string;
  initialResults: FixtureCard[];
}) {
  const [market, setMarket] = useState(initialMarket);
  const [floor, setFloor] = useState(0.5);
  const [results, setResults] = useState(initialResults);
  const [pending, startTransition] = useTransition();
  const [failed, setFailed] = useState(false);

  const run = useCallback((nextMarket: string, nextFloor: number) => {
    startTransition(async () => {
      const found = await searchFixtures({
        market: nextMarket,
        minProbability: nextFloor,
      });
      // Null means the request failed, which is different from finding
      // nothing. Showing "no fixtures" for a network error would tell the
      // reader something false about the card.
      setFailed(found === null);
      setResults(found ?? []);
    });
  }, []);

  // Skip the first run: the server already resolved these results.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (!mounted) {
      setMounted(true);
      return;
    }
    run(market, floor);
  }, [market, floor, mounted, run]);

  const selected = markets.find((option) => option.key === market);

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {markets.map((option) => {
          const active = option.key === market;
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => setMarket(option.key)}
              className={[
                "rounded-pill border px-4 py-2 text-sm transition-all duration-base ease-quant",
                "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald",
                active
                  ? "border-ink bg-ink text-white"
                  : "border-line bg-white text-ink-muted hover:border-line-strong hover:text-ink",
              ].join(" ")}
            >
              {option.label}
              {option.available > 0 ? (
                <span
                  className={[
                    "tabular ml-2 font-mono text-[11px]",
                    active ? "text-white/60" : "text-ink-faint",
                  ].join(" ")}
                >
                  {option.available}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-line pt-6">
        <span className="mono-label">Probability floor</span>
        {BANDS.map((band) => (
          <button
            key={band.value}
            type="button"
            onClick={() => setFloor(band.value)}
            className={[
              "tabular rounded-pill px-3.5 py-1.5 font-mono text-xs transition-all duration-base ease-quant",
              floor === band.value
                ? "bg-surface-mint text-emerald-deep"
                : "text-ink-faint hover:text-ink",
            ].join(" ")}
          >
            {band.label}
          </button>
        ))}
      </div>

      <div
        className="mt-10 transition-opacity duration-base ease-quant"
        style={{ opacity: pending ? 0.45 : 1 }}
      >
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-title font-semibold text-ink">
            {selected?.label ?? "Results"}
          </h2>
          <span className="tabular font-mono text-[11px] text-ink-faint">
            {results.length} fixture{results.length === 1 ? "" : "s"}
          </span>
        </div>

        {failed ? (
          <div className="mt-8 rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">Results unavailable</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              We could not reach the intelligence layer. This is a connection
              problem, not a statement about today&rsquo;s card.
            </p>
          </div>
        ) : results.length === 0 ? (
          <div className="mt-8 rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">
              No fixture reaches {Math.round(floor * 100)}% for this market
            </p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              That is a real answer about today&rsquo;s football, not a gap in
              the data. Lower the floor or choose another market.
            </p>
          </div>
        ) : (
          <div className="mt-6 border-t border-line">
            {results.map((fixture, index) => {
              const coverage =
                COVERAGE[fixture.coverage] ?? COVERAGE.unsupported;
              return (
                <article
                  key={fixture.fixture_id}
                  className="grid items-center gap-x-5 gap-y-3 border-b border-line py-5 transition-colors duration-base ease-quant hover:bg-surface sm:grid-cols-[2.5rem_1fr_auto] sm:px-4"
                >
                  <span className="tabular font-mono text-xs text-ink-faint">
                    {String(index + 1).padStart(2, "0")}
                  </span>

                  <div className="min-w-0">
                    <h3 className="text-[17px] font-semibold leading-snug tracking-[-0.02em] text-ink">
                      {fixture.home_name}
                      <span className="mx-2 font-normal text-ink-faint">v</span>
                      {fixture.away_name}
                    </h3>
                    <p className="mt-1 truncate text-[13px] text-ink-muted">
                      <span className="tabular font-mono">
                        {formatKickoff(fixture.kickoff).replace(" UTC", "")}
                      </span>
                      <span className="mx-2 text-line-strong">•</span>
                      {fixture.competition ?? "Unknown league"}
                      <span className="mx-2 text-line-strong">•</span>
                      <span title={coverage.label}>{coverage.badge}</span>
                    </p>
                  </div>

                  <div className="tabular font-mono text-2xl font-medium text-emerald-deep sm:text-right">
                    {asPercent(fixture.strongest_probability)}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
