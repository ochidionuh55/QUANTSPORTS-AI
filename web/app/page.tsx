import Link from "next/link";
import { Stat } from "@/components/Stat";
import { DistributionCurve } from "@/components/DistributionCurve";
import { COVERAGE, asPercent, getServices, getSummary, getToday } from "@/lib/api";

export const revalidate = 300;

export default async function Home() {
  const [summary, fixtures, services] = await Promise.all([
    getSummary(),
    getToday(),
    getServices(),
  ]);

  const upcoming = (fixtures ?? []).slice(0, 6);
  const published = (services ?? []).filter((service) => service.published);

  return (
    <>
      {/* Hero */}
      <section className="relative overflow-hidden border-b border-line">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-40 -top-40 h-[32rem] w-[32rem] rounded-full bg-gradient-to-br from-emerald-bright/20 via-emerald/10 to-transparent blur-3xl"
        />
        <div className="mx-auto max-w-6xl px-5 py-20 sm:py-28">
          <div className="grid items-center gap-14 lg:grid-cols-[1.1fr_0.9fr]">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.18em] text-emerald-deep">
                Data · Models · Markets · Evidence
              </p>
              <h1 className="mt-5 text-4xl font-semibold leading-[1.08] tracking-tight text-ink sm:text-6xl">
                Football Intelligence,
                <br />
                <span className="text-emerald">Quantified.</span>
              </h1>
              <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted">
                Mathematical models, historical evidence and transparent
                probabilities — built to help you explore football beyond
                opinion.
              </p>

              <div className="mt-9 flex flex-wrap gap-3">
                <Link
                  href="/today"
                  className="rounded-pill bg-emerald px-6 py-3 text-sm font-medium text-white shadow-card transition-opacity hover:opacity-90"
                >
                  Explore Today
                </Link>
                <a
                  href="https://t.me/quantpredictzbot"
                  className="rounded-pill border border-line bg-white px-6 py-3 text-sm font-medium text-ink transition-colors hover:border-emerald"
                >
                  Try on Telegram
                </a>
              </div>

              <p className="mt-8 text-sm italic text-emerald-deep">
                Ask the Data.
              </p>
            </div>

            <DistributionCurve lambda={2.68} />
          </div>

          {/* Figures come from the database, never from the design. */}
          <div className="mt-16 grid grid-cols-2 gap-8 border-t border-line pt-10 sm:grid-cols-4">
            <Stat
              value={summary ? summary.matches.toLocaleString() : "—"}
              label="Matches"
            />
            <Stat
              value={summary ? String(summary.competitions) : "—"}
              label="Competitions"
            />
            <Stat
              value={summary ? summary.teams.toLocaleString() : "—"}
              label="Teams"
            />
            <Stat
              value={summary ? String(summary.services) : "—"}
              label="Daily services"
            />
          </div>
        </div>
      </section>

      {/* Today's intelligence */}
      <section className="mx-auto max-w-6xl px-5 py-20">
        <div className="flex items-end justify-between gap-6">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Today&rsquo;s Intelligence
            </h2>
            <p className="mt-2 text-muted">
              {summary
                ? `${summary.fixtures_modelled_today} of ${summary.fixtures_today} fixtures modelled.`
                : "Fixture data is unavailable right now."}
            </p>
          </div>
          <Link
            href="/today"
            className="hidden shrink-0 text-sm font-medium text-emerald hover:underline sm:block"
          >
            View all →
          </Link>
        </div>

        {upcoming.length === 0 ? (
          <div className="mt-8 rounded-card border border-line bg-surface p-10 text-center">
            <p className="font-medium text-ink">No fixtures to show yet</p>
            <p className="mt-2 text-sm text-muted">
              Nothing is on the card at the moment. We publish what the data
              supports rather than filling the page.
            </p>
          </div>
        ) : (
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {upcoming.map((fixture) => {
              const coverage =
                COVERAGE[fixture.coverage] ?? COVERAGE.unsupported;
              return (
                <article
                  key={fixture.fixture_id}
                  className="rounded-card border border-line bg-white p-5 shadow-card transition-shadow hover:shadow-lift"
                >
                  <div className="flex items-center justify-between text-xs text-muted">
                    <span>{fixture.competition ?? "Unknown league"}</span>
                    <span title={coverage.label}>{coverage.badge}</span>
                  </div>
                  <h3 className="mt-3 text-[15px] font-semibold leading-snug text-ink">
                    {fixture.home_name}{" "}
                    <span className="font-normal text-muted">v</span>{" "}
                    {fixture.away_name}
                  </h3>
                  <p className="mt-1 text-xs text-muted">
                    {new Date(fixture.kickoff).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </p>

                  {fixture.strongest_market ? (
                    <div className="mt-4 rounded-xl bg-surface-green px-3.5 py-3">
                      <div className="text-xs text-muted">Model view</div>
                      <div className="mt-0.5 flex items-baseline justify-between">
                        <span className="text-sm font-medium text-ink">
                          {fixture.strongest_market}
                        </span>
                        <span className="tabular text-lg font-semibold text-emerald-deep">
                          {asPercent(fixture.strongest_probability)}
                        </span>
                      </div>
                    </div>
                  ) : (
                    <p className="mt-4 text-xs text-muted">
                      No service selected this fixture today.
                    </p>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* Services */}
      <section className="border-y border-line bg-surface">
        <div className="mx-auto max-w-6xl px-5 py-20">
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            Daily services
          </h2>
          <p className="mt-2 max-w-2xl text-muted">
            Each service ranks today&rsquo;s fixtures for one market. Nothing is
            published to fill a slot — if nothing qualifies, the service says
            so.
          </p>

          <div className="mt-8 flex flex-wrap gap-2.5">
            {published.length === 0 ? (
              <p className="text-sm text-muted">
                Service data is unavailable right now.
              </p>
            ) : (
              published.map((service) => (
                <span
                  key={service.key}
                  className="inline-flex items-center gap-2 rounded-pill border border-line bg-white px-4 py-2 text-sm"
                >
                  <span
                    aria-hidden
                    className={
                      service.status === "validated"
                        ? "h-1.5 w-1.5 rounded-full bg-emerald"
                        : "h-1.5 w-1.5 rounded-full bg-lime"
                    }
                  />
                  {service.label.replace(/^[^\s]+\s/, "")}
                  {service.selections_today > 0 ? (
                    <span className="tabular text-xs text-muted">
                      {service.selections_today}
                    </span>
                  ) : null}
                </span>
              ))
            )}
          </div>

          <p className="mt-6 text-xs text-muted">
            Green = validated · Lime = under observation. Services measured as
            overconfident are withheld rather than published.
          </p>
        </div>
      </section>

      {/* Honesty */}
      <section className="mx-auto max-w-6xl px-5 py-20">
        <div className="grid gap-10 lg:grid-cols-2">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              What we can show, and what we can&rsquo;t
            </h2>
            <p className="mt-5 leading-relaxed text-muted">
              Our probabilities are well calibrated: outcomes happen about as
              often as we say they will. We have tested, more than once, whether
              the models beat bookmaker prices. They do not.
            </p>
            <p className="mt-4 leading-relaxed text-muted">
              We publish that finding rather than obscure it, because a track
              record you cannot check is only a claim. Every selection is
              timestamped before kickoff, settled from the result, and never
              edited afterwards — including the ones that lost.
            </p>
            <Link
              href="/methodology"
              className="mt-6 inline-block text-sm font-medium text-emerald hover:underline"
            >
              Read the methodology →
            </Link>
          </div>

          <div className="rounded-card border border-line bg-white p-8 shadow-card">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-ink">
              What this is not
            </h3>
            <ul className="mt-5 space-y-3.5 text-sm text-muted">
              {[
                "A tipping service",
                "A claim to beat bookmakers",
                "Guaranteed outcomes of any kind",
                "A record with the losses removed",
              ].map((item) => (
                <li key={item} className="flex gap-3">
                  <span aria-hidden className="text-muted">
                    ✕
                  </span>
                  {item}
                </li>
              ))}
            </ul>
            <p className="mt-6 border-t border-line pt-5 text-sm leading-relaxed text-ink">
              It is a research tool for people who would rather see the
              mathematics than be told an answer.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
