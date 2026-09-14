import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { CalibrationPlot } from "@/components/product/CalibrationPlot";
import { CountUp } from "@/components/product/CountUp";
import { getTrackRecord } from "@/lib/api";

export const metadata: Metadata = {
  title: "Track Record",
  description:
    "Every service's live performance against what it predicted. Published before kickoff, settled from the result, never edited.",
};

export const revalidate = 600;

/** One service's bar: what happened, against what was claimed. */
function RecordBar({
  actual,
  expected,
  index,
}: {
  actual: number;
  expected: number;
  index: number;
}) {
  const gap = actual - expected;
  const honest = gap >= -0.05;

  return (
    <div className="relative h-2.5 w-full overflow-hidden rounded-pill bg-line/70">
      <div
        className={[
          "h-full rounded-pill",
          honest
            ? "bg-gradient-to-r from-emerald-deep to-emerald"
            : "bg-[#C2410C]",
        ].join(" ")}
        style={{
          width: `${Math.min(100, actual * 100)}%`,
          animation: `rise 620ms cubic-bezier(0.22,1,0.36,1) ${index * 60}ms both`,
        }}
      />
      <div
        className="absolute top-0 h-full w-[2px] bg-ink"
        style={{ left: `${Math.min(100, expected * 100)}%` }}
        title="What we predicted"
      />
    </div>
  );
}

export default async function TrackRecordPage() {
  const records = await getTrackRecord();
  const all = records ?? [];
  const measured = all
    .filter((record) => record.meaningful)
    .sort((a, b) => (b.actual_rate ?? 0) - (a.actual_rate ?? 0));
  const waiting = all.filter((record) => !record.meaningful);

  const settled = all.reduce((sum, r) => sum + r.won + r.lost, 0);
  const won = all.reduce((sum, r) => sum + r.won, 0);
  const pending = all.reduce((sum, r) => sum + r.pending, 0);
  const calibrated = measured.filter((r) => (r.gap ?? 0) >= -0.05).length;

  return (
    <>
      <PageHeader
        label="Evidence"
        title="The record, including the losses."
        lead="Every selection is timestamped before kickoff, settled from the result and never edited afterwards."
      />

      {/* Headline figures. Only shown once something has settled — a page of
          zeroes says less than a sentence explaining why it is empty. */}
      {settled > 0 ? (
        <section className="border-b border-line bg-warm">
          <div className="mx-auto grid max-w-shell gap-px bg-line px-0 sm:grid-cols-4">
            {[
              { value: won, label: "Won", decimals: 0 },
              { value: settled, label: "Settled", decimals: 0 },
              {
                value: settled ? (won / settled) * 100 : 0,
                label: "Strike rate",
                decimals: 1,
                suffix: "%",
              },
              { value: pending, label: "Pending", decimals: 0 },
            ].map((item) => (
              <div key={item.label} className="bg-warm px-6 py-10 sm:px-8">
                <div className="font-mono text-[2.25rem] font-medium leading-none tracking-[-0.03em] text-ink">
                  <CountUp
                    value={item.value}
                    decimals={item.decimals}
                    suffix={item.suffix ?? ""}
                  />
                </div>
                <p className="mono-label mt-3">{item.label}</p>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        {all.length === 0 ? (
          <div className="rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">No record yet</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              Performance data is unavailable right now.
            </p>
          </div>
        ) : null}

        {measured.length > 0 ? (
          <div className="grid gap-10 lg:grid-cols-[1.05fr_0.95fr] lg:gap-14">
            <div>
              <p className="mono-label">By service</p>
              <h2 className="mt-4 text-title font-semibold text-ink">
                Actual against predicted
              </h2>
              <p className="mt-3 max-w-prose leading-relaxed text-ink-muted">
                The bar is what happened. The line is what we said would happen.
                The gap between them is the number that matters.
              </p>

              <div className="mt-9 space-y-7">
                {measured.map((record, index) => {
                  const actual = record.actual_rate ?? 0;
                  const expected = record.expected_rate ?? 0;
                  const gap = record.gap ?? 0;
                  return (
                    <div key={record.key}>
                      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                        <h3 className="text-[15px] font-medium tracking-[-0.02em] text-ink">
                          {record.label}
                        </h3>
                        <div className="tabular flex items-baseline gap-3 font-mono text-[12px]">
                          <span className="text-ink-faint">
                            {record.won}/{record.won + record.lost}
                          </span>
                          <span className="text-[15px] text-ink">
                            {(actual * 100).toFixed(1)}%
                          </span>
                          <span
                            className={
                              gap >= -0.05
                                ? "text-emerald-deep"
                                : "text-[#C2410C]"
                            }
                          >
                            {gap >= 0 ? "+" : ""}
                            {(gap * 100).toFixed(1)}
                          </span>
                        </div>
                      </div>
                      <div className="mt-2.5">
                        <RecordBar
                          actual={actual}
                          expected={expected}
                          index={index}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="lg:sticky lg:top-28 lg:self-start">
              <CalibrationPlot records={all} />
            </div>
          </div>
        ) : null}

        {waiting.length > 0 ? (
          <div className="mt-20">
            <p className="mono-label">Gathering evidence</p>
            <h2 className="mt-4 text-title font-semibold text-ink">
              Not yet enough to rate
            </h2>
            <p className="mt-3 max-w-prose leading-relaxed text-ink-muted">
              These services are publishing but have too few settled selections
              to quote a rate. A percentage from four results is noise dressed
              as evidence, so we do not print one.
            </p>

            <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {waiting.map((record) => (
                <div
                  key={record.key}
                  className="rounded-md border border-line bg-white px-5 py-4"
                >
                  <p className="text-sm font-medium text-ink">{record.label}</p>
                  <p className="tabular mt-1.5 font-mono text-[11px] text-ink-faint">
                    {record.won + record.lost} settled · {record.pending} pending
                  </p>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      <section className="relative overflow-hidden bg-night-deep py-20 text-white sm:py-28">
        <div
          aria-hidden
          className="pointer-events-none absolute -left-40 top-0 h-[30rem] w-[30rem] rounded-full bg-emerald/10 blur-3xl"
        />
        <div className="relative mx-auto max-w-shell px-5 sm:px-6">
          <p className="font-mono text-mono uppercase text-emerald-mint">
            What this means
          </p>
          <h2 className="mt-6 max-w-3xl text-display font-semibold">
            Winning at the rate you predicted is calibration, not profit.
          </h2>
          <p className="mt-7 max-w-prose text-lead text-white/70">
            A service landing 89% of the time sounds extraordinary until you
            notice it is priced around 1.10. Our models are well calibrated and
            have not been shown to beat closing prices — we have tested that
            directly, more than once.
          </p>
          <p className="mt-5 max-w-prose text-sm text-white/45">
            {measured.length > 0
              ? `${calibrated} of ${measured.length} measured services sit within five points of their own forecast.`
              : "Measured services will appear here as selections settle."}{" "}
            We publish the finding rather than obscure it. A record you can
            check is worth more than a claim you cannot.
          </p>
        </div>
      </section>
    </>
  );
}
