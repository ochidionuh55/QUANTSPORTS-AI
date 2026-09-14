import { COVERAGE, asPercent, type FixtureCard as Fixture } from "@/lib/api";
import { formatKickoff } from "@/lib/format";

/**
 * One fixture, as a full-width editorial row.
 *
 * The brief asks for breathable rows rather than a wall of small cards, and
 * it is right: a card grid forces every fixture to the same visual weight,
 * which is exactly wrong for a product whose point is that some fixtures are
 * better evidenced than others.
 *
 * Coverage sits in the row rather than a tooltip. A probability from a fully
 * modelled fixture and one relayed from a bookmaker deserve different weight,
 * and a reader can only apply that judgement if they can see which is which.
 */
export function FixtureRow({ fixture }: { fixture: Fixture }) {
  const coverage = COVERAGE[fixture.coverage] ?? COVERAGE.unsupported;
  const services = fixture.services ?? [];
  const selected = services.length > 0;

  return (
    <article
      className={[
        "group grid items-center gap-x-6 gap-y-4 border-b border-line py-6",
        "transition-colors duration-base ease-quant hover:bg-surface",
        "sm:grid-cols-[7rem_1fr_auto] sm:px-4",
      ].join(" ")}
    >
      <div className="flex items-center gap-3 sm:block">
        <span className="tabular font-mono text-[13px] text-ink">
          {formatKickoff(fixture.kickoff).replace(" UTC", "")}
        </span>
        <span className="mono-label sm:mt-1.5 sm:block">
          {coverage.badge} {coverage.label}
        </span>
      </div>

      <div className="min-w-0">
        <h3 className="text-[19px] font-semibold leading-snug tracking-[-0.025em] text-ink sm:text-[22px]">
          {fixture.home_name}
          <span className="mx-2 font-normal text-ink-faint">v</span>
          {fixture.away_name}
        </h3>
        <p className="mt-1.5 truncate text-[13px] text-ink-muted">
          {fixture.competition ?? "Unknown league"}
          {selected ? (
            <>
              <span className="mx-2 text-line-strong">•</span>
              {services.length} service
              {services.length > 1 ? "s" : ""} selected this fixture
            </>
          ) : null}
        </p>
      </div>

      <div className="sm:text-right">
        {fixture.strongest_market ? (
          <>
            <div className="tabular font-mono text-2xl font-medium text-emerald-deep sm:text-3xl">
              {asPercent(fixture.strongest_probability)}
            </div>
            <div className="mt-1 text-[13px] text-ink-muted">
              {fixture.strongest_market}
            </div>
          </>
        ) : (
          <p className="max-w-[16rem] text-[13px] leading-relaxed text-ink-faint">
            No service selected this fixture today.
          </p>
        )}
      </div>
    </article>
  );
}
