import { COVERAGE, asPercent, type FixtureCard as Fixture } from "@/lib/api";
import { formatKickoff } from "@/lib/format";

/**
 * One fixture on the card.
 *
 * The coverage grade sits beside the name rather than hidden in a tooltip,
 * because a probability from a fully modelled fixture and one relayed from a
 * bookmaker deserve different weight, and a reader can only apply that
 * judgement if they can see which they are looking at.
 */
export function FixtureCard({ fixture }: { fixture: Fixture }) {
  const coverage = COVERAGE[fixture.coverage] ?? COVERAGE.unsupported;
  const services = fixture.services ?? [];

  return (
    <article className="group rounded-lg border border-line bg-white p-6 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:border-line-strong hover:shadow-float">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="mono-label truncate">
            {fixture.competition ?? "Unknown league"}
          </p>
          <h3 className="mt-3 text-[17px] font-semibold leading-snug tracking-[-0.02em] text-ink">
            {fixture.home_name}
            <span className="mx-1.5 font-normal text-ink-faint">v</span>
            {fixture.away_name}
          </h3>
        </div>
        <span
          className="shrink-0 text-sm"
          title={coverage.label}
          aria-label={coverage.label}
        >
          {coverage.badge}
        </span>
      </div>

      <p className="tabular mt-2 font-mono text-[11px] text-ink-faint">
        {formatKickoff(fixture.kickoff)}
      </p>

      {fixture.strongest_market ? (
        <div className="mt-5 rounded-md bg-surface px-4 py-3.5">
          <div className="mono-label">Model view</div>
          <div className="mt-1.5 flex items-baseline justify-between gap-3">
            <span className="text-sm font-medium text-ink">
              {fixture.strongest_market}
            </span>
            <span className="tabular font-mono text-xl font-medium text-emerald-deep">
              {asPercent(fixture.strongest_probability)}
            </span>
          </div>
          {services.length > 1 ? (
            <p className="mt-2 text-[11px] text-ink-faint">
              +{services.length - 1} more service
              {services.length > 2 ? "s" : ""} selected this fixture
            </p>
          ) : null}
        </div>
      ) : (
        <p className="mt-5 text-xs leading-relaxed text-ink-faint">
          No service selected this fixture today. Its numbers did not clear any
          threshold.
        </p>
      )}
    </article>
  );
}
