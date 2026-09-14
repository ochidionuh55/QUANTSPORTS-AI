import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { getCompetitions } from "@/lib/api";
import { formatCount } from "@/lib/format";

export const metadata: Metadata = {
  title: "Competitions",
  description:
    "How each of the competitions on record actually behaves — goals, home advantage and draw frequency, measured.",
};

export const revalidate = 3600;

/** A bar showing one rate against the others in a result split. */
function ResultSplit({
  home,
  draw,
  away,
}: {
  home: number | null;
  draw: number | null;
  away: number | null;
}) {
  if (home === null || draw === null || away === null) {
    return <p className="text-[13px] text-ink-faint">Sample too small</p>;
  }

  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-pill">
        <div
          className="bg-emerald"
          style={{ width: `${home * 100}%` }}
          title={`Home ${Math.round(home * 100)}%`}
        />
        <div
          className="bg-line-strong"
          style={{ width: `${draw * 100}%` }}
          title={`Draw ${Math.round(draw * 100)}%`}
        />
        <div
          className="bg-ink/25"
          style={{ width: `${away * 100}%` }}
          title={`Away ${Math.round(away * 100)}%`}
        />
      </div>
      <div className="tabular mt-2 flex justify-between font-mono text-[11px] text-ink-faint">
        <span>H {Math.round(home * 100)}%</span>
        <span>D {Math.round(draw * 100)}%</span>
        <span>A {Math.round(away * 100)}%</span>
      </div>
    </div>
  );
}

export default async function CompetitionsPage() {
  const competitions = await getCompetitions();
  const leagues = competitions ?? [];
  const active = leagues.filter((league) => league.fixtures_today > 0);

  return (
    <>
      <PageHeader
        label="Competitions"
        title="Every league has a character."
        lead="Goals per game, home advantage and draw frequency, measured per competition rather than assumed. Leagues differ more than most models allow for."
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        {leagues.length === 0 ? (
          <div className="rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">No competitions to show</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              Competition data is unavailable right now.
            </p>
          </div>
        ) : (
          <>
            <div className="flex items-baseline justify-between gap-4">
              <div>
                <p className="mono-label">On record</p>
                <h2 className="mt-4 text-title font-semibold text-ink">
                  {leagues.length} competitions
                </h2>
              </div>
              {active.length > 0 ? (
                <span className="tabular font-mono text-[11px] text-ink-faint">
                  {active.length} with fixtures today
                </span>
              ) : null}
            </div>

            <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {leagues.map((league) => (
                <article
                  key={league.code}
                  className="rounded-lg border border-line bg-white p-6 transition-all duration-base ease-quant hover:border-line-strong hover:shadow-float"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-[16px] font-semibold tracking-[-0.02em] text-ink">
                        {league.name}
                      </h3>
                      <p className="mt-0.5 text-[12px] text-ink-faint">
                        {league.country}
                      </p>
                    </div>
                    {league.fixtures_today > 0 ? (
                      <span className="tabular shrink-0 rounded-pill bg-surface-mint px-2.5 py-1 font-mono text-[11px] text-emerald-deep">
                        {league.fixtures_today} today
                      </span>
                    ) : null}
                  </div>

                  <div className="mt-6">
                    <ResultSplit
                      home={league.home_rate}
                      draw={league.draw_rate}
                      away={league.away_rate}
                    />
                  </div>

                  <dl className="mt-6 space-y-0">
                    {[
                      [
                        "Goals per game",
                        league.goals_per_game === null
                          ? "—"
                          : league.goals_per_game.toFixed(2),
                      ],
                      [
                        "Over 2.5",
                        league.over_2_5 === null
                          ? "—"
                          : `${Math.round(league.over_2_5 * 100)}%`,
                      ],
                      [
                        "Both scored",
                        league.both_scored === null
                          ? "—"
                          : `${Math.round(league.both_scored * 100)}%`,
                      ],
                      ["Matches", formatCount(league.matches)],
                    ].map(([name, value]) => (
                      <div
                        key={name}
                        className="flex items-baseline justify-between gap-3 border-b border-line/70 py-2 last:border-0"
                      >
                        <dt className="text-[12px] text-ink-muted">{name}</dt>
                        <dd className="tabular font-mono text-[12px] text-ink">
                          {value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </article>
              ))}
            </div>

            <p className="mt-12 max-w-prose text-sm leading-relaxed text-ink-muted">
              Rates are shown only where the sample supports them. These are
              counted from matches on record within a rolling window — a
              competition&rsquo;s character shifts over time, and a figure
              averaged across fifteen years would describe a league that no
              longer exists.
            </p>
          </>
        )}
      </section>
    </>
  );
}
