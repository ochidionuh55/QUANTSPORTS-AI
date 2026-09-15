import { BOT_URL, CHANNEL_URL, type CommunityStats } from "@/lib/api";
import { formatCount } from "@/lib/format";

/**
 * The community band.
 *
 * Two destinations, deliberately distinguished. The bot is the product; the
 * channel is where the record gets discussed. Presenting them as one link
 * leaves a reader unsure which they have opened, and a person who wanted
 * today's card ends up in a chat instead.
 *
 * Figures are aggregate counts only. A milestone is worth celebrating; the
 * people making it up are not a leaderboard.
 */
export function Community({ stats }: { stats: CommunityStats | null }) {
  return (
    <section className="border-y border-line bg-surface">
      <div className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        <div className="grid gap-12 lg:grid-cols-[1.1fr_0.9fr] lg:gap-20">
          <div>
            <p className="mono-label">The community</p>
            <h2 className="mt-5 text-display font-semibold text-ink">
              {stats
                ? `${formatCount(stats.members)} people use QUANTSPORT.`
                : "Built in the open."}
            </h2>
            <p className="mt-6 max-w-prose leading-relaxed text-ink-muted">
              Every selection is published before kickoff and settled from the
              result. The channel is where that record gets discussed — the
              days it worked and the days it did not.
            </p>

            {stats ? (
              <div className="mt-10 grid grid-cols-2 gap-8 border-t border-line pt-8 sm:grid-cols-3">
                {[
                  {
                    value: formatCount(stats.selections_published),
                    label: "Selections published",
                  },
                  {
                    value: String(stats.days_on_record),
                    label: "Days on record",
                  },
                  {
                    value: formatCount(stats.active_this_week),
                    label: "Active this week",
                  },
                ].map((item) => (
                  <div key={item.label}>
                    <p className="tabular font-mono text-[1.75rem] font-medium leading-none tracking-[-0.03em] text-ink">
                      {item.value}
                    </p>
                    <p className="mono-label mt-2.5">{item.label}</p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          <div className="space-y-4">
            <a
              href={BOT_URL}
              target="_blank"
              rel="noreferrer"
              className="group block rounded-lg border border-line bg-white p-7 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:border-emerald hover:shadow-float"
            >
              <p className="mono-label">The product</p>
              <h3 className="mt-3 text-title font-semibold text-ink">
                Open the bot
              </h3>
              <p className="mt-3 leading-relaxed text-ink-muted">
                Today&rsquo;s selections, the market explorer, team records and
                the full history — all inside Telegram.
              </p>
              <span className="mt-5 inline-flex items-center gap-2 text-sm font-medium text-emerald">
                @quantpredictzbot
                <span
                  aria-hidden
                  className="transition-transform duration-base ease-quant group-hover:translate-x-1"
                >
                  →
                </span>
              </span>
            </a>

            <a
              href={stats?.channel_url ?? CHANNEL_URL}
              target="_blank"
              rel="noreferrer"
              className="group block rounded-lg border border-line bg-white p-7 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:border-emerald hover:shadow-float"
            >
              <p className="mono-label">The channel</p>
              <h3 className="mt-3 text-title font-semibold text-ink">
                Join PitchIQ
              </h3>
              <p className="mt-3 leading-relaxed text-ink-muted">
                Daily selections, results as they settle, and what the models
                got wrong as well as right.
              </p>
              <span className="mt-5 inline-flex items-center gap-2 text-sm font-medium text-emerald">
                t.me/PITCHIQ2
                <span
                  aria-hidden
                  className="transition-transform duration-base ease-quant group-hover:translate-x-1"
                >
                  →
                </span>
              </span>
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
