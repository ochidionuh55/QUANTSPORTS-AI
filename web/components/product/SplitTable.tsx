import type { Split } from "@/lib/api";

/**
 * One slice of a club's record.
 *
 * Rates arrive as null below the minimum sample, and are shown as "—" with the
 * match count beside them. Printing a percentage from four matches would look
 * measured and would not be, which is the failure this product is built to
 * avoid everywhere else.
 */
export function SplitTable({ label, split }: { label: string; split: Split }) {
  const percent = (value: number | null) =>
    value === null ? "—" : `${Math.round(value * 100)}%`;

  const rows: [string, string][] = [
    ["Played", String(split.played)],
    ["W / D / L", `${split.won} / ${split.drawn} / ${split.lost}`],
    ["Goals", `${split.scored} scored · ${split.conceded} conceded`],
    [
      "Points per game",
      split.points_per_game === null ? "—" : split.points_per_game.toFixed(2),
    ],
    [
      "Goals per game",
      split.goals_per_game === null ? "—" : split.goals_per_game.toFixed(2),
    ],
    ["Over 1.5", percent(split.over_1_5)],
    ["Over 2.5", percent(split.over_2_5)],
    ["Over 3.5", percent(split.over_3_5)],
    ["Both scored", percent(split.both_scored)],
    ["Clean sheets", percent(split.clean_sheets)],
  ];

  return (
    <div className="rounded-lg border border-line bg-white p-6">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[15px] font-semibold tracking-[-0.02em] text-ink">
          {label}
        </h3>
        {!split.meaningful ? (
          <span className="mono-label">Small sample</span>
        ) : null}
      </div>

      <dl className="mt-5 space-y-0">
        {rows.map(([name, value]) => (
          <div
            key={name}
            className="flex items-baseline justify-between gap-4 border-b border-line/70 py-2.5 last:border-0"
          >
            <dt className="text-[13px] text-ink-muted">{name}</dt>
            <dd className="tabular font-mono text-[13px] text-ink">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
