import { Button } from "@/components/ui/Button";
import { ScoreMatrix } from "@/components/hero/ScoreMatrix";
import { ProbabilityField } from "@/components/hero/ProbabilityField";
import { MarketDerivation } from "@/components/hero/MarketDerivation";
import { CommandBar } from "@/components/hero/CommandBar";
import { getCommunity, getSummary } from "@/lib/api";
import { Community } from "@/components/product/Community";
import { formatCount } from "@/lib/format";

export const revalidate = 900;

export default async function Home() {
  const [summary, community] = await Promise.all([
    getSummary(),
    getCommunity(),
  ]);

  return (
    <>
      {/* Screen 1 — the statement.
          Full-viewport and left-aligned. The mathematics occupies the space
          rather than sitting in a card beside the copy, which is what made the
          previous version read as a template with a widget bolted on. */}
      <section className="relative isolate flex min-h-[92vh] items-center overflow-hidden bg-white">
        <ProbabilityField />

        {/* A wash so the type stays legible over the field without hiding it. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 bg-gradient-to-r from-white via-white/85 to-transparent"
        />

        <div className="relative mx-auto w-full max-w-shell px-5 py-24 sm:px-6">
          <div className="max-w-3xl">
            <p className="mono-label animate-rise">
              QUANTSPORT Intelligence Engine
            </p>

            <h1
              className="mt-8 animate-rise text-hero font-semibold text-ink"
              style={{ animationDelay: "60ms" }}
            >
              Football intelligence.
              <br />
              <span className="text-emerald">Quantified.</span>
            </h1>

            <p
              className="mt-10 max-w-lg animate-rise text-lead text-ink-muted"
              style={{ animationDelay: "130ms" }}
            >
              Football is full of opinions. We start with probabilities.
            </p>

            <div
              className="mt-11 flex animate-rise flex-wrap gap-3"
              style={{ animationDelay: "200ms" }}
            >
              <Button href="/today">Explore Today</Button>
              <Button
                href="https://t.me/quantpredictzbot"
                variant="secondary"
                external
              >
                Open Telegram
              </Button>
            </div>

            {/* Figures come from the database. If it is unreachable we show a
                dash rather than a number nobody measured. */}
            <div
              className="mt-16 flex animate-rise flex-wrap items-center gap-x-3 gap-y-2 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-faint"
              style={{ animationDelay: "260ms" }}
            >
              {/* The corpus triple: three figures describing one universe —
                  the historical data the model is built on. All three are
                  "data held", never "publishing today". Labels say "on record"
                  so corpus competitions cannot be read as today's coverage. */}
              <span className="tabular text-ink">
                {summary ? formatCount(summary.matches) : "—"}
              </span>
              <span>historical matches</span>
              <span className="text-line-strong">•</span>
              <span className="tabular text-ink">
                {summary ? summary.corpus_competitions : "—"}
              </span>
              <span>competitions on record</span>
              <span className="text-line-strong">•</span>
              <span className="tabular text-ink">
                {summary ? formatCount(summary.teams) : "—"}
              </span>
              <span>canonical teams</span>
            </div>

            <p
              className="mt-10 animate-rise text-sm italic text-emerald-deep"
              style={{ animationDelay: "320ms" }}
            >
              Ask the Data.
            </p>
          </div>
        </div>
      </section>

      {/* Screen 3 — the matrix the hero field resolves into. */}
      <section className="border-y border-line bg-warm">
        <div className="mx-auto max-w-shell px-5 py-20 sm:px-6 sm:py-28">
          <div className="grid gap-12 lg:grid-cols-[0.85fr_1.15fr] lg:gap-20">
            <div className="lg:sticky lg:top-28 lg:self-start">
              <p className="mono-label">The distribution</p>
              <h2 className="mt-6 text-display font-semibold text-ink">
                One match.
                <br />
                Thousands of outcomes.
              </h2>
              <p className="mt-7 max-w-prose leading-relaxed text-ink-muted">
                A football match does not have an answer. It has a distribution
                — every scoreline with a probability attached, estimated from
                how the two sides have actually scored and conceded.
              </p>
              <p className="mt-4 max-w-prose leading-relaxed text-ink-muted">
                Hover a cell to see its probability, and which markets that
                scoreline feeds.
              </p>
            </div>

            <div className="rounded-lg border border-line bg-white p-5 shadow-float sm:p-8">
              <ScoreMatrix />
            </div>
          </div>
        </div>
      </section>

      {/* Screen 04 — one model, every market */}
      <section className="mx-auto max-w-shell px-5 py-20 sm:px-6 sm:py-28">
        <MarketDerivation />
      </section>

      {/* Screen 05 — Ask the Data */}
      <section className="light-field grain relative overflow-hidden border-y border-line">
        <div className="mx-auto max-w-shell px-5 py-20 sm:px-6 sm:py-28">
          <CommandBar />
        </div>
      </section>

      <Community stats={community} />

      {/* Screen — the honesty, as a cinematic dark section */}
      <section className="bg-night-deep py-28 text-white">
        <div className="mx-auto max-w-shell px-6">
          <p className="font-mono text-mono uppercase text-emerald-mint">
            Nothing hidden
          </p>
          <h2 className="mt-6 max-w-3xl text-display font-semibold">
            We test before we trust.
          </h2>
          <p className="mt-7 max-w-prose text-lead text-white/70">
            Our models are measured against outcomes and market benchmarks
            wherever valid data exists. We publish what the evidence supports —
            not what sounds impressive.
          </p>

          <div className="mt-14 grid gap-px overflow-hidden rounded-lg border border-white/10 bg-white/10 sm:grid-cols-3">
            {[
              {
                value: "Calibrated",
                label: "Outcomes happen about as often as we say they will",
              },
              {
                value: "No proven edge",
                label:
                  "Tested against closing prices more than once. The market won",
              },
              {
                value: "Published first",
                label:
                  "Every selection timestamped before kickoff, never edited after",
              },
            ].map((item) => (
              <div key={item.value} className="bg-night-deep p-8">
                <div className="text-title font-semibold text-emerald-mint">
                  {item.value}
                </div>
                <p className="mt-3 text-sm leading-relaxed text-white/60">
                  {item.label}
                </p>
              </div>
            ))}
          </div>

          <p className="mt-10 max-w-prose text-sm text-white/45">
            That is not a weakness we are admitting. It is the finding, and a
            record you can check is worth more than a claim you cannot.
          </p>
        </div>
      </section>

      {/* Close */}
      <section className="mx-auto max-w-shell px-6 py-28">
        <div className="max-w-2xl">
          <h2 className="text-display font-semibold text-ink">
            The game looks different from here.
          </h2>
          <p className="mt-6 text-lead text-ink-muted">
            Probability before opinion. See what the model sees.
          </p>
          <div className="mt-9 flex flex-wrap gap-3">
            <Button href="/today">Explore QUANTSPORT AI</Button>
            <Button href="/methodology" variant="secondary">
              Read the methodology
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
