import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { ScoreMatrix } from "@/components/hero/ScoreMatrix";
import { Button } from "@/components/ui/Button";
import { getSummary } from "@/lib/api";
import { formatCount } from "@/lib/format";

export const metadata: Metadata = {
  title: "Methodology",
  description:
    "How QUANTSPORT estimates probabilities, what it has been tested against, and what it does not claim.",
};

export const revalidate = 900;

const TAIL =
  "Team identity is hard and wrong matches poison everything downstream, so resolution is conservative: ambiguous names are rejected rather than guessed.";

const STEPS = [
  {
    label: "01",
    title: "Count what actually happened",
    // Durable fallback used only when the API is unreachable — no counts a
    // reader could mistake for measured figures. When summary is available the
    // component replaces this with the live corpus numbers.
    body: `Matches across every competition on record, resolved to canonical clubs. ${TAIL}`,
  },
  {
    label: "02",
    title: "Estimate scoring rates",
    body: "Each side's attack and defence are estimated from recent matches, weighted toward the present. A Dixon-Coles correction adjusts the low-scoring scorelines where a plain Poisson model is known to be wrong.",
  },
  {
    label: "03",
    title: "Combine independent views",
    body: "Poisson goal expectancy, Elo ratings and recency-weighted form each produce their own estimate. They are blended in log-odds space, and where they disagree the fixture is scored as less certain rather than averaged into false confidence.",
  },
  {
    label: "04",
    title: "Derive every market from one distribution",
    body: "Result, double chance, totals, both teams to score and the combination markets are all sums over the same joint score distribution. This makes contradiction impossible — two markets from one table cannot disagree.",
  },
  {
    label: "05",
    title: "Rank, then publish before kickoff",
    body: "Selections are ranked on probability, model agreement, sample depth and coverage — not probability alone, because thin samples produce extreme estimates. Everything is written before the match starts and never edited after.",
  },
  {
    label: "06",
    title: "Settle, and show the result",
    body: "Outcomes are resolved by the same rule that produced the probability, so a market cannot be priced one way and judged another. Wins and losses both appear in the record.",
  },
];

export default async function MethodologyPage() {
  const summary = await getSummary();

  // Corpus figures come from the database, never hardcoded. If the API is
  // unreachable the static durable copy stands rather than inventing a count.
  const steps = STEPS.map((step) =>
    step.label === "01" && summary
      ? {
          ...step,
          body: `${formatCount(summary.matches)} matches across ${summary.corpus_competitions} competitions on record, resolved to ${formatCount(summary.teams)} canonical clubs. ${TAIL}`,
        }
      : step,
  );

  return (
    <>
      <PageHeader
        label="Methodology"
        title="How the numbers are made."
        lead="No black box. Every figure QUANTSPORT publishes can be traced from raw match data to the probability on your screen."
      />

      <section className="mx-auto max-w-shell px-6 py-20">
        <div className="grid gap-16 lg:grid-cols-[1fr_0.85fr]">
          <ol className="space-y-12">
            {steps.map((step) => (
              <li key={step.label} className="grid gap-5 sm:grid-cols-[3rem_1fr]">
                <span className="tabular font-mono text-sm text-emerald">
                  {step.label}
                </span>
                <div>
                  <h2 className="text-[19px] font-semibold tracking-[-0.02em] text-ink">
                    {step.title}
                  </h2>
                  <p className="mt-3 max-w-prose leading-relaxed text-ink-muted">
                    {step.body}
                  </p>
                </div>
              </li>
            ))}
          </ol>

          <aside className="lg:sticky lg:top-28 lg:self-start">
            <div className="rounded-lg border border-line bg-white p-7 shadow-float">
              <ScoreMatrix />
            </div>
          </aside>
        </div>
      </section>

      <section className="border-y border-line bg-warm">
        <div className="mx-auto max-w-shell px-6 py-20">
          <p className="mono-label">Validation</p>
          <h2 className="mt-5 max-w-2xl text-display font-semibold text-ink">
            What the testing found.
          </h2>

          <div className="mt-12 grid gap-8 md:grid-cols-3">
            {[
              {
                title: "Calibrated",
                body: "Across thousands of forecasts, outcomes occur about as often as we say they will. That is the property that makes a probability useful.",
              },
              {
                title: "No proven edge",
                body: "We measured our probabilities against closing bookmaker prices, more than once and across sports. The market was closer to the truth each time.",
              },
              {
                title: "Services withheld",
                body: "Two services were measured as overconfident — claiming materially more than they delivered — and are not published. Building something is not a reason to ship it.",
              },
            ].map((item) => (
              <div key={item.title}>
                <h3 className="text-[17px] font-semibold tracking-[-0.02em] text-ink">
                  {item.title}
                </h3>
                <p className="mt-3 leading-relaxed text-ink-muted">
                  {item.body}
                </p>
              </div>
            ))}
          </div>

          <p className="mt-12 max-w-prose leading-relaxed text-ink">
            We report this because a research tool that hides its own negative
            results is not a research tool. The value QUANTSPORT offers is
            honest probability and a record you can audit — not a promise we
            cannot keep.
          </p>
        </div>
      </section>

      <section className="mx-auto max-w-shell px-6 py-20">
        <div className="max-w-2xl">
          <h2 className="text-display font-semibold text-ink">
            See the working.
          </h2>
          <p className="mt-5 text-lead text-ink-muted">
            Every selection carries its own evidence — the ranking breakdown,
            which models ran, how much history sits behind it, and when it was
            published.
          </p>
          <div className="mt-9 flex flex-wrap gap-3">
            <Button href="/today">Explore today</Button>
            <Button href="/track-record" variant="secondary">
              See the record
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
