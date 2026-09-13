import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { getTrackRecord } from "@/lib/api";

export const metadata: Metadata = {
  title: "Track Record",
  description:
    "Every service's live performance against what it predicted. Published before kickoff, settled from the result, never edited.",
};

export const revalidate = 600;

/** A rate, drawn only when the sample can carry one. */
function Bar({ actual, expected }: { actual: number; expected: number }) {
  const gap = actual - expected;
  const honest = gap >= -0.05;
  return (
    <div className="relative h-2 w-full overflow-hidden rounded-pill bg-line">
      <div
        className={`h-full rounded-pill ${honest ? "bg-emerald" : "bg-[#C2410C]"}`}
        style={{ width: `${Math.min(100, actual * 100)}%` }}
      />
      <div
        className="absolute top-0 h-full w-px bg-ink"
        style={{ left: `${Math.min(100, expected * 100)}%` }}
        title="What we predicted"
      />
    </div>
  );
}

export default async function TrackRecordPage() {
  const records = await getTrackRecord();
  const all = records ?? [];
  const measured = all.filter((record) => record.meaningful);
  const waiting = all.filter((record) => !record.meaningful);

  return (
    <>
      <PageHeader
        label="Evidence"
        title="The record, including the losses."
        lead="Every selection is timestamped before kickoff, settled from the result and never edited afterwards. Rates appear only once a service has enough settled selections to mean something."
      />

      <section className="mx-auto max-w-shell px-6 py-20">
        {all.length === 0 ? (
          <div className="rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">No record yet</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              Performance data is unavailable right now.
            </p>
          </div>
        ) : null}

        {measured.length > 0 ? (
          <>
            <h2 className="text-title font-semibold text-ink">
              Measured services
            </h2>
            <p className="mt-3 max-w-prose text-ink-muted">
              The bar is what happened. The line is what we said would happen.
              The gap between them is the number that matters — a service that
              knows itself sits near zero.
            </p>

            <div className="mt-10 space-y-6">
              {measured.map((record) => {
                const actual = record.actual_rate ?? 0;
                const expected = record.expected_rate ?? 0;
                const gap = record.gap ?? 0;
                return (
                  <div
                    key={record.key}
                    className="rounded-lg border border-line bg-white p-6"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-3">
                      <h3 className="text-[15px] font-semibold tracking-[-0.02em] text-ink">
                        {record.label}
                      </h3>
                      <div className="tabular flex items-baseline gap-4 font-mono text-xs">
                        <span className="text-ink-faint">
                          {record.won}/{record.won + record.lost}
                        </span>
                        <span className="text-base text-ink">
                          {(actual * 100).toFixed(1)}%
                        </span>
                        <span className="text-ink-faint">
                          said {(expected * 100).toFixed(1)}%
                        </span>
                        <span
                          className={
                            gap >= -0.05 ? "text-emerald-deep" : "text-[#C2410C]"
                          }
                        >
                          {gap >= 0 ? "+" : ""}
                          {(gap * 100).toFixed(1)}%
                        </span>
                      </div>
                    </div>
                    <div className="mt-4">
                      <Bar actual={actual} expected={expected} />
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        ) : null}

        {waiting.length > 0 ? (
          <div className="mt-16">
            <h2 className="text-title font-semibold text-ink">
              Not yet enough evidence
            </h2>
            <p className="mt-3 max-w-prose text-ink-muted">
              These services are publishing but have too few settled selections
              to quote a rate. A percentage from four results is noise dressed
              as evidence, so we do not print one.
            </p>

            <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {waiting.map((record) => (
                <div
                  key={record.key}
                  className="rounded-md border border-line bg-warm px-5 py-4"
                >
                  <p className="text-sm font-medium text-ink">{record.label}</p>
                  <p className="tabular mt-1 font-mono text-[11px] text-ink-faint">
                    {record.won + record.lost} settled · {record.pending}{" "}
                    pending
                  </p>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      <section className="bg-night-deep py-20 text-white">
        <div className="mx-auto max-w-shell px-6">
          <h2 className="max-w-3xl text-title font-semibold">
            Winning at the rate you predicted is calibration, not profit.
          </h2>
          <p className="mt-6 max-w-prose text-white/70">
            A service landing 89% of the time sounds extraordinary until you
            notice it is priced around 1.10. Our models are well calibrated and
            have not been shown to beat closing prices — we have tested that
            directly, more than once.
          </p>
          <p className="mt-4 max-w-prose text-sm text-white/45">
            We publish the finding rather than obscure it. A record you can
            check is worth more than a claim you cannot.
          </p>
        </div>
      </section>
    </>
  );
}
