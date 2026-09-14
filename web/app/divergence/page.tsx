import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { COVERAGE, getDivergence } from "@/lib/api";
import { formatKickoff } from "@/lib/format";

export const metadata: Metadata = {
  title: "Model–Market Divergence",
  description:
    "Fixtures where our models reach a materially different conclusion from the bookmaker. A disagreement report, not a value signal.",
};

export const revalidate = 300;

export default async function DivergencePage() {
  const items = (await getDivergence()) ?? [];

  return (
    <>
      <PageHeader
        label="Divergence"
        title="Where we disagree with the price."
        lead="Fixtures whose mathematics leads us somewhere the market has not gone. We show the disagreement and say nothing about who is right."
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        {items.length === 0 ? (
          <div className="rounded-lg border border-line bg-white p-12 text-center sm:p-16">
            <p className="mono-label">No material disagreement</p>
            <h2 className="mx-auto mt-5 max-w-xl text-title font-semibold text-ink">
              Our models and the market agree today.
            </h2>
            <p className="mx-auto mt-5 max-w-prose leading-relaxed text-ink-muted">
              That is the usual state of things. Bookmakers are good at this,
              and a day with no material disagreement is not a day the product
              failed.
            </p>
          </div>
        ) : (
          <div className="border-t border-line">
            {items.map((item, index) => {
              const coverage =
                COVERAGE[item.coverage] ?? COVERAGE.unsupported;
              return (
                <article
                  key={item.fixture_id}
                  className="grid items-center gap-x-6 gap-y-4 border-b border-line py-7 transition-colors duration-base ease-quant hover:bg-surface sm:grid-cols-[2.5rem_1fr_auto] sm:px-4"
                >
                  <span className="tabular font-mono text-xs text-ink-faint">
                    {String(index + 1).padStart(2, "0")}
                  </span>

                  <div className="min-w-0">
                    <h3 className="text-[18px] font-semibold leading-snug tracking-[-0.02em] text-ink">
                      {item.home_name}
                      <span className="mx-2 font-normal text-ink-faint">v</span>
                      {item.away_name}
                    </h3>
                    <p className="mt-1.5 truncate text-[12px] text-ink-muted">
                      <span className="tabular font-mono">
                        {formatKickoff(item.kickoff).replace(" UTC", "")}
                      </span>
                      <span className="mx-2 text-line-strong">•</span>
                      {item.competition ?? "Unknown league"}
                      <span className="mx-2 text-line-strong">•</span>
                      <span title={coverage.label}>{coverage.badge}</span>
                      <span className="mx-2 text-line-strong">•</span>
                      {item.sample} matches behind the thinner side
                    </p>
                  </div>

                  {/* Both figures, always. A divergence shown without the price
                      it diverges from invites the reader to assume we think we
                      are right. */}
                  <div className="flex items-end gap-6 sm:gap-9">
                    <div>
                      <p className="mono-label">We say</p>
                      <p className="tabular mt-1 font-mono text-2xl font-medium text-emerald-deep">
                        {Math.round(item.model_probability * 100)}%
                      </p>
                      <p className="tabular font-mono text-[11px] text-ink-faint">
                        {item.model_odds.toFixed(2)}
                      </p>
                    </div>
                    <div>
                      <p className="mono-label">Market</p>
                      <p className="tabular mt-1 font-mono text-2xl font-medium text-ink">
                        {Math.round(item.market_probability * 100)}%
                      </p>
                      <p className="tabular font-mono text-[11px] text-ink-faint">
                        {item.market_odds.toFixed(2)}
                      </p>
                    </div>
                    <div className="hidden sm:block">
                      <p className="mono-label">{item.outcome}</p>
                      <p className="tabular mt-1 font-mono text-2xl font-medium text-emerald">
                        +{Math.round(item.gap * 100)}
                      </p>
                      <p className="font-mono text-[11px] text-ink-faint">
                        points
                      </p>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="bg-night-deep py-20 text-white sm:py-24">
        <div className="mx-auto max-w-shell px-5 sm:px-6">
          <p className="font-mono text-mono uppercase text-emerald-mint">
            What this is, and is not
          </p>
          <h2 className="mt-6 max-w-3xl text-display font-semibold">
            A disagreement. Not a value signal.
          </h2>
          <p className="mt-7 max-w-prose text-lead text-white/70">
            A value signal would say the market is wrong and this price is worth
            taking. Making that claim requires having shown our models beat
            closing prices. We tested exactly that — across nine seasons of
            football and eleven of basketball — and measured no advantage.
            Where we have disagreed with the price before, the price was right.
          </p>
          <p className="mt-5 max-w-prose text-sm text-white/45">
            We publish these because a real disagreement is worth seeing, and
            because the record will show over time whether they carry
            information. Today they are a curiosity with evidence attached.
          </p>
        </div>
      </section>
    </>
  );
}
