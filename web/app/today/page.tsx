import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { FixtureRow } from "@/components/product/FixtureRow";
import { FeaturedSelection } from "@/components/product/FeaturedSelection";
import {
  getBestOfToday,
  getSummary,
  getToday,
  type SelectionCard,
} from "@/lib/api";

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

  // The strongest selections across every service, so the top of the page
  // answers "what is worth looking at today" before the full card answers
  // "what else is on".
  const featured = [...best]
    .sort((a, b) => b.probability - a.probability)
    .slice(0, 3);

  const byService = best.reduce<Record<string, SelectionGroup>>(
    (groups, item) => {
      const group = (groups[item.service_label] ??= {
        label: item.service_label,
        items: [],
      });
      group.items.push(item);
      return groups;
    },
    {},
  );

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

      {featured.length > 0 ? (
        <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
          <div className="flex items-baseline justify-between gap-4">
            <div>
              <p className="mono-label">Strongest today</p>
              <h2 className="mt-4 text-display font-semibold text-ink">
                Where the model is most confident.
              </h2>
            </div>
          </div>

          <p className="mt-5 max-w-prose leading-relaxed text-ink-muted">
            High probability is not the same as good value — an outcome our
            models put at 94% is priced accordingly. These are simply the
            conclusions the mathematics holds most firmly today.
          </p>

          <div className="mt-10 grid gap-5 lg:grid-cols-3">
            {featured.map((selection, index) => (
              <FeaturedSelection
                key={selection.id}
                selection={selection}
                rank={index + 1}
              />
            ))}
          </div>
        </section>
      ) : null}

      {Object.keys(byService).length > 0 ? (
        <section className="border-y border-line bg-warm">
          <div className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
            <p className="mono-label">Every service</p>
            <h2 className="mt-4 text-title font-semibold text-ink">
              Ranked by market
            </h2>
            <p className="mt-3 max-w-prose leading-relaxed text-ink-muted">
              Each service ranks today&rsquo;s fixtures for one market,
              strongest first. Where nothing clears the threshold, the service
              publishes nothing.
            </p>

            <div className="mt-10 grid gap-x-12 gap-y-10 lg:grid-cols-2">
              {Object.values(byService).map((group) => (
                <div key={group.label}>
                  <div className="flex items-baseline justify-between gap-4 border-b border-line pb-3">
                    <h3 className="text-[15px] font-semibold tracking-[-0.02em] text-ink">
                      {group.label}
                    </h3>
                    <span className="tabular font-mono text-[11px] text-ink-faint">
                      {group.items.length}
                    </span>
                  </div>

                  <ol className="mt-4">
                    {group.items.slice(0, 4).map((item, index) => (
                      <li
                        key={item.id}
                        className="flex items-center gap-4 border-b border-line/70 py-3 last:border-0"
                      >
                        <span className="tabular w-4 shrink-0 font-mono text-[11px] text-ink-faint">
                          {index + 1}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[14px] font-medium text-ink">
                            {item.home_name}
                            <span className="mx-1.5 font-normal text-ink-faint">
                              v
                            </span>
                            {item.away_name}
                          </p>
                          <p className="mt-0.5 truncate text-[11px] text-ink-faint">
                            {item.outcome}
                          </p>
                        </div>
                        <span className="tabular shrink-0 font-mono text-base font-medium text-emerald-deep">
                          {Math.round(item.probability * 100)}%
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        <p className="mono-label">Full card</p>
        <h2 className="mt-4 text-title font-semibold text-ink">
          Every fixture, graded
        </h2>
        <p className="mt-3 max-w-prose leading-relaxed text-ink-muted">
          Including the ones no service selected. A fixture we cannot say much
          about is still worth showing — with the reason visible.
        </p>

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
      </section>
    </>
  );
}

type SelectionGroup = {
  label: string;
  items: SelectionCard[];
};
