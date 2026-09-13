import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { Button } from "@/components/ui/Button";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "Free access to the evidence. A subscription for the analysis built on it.",
};

const FREE = [
  "Today's analysis — every fixture, graded",
  "Full published history, including the losses",
  "Track record for every service",
  "Methodology and data sources",
];

const PRO = [
  "Best of Today — 18 services, ten ranked fixtures each",
  "Market Explorer across 38 competitions",
  "Team Intelligence from 113,000 matches",
  "Follow selections and track your own record",
  "The evidence behind every selection",
];

export default function PricingPage() {
  return (
    <>
      <PageHeader
        label="Pricing"
        title="The evidence is free. Always."
        lead="You should be able to check what we published and how it turned out — including the losses — before deciding whether the analysis is worth paying for."
      />

      <section className="mx-auto max-w-shell px-6 py-20">
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="rounded-lg border border-line bg-white p-9">
            <p className="mono-label">Free</p>
            <p className="mt-5 text-display font-semibold text-ink">₦0</p>
            <p className="mt-3 text-ink-muted">No account required to read.</p>
            <ul className="mt-8 space-y-3.5">
              {FREE.map((item) => (
                <li key={item} className="flex gap-3 text-sm text-ink-muted">
                  <span aria-hidden className="text-emerald">
                    ✓
                  </span>
                  {item}
                </li>
              ))}
            </ul>
            <div className="mt-9">
              <Button href="/track-record" variant="secondary">
                See the record
              </Button>
            </div>
          </div>

          <div className="rounded-lg border border-emerald/30 bg-surface p-9">
            <p className="mono-label text-emerald-deep">QUANTSPORT Pro</p>
            <p className="mt-5 text-display font-semibold text-ink">
              7 days free
            </p>
            <p className="mt-3 text-ink-muted">
              No card. Long enough to see a full weekend settle.
            </p>
            <ul className="mt-8 space-y-3.5">
              {PRO.map((item) => (
                <li key={item} className="flex gap-3 text-sm text-ink-muted">
                  <span aria-hidden className="text-emerald">
                    ✓
                  </span>
                  {item}
                </li>
              ))}
            </ul>
            <div className="mt-9">
              <Button href="https://t.me/quantpredictzbot" external>
                Start on Telegram
              </Button>
            </div>
            <p className="mt-6 text-xs leading-relaxed text-ink-faint">
              Subscription pricing is being finalised. Trials run through the
              Telegram bot in the meantime.
            </p>
          </div>
        </div>

        <p className="mx-auto mt-14 max-w-prose text-center text-sm leading-relaxed text-ink-muted">
          We sell access to analysis, not outcomes. Our models are calibrated
          and have not been shown to beat bookmaker prices — a finding we
          publish rather than obscure.
        </p>
      </section>
    </>
  );
}
