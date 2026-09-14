import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { PageHeader } from "@/components/product/PageHeader";
import { SplitTable } from "@/components/product/SplitTable";
import { getTeam } from "@/lib/api";

export const revalidate = 600;

type Props = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const team = await getTeam(Number(id));
  return {
    title: team ? `${team.name} — record` : "Team record",
    description: team
      ? `${team.name}: counted results, goals, home and away splits from matches on record.`
      : undefined,
  };
}

/** A club's counted record. */
export default async function TeamPage({ params }: Props) {
  const { id } = await params;
  const team = await getTeam(Number(id));
  if (!team) notFound();

  const homeAdvantage =
    team.home.points_per_game !== null && team.away.points_per_game !== null
      ? team.home.points_per_game - team.away.points_per_game
      : null;

  return (
    <>
      <PageHeader
        label={team.country ?? "Club record"}
        title={team.name}
        lead={`${team.overall.played} matches on record${
          team.competitions.length
            ? ` across ${team.competitions.slice(0, 3).join(", ")}`
            : ""
        }. Counted, not forecast.`}
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        <div className="grid gap-8 lg:grid-cols-[1fr_1fr_1fr]">
          <SplitTable label="Overall" split={team.overall} />
          <SplitTable label="At home" split={team.home} />
          <SplitTable label="Away" split={team.away} />
        </div>

        <div className="mt-10 grid gap-8 lg:grid-cols-2">
          {team.form ? (
            <div className="rounded-lg border border-line bg-white p-6">
              <p className="mono-label">Recent form</p>
              <div className="mt-5 flex flex-wrap gap-2">
                {team.form.split("").map((result, index) => (
                  <span
                    key={`${result}-${index}`}
                    className={[
                      "grid h-9 w-9 place-items-center rounded-sm font-mono text-[13px] font-medium",
                      result === "W"
                        ? "bg-surface-mint text-emerald-deep"
                        : result === "D"
                          ? "bg-surface text-ink-muted"
                          : "bg-line text-ink-muted",
                    ].join(" ")}
                  >
                    {result}
                  </span>
                ))}
              </div>
              <p className="mt-4 text-xs text-ink-faint">Oldest first.</p>
            </div>
          ) : null}

          <div className="rounded-lg border border-line bg-white p-6">
            <p className="mono-label">Home advantage</p>
            {homeAdvantage === null ? (
              <p className="mt-5 text-sm leading-relaxed text-ink-muted">
                Not enough matches to measure the difference between this
                club&rsquo;s home and away form.
              </p>
            ) : (
              <>
                <p className="tabular mt-4 font-mono text-display font-medium text-emerald-deep">
                  {homeAdvantage >= 0 ? "+" : ""}
                  {homeAdvantage.toFixed(2)}
                </p>
                <p className="mt-3 text-sm leading-relaxed text-ink-muted">
                  Points per game at home compared with away. Measured from this
                  club&rsquo;s own record rather than a league-wide assumption —
                  home advantage varies more between clubs than most models
                  allow for.
                </p>
              </>
            )}
          </div>
        </div>

        <Link
          href="/teams"
          className="mt-12 inline-block text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
        >
          ← All clubs
        </Link>
      </section>
    </>
  );
}
