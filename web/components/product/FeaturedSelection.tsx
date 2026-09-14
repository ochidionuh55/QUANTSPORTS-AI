import { COVERAGE, asPercent, type SelectionCard } from "@/lib/api";
import { formatKickoff } from "@/lib/format";

/**
 * The day's strongest selections, given real space.
 *
 * A flat list makes every fixture compete at the same visual weight, which is
 * exactly wrong for a product whose central claim is that some fixtures are
 * better evidenced than others. The hierarchy on the page should match the
 * hierarchy in the data.
 *
 * The probability is set at display size because it is the answer. Everything
 * else on the card exists to tell the reader how much to trust it.
 */
export function FeaturedSelection({
  selection,
  rank,
}: {
  selection: SelectionCard;
  rank: number;
}) {
  const coverage = COVERAGE[selection.coverage] ?? COVERAGE.unsupported;

  return (
    <article className="group relative overflow-hidden rounded-lg border border-line bg-white p-7 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:shadow-float sm:p-9">
      <div
        aria-hidden
        className="pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full bg-gradient-to-br from-emerald-mint/25 to-transparent blur-2xl transition-opacity duration-slow group-hover:opacity-70"
      />

      <div className="relative">
        <div className="flex items-center justify-between gap-4">
          <span className="mono-label">{selection.service_label}</span>
          <span className="tabular font-mono text-[11px] text-ink-faint">
            {String(rank).padStart(2, "0")}
          </span>
        </div>

        <h3 className="mt-5 text-title font-semibold leading-[1.1] text-ink">
          {selection.home_name}
          <span className="mx-2 font-normal text-ink-faint">v</span>
          {selection.away_name}
        </h3>

        <p className="mt-2 text-[13px] text-ink-muted">
          <span className="tabular font-mono">
            {formatKickoff(selection.kickoff).replace(" UTC", "")}
          </span>
          <span className="mx-2 text-line-strong">•</span>
          {selection.competition ?? "Unknown league"}
        </p>

        <div className="mt-7 flex items-end justify-between gap-6 border-t border-line pt-6">
          <div>
            <p className="mono-label">Model says</p>
            <p className="mt-1.5 text-[15px] font-medium text-ink">
              {selection.outcome}
            </p>
          </div>
          <span className="tabular font-mono text-[2.75rem] font-medium leading-none tracking-[-0.03em] text-emerald-deep">
            {asPercent(selection.probability)}
          </span>
        </div>

        <p className="mt-5 flex items-center gap-2 text-[11px] text-ink-faint">
          <span title={coverage.label}>{coverage.badge}</span>
          {coverage.label}
          <span className="text-line-strong">•</span>
          Published before kickoff
        </p>
      </div>
    </article>
  );
}
