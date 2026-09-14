import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { PageHeader } from "@/components/product/PageHeader";
import { COVERAGE, getDayRecord, getHistoryDays } from "@/lib/api";
import { formatDay, formatKickoff } from "@/lib/format";

export const revalidate = 600;

type Props = { params: Promise<{ day: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { day } = await params;
  return {
    title: `${formatDay(`${day}T12:00:00Z`)} — record`,
    description: `What QUANTSPORT published on ${day}, and how it finished.`,
  };
}

const STATUS = {
  won: { mark: "✓", label: "WON", className: "text-emerald-deep" },
  lost: { mark: "✕", label: "LOST", className: "text-[#C2410C]" },
  void: { mark: "—", label: "VOID", className: "text-ink-faint" },
  pending: { mark: "⏳", label: "PENDING", className: "text-ink-faint" },
} as const;

export default async function DayPage({ params }: Props) {
  const { day } = await params;
  const [record, days] = await Promise.all([
    getDayRecord(day),
    getHistoryDays(),
  ]);
  if (!record) notFound();

  const tallies = record.tallies ?? [];
  const selections = record.selections ?? [];
  const settled = record.won + record.lost;

  // Navigation across whatever exists, rather than blind date arithmetic that
  // would offer days we never published on.
  const ordered = [...(days ?? [])].sort();
  const index = ordered.indexOf(day);
  const previous = index > 0 ? ordered[index - 1] : null;
  const next =
    index >= 0 && index < ordered.length - 1 ? ordered[index + 1] : null;

  return (
    <>
      <PageHeader
        label="Daily record"
        title={formatDay(`${day}T12:00:00Z`)}
        lead={
          settled > 0
            ? `${record.won} of ${settled} settled selections won${
                record.pending ? `, ${record.pending} still pending` : ""
              }.`
            : `${record.total} selections published${
                record.pending ? `, ${record.pending} awaiting results` : ""
              }.`
        }
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        {tallies.length > 0 ? (
          <>
            <p className="mono-label">By service</p>
            <div className="mt-6 grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {tallies.map((tally) => {
                const decided = tally.won + tally.lost;
                return (
                  <div
                    key={tally.key}
                    className="flex items-baseline justify-between gap-4 rounded-md border border-line bg-white px-5 py-4"
                  >
                    <span className="truncate text-[14px] font-medium text-ink">
                      {tally.label}
                    </span>
                    <span className="tabular shrink-0 font-mono text-[15px]">
                      {decided > 0 ? (
                        <span
                          className={
                            tally.won >= decided - tally.won
                              ? "text-emerald-deep"
                              : "text-ink"
                          }
                        >
                          {tally.won}/{decided}
                        </span>
                      ) : (
                        <span className="text-ink-faint">
                          {tally.pending} pending
                        </span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          </>
        ) : null}

        <div className="mt-14">
          <p className="mono-label">Every selection</p>
          {selections.length === 0 ? (
            <div className="mt-6 rounded-lg border border-line bg-white p-12 text-center">
              <p className="font-medium text-ink">Nothing published</p>
              <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
                No fixture cleared a service threshold on this date. That is a
                real answer — we never publish a selection to fill a slot.
              </p>
            </div>
          ) : (
            <div className="mt-6 border-t border-line">
              {selections.map((selection) => {
                const status =
                  STATUS[selection.status as keyof typeof STATUS] ??
                  STATUS.pending;
                const coverage =
                  COVERAGE[selection.coverage] ?? COVERAGE.unsupported;
                const score =
                  selection.home_goals !== null &&
                  selection.away_goals !== null
                    ? `${selection.home_goals}–${selection.away_goals}`
                    : null;

                return (
                  <article
                    key={selection.id}
                    className="grid items-center gap-x-5 gap-y-2 border-b border-line py-5 sm:grid-cols-[8rem_1fr_auto] sm:px-4"
                  >
                    <span
                      className={`font-mono text-[12px] font-medium ${status.className}`}
                    >
                      {status.mark} {status.label}
                    </span>

                    <div className="min-w-0">
                      <h3 className="text-[16px] font-semibold leading-snug tracking-[-0.02em] text-ink">
                        {selection.home_name}
                        {score ? (
                          <span className="tabular mx-2.5 font-mono text-ink">
                            {score}
                          </span>
                        ) : (
                          <span className="mx-2 font-normal text-ink-faint">
                            v
                          </span>
                        )}
                        {selection.away_name}
                      </h3>
                      <p className="mt-1 truncate text-[12px] text-ink-muted">
                        {selection.service_label}
                        <span className="mx-2 text-line-strong">•</span>
                        {selection.outcome}
                        <span className="mx-2 text-line-strong">•</span>
                        <span title={coverage.label}>{coverage.badge}</span>
                        <span className="mx-2 text-line-strong">•</span>
                        <span className="tabular font-mono">
                          {formatKickoff(selection.kickoff)}
                        </span>
                      </p>
                    </div>

                    <span className="tabular font-mono text-xl font-medium text-emerald-deep sm:text-right">
                      {Math.round(selection.probability * 100)}%
                    </span>
                  </article>
                );
              })}
            </div>
          )}
        </div>

        <p className="mt-10 max-w-prose text-[13px] leading-relaxed text-ink-muted">
          Every probability above was published before its match kicked off and
          has not been changed since. Only the result was added afterwards.
        </p>

        <div className="mt-10 flex flex-wrap items-center gap-4 border-t border-line pt-8">
          {previous ? (
            <Link
              href={`/history/${previous}`}
              className="text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
            >
              ← {formatDay(`${previous}T12:00:00Z`)}
            </Link>
          ) : null}
          <Link
            href="/history"
            className="text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
          >
            All dates
          </Link>
          {next ? (
            <Link
              href={`/history/${next}`}
              className="text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
            >
              {formatDay(`${next}T12:00:00Z`)} →
            </Link>
          ) : null}
        </div>
      </section>
    </>
  );
}
