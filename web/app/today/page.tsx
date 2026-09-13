import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { FixtureRow } from "@/components/product/FixtureRow";
import { asPercent, getBestOfToday, getSummary, getToday } from "@/lib/api";

export const metadata: Metadata = {
  title: "Today",
  description:
    "Every fixture we can model today, with the services that selected them.",
};

export const revalidate = 300;

export default async function TodayPage() {
  const [summary, fixtures, selections] = await Promise.all([
    getSummary(),
    getToday(),
    getBestOfToday(),
  ]);

  const card = fixtures ?? [];
  const best = selections ?? [];

  // Grouped by service so a reader scans by the market they care about rather
  // than reading eighteen services in one column.
  const byService = best.reduce<Record<string, typeof best>>((groups, item) => {
    (groups[item.service_label] ??= []).push(item);
    return groups;
  }, {});

  return (
    <>
      <PageHeader
        label="Today's card"
        title="What the models see today."
        lead={
          summary
            ? `${summary.fixtures_modelled_today} of ${summary.fixtures_today} fixtures modelled. ${summary.selections_today} selections published before kickoff.`
            : "Fixture data is unavailable right now."
        }
      />

      {Object.keys(byService).length > 0 ? (
        <section className="mx-auto max-w-shell px-6 py-20">
          <p className="mono-label">Best of today</p>
          <h2 className="mt-5 text-title font-semibold text-ink">
            Ranked by service
          </h2>
          <p className="mt-3 max-w-prose text-ink-muted">
            Each service ranks today&rsquo;s fixtures for one market, strongest
            first. Where nothing clears the threshold, the service publishes
            nothing.
          </p>

          <div className="mt-10 space-y-12">
            {Object.entries(byService).map(([label, items]) => (
              <div key={label}>
                <div className="flex items-baseline justify-between gap-4 border-b border-line pb-3">
                  <h3 className="text-[15px] font-semibold tracking-[-0.02em] text-ink">
                    {label}
                  </h3>
                  <span className="tabular font-mono text-[11px] text-ink-faint">
                    {items.length} qualifying
                  </span>
                </div>

                <ol className="mt-5 space-y-2.5">
                  {items.slice(0, 5).map((item, index) => (
                    <li
                      key={item.id}
                      className="flex items-center gap-4 rounded-md border border-line bg-white px-5 py-4 transition-colors duration-fast hover:border-line-strong"
                    >
                      <span className="tabular w-5 shrink-0 font-mono text-xs text-ink-faint">
                        {index + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-ink">
                          {item.home_name}
                          <span className="mx-1.5 font-normal text-ink-faint">
                            v
                          </span>
                          {item.away_name}
                        </p>
                        <p className="mt-0.5 truncate text-[11px] text-ink-faint">
                          {item.competition ?? "Unknown league"} ·{" "}
                          {item.outcome}
                        </p>
                      </div>
                      <span className="tabular shrink-0 font-mono text-lg font-medium text-emerald-deep">
                        {asPercent(item.probability)}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="border-t border-line bg-warm">
        <div className="mx-auto max-w-shell px-6 py-20">
          <p className="mono-label">Full card</p>
          <h2 className="mt-5 text-title font-semibold text-ink">
            Every fixture, graded
          </h2>

          {card.length === 0 ? (
            <div className="mt-10 rounded-lg border border-line bg-white p-12 text-center">
              <p className="font-medium text-ink">Nothing on the card</p>
              <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
                No fixtures are loaded at the moment. We show what the data
                supports rather than filling the page.
              </p>
            </div>
          ) : (
            <div className="mt-10 border-t border-line">
              {card.map((fixture) => (
                <FixtureRow key={fixture.fixture_id} fixture={fixture} />
              ))}
            </div>
          )}

          <div className="mt-10 flex flex-wrap gap-x-7 gap-y-2 font-mono text-[11px] text-ink-faint">
            <span>🟢 Full model</span>
            <span>🟡 Partial — some components dropped</span>
            <span>🔵 Market view — our models could not run</span>
            <span>⚪ Insufficient data</span>
          </div>
        </div>
      </section>
    </>
  );
}
