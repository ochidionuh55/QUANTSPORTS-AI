import type { Metadata } from "next";
import Link from "next/link";
import { PageHeader } from "@/components/product/PageHeader";
import { getSummary, getTeams } from "@/lib/api";
import { formatCount } from "@/lib/format";

export const metadata: Metadata = {
  title: "Team Intelligence",
  description:
    "A club's full counted record — results, goals, home and away splits and form.",
};

export const revalidate = 300;

export default async function TeamsPage() {
  const [teams, summary] = await Promise.all([getTeams(), getSummary()]);
  const clubs = teams ?? [];

  // Corpus match count from the live summary, durable fallback if unreachable.
  const lead = summary
    ? `Counted from ${formatCount(summary.matches)} matches on record. No forecast involved — just what happened, measured.`
    : "Counted from every match on record. No forecast involved — just what happened, measured.";

  return (
    <>
      <PageHeader
        label="Team Intelligence"
        title="What a club has actually done."
        lead={lead}
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        <p className="mono-label">Playing today</p>
        <h2 className="mt-4 text-title font-semibold text-ink">
          Open a club&rsquo;s record
        </h2>

        {clubs.length === 0 ? (
          <div className="mt-10 rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">No clubs to list</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              No fixtures are loaded at the moment, so there are no clubs
              playing today to show.
            </p>
          </div>
        ) : (
          <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {clubs.map((club) => (
              <Link
                key={club.team_id}
                href={`/teams/${club.team_id}`}
                className="group flex items-center justify-between gap-4 rounded-md border border-line bg-white px-5 py-4 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:border-line-strong hover:shadow-float"
              >
                <div className="min-w-0">
                  <p className="truncate text-[15px] font-medium text-ink">
                    {club.name}
                  </p>
                  {club.country ? (
                    <p className="mt-0.5 truncate text-[12px] text-ink-faint">
                      {club.country}
                    </p>
                  ) : null}
                </div>
                <span
                  aria-hidden
                  className="text-ink-faint transition-transform duration-base ease-quant group-hover:translate-x-1"
                >
                  →
                </span>
              </Link>
            ))}
          </div>
        )}

        <p className="mt-10 text-sm leading-relaxed text-ink-muted">
          Every club in the register is available in the Telegram bot with{" "}
          <span className="font-mono text-[13px] text-ink">/team Arsenal</span>.
          Web search is next.
        </p>
      </section>
    </>
  );
}
