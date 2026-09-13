/**
 * A goal-probability distribution, drawn from an actual Poisson mass function.
 *
 * The brief asked for the mathematics to be visible rather than decorative, so
 * this is the real distribution behind a fixture's goals markets — not a
 * generic curve chosen because it looks scientific.
 */
function poisson(k: number, lambda: number): number {
  let factorial = 1;
  for (let i = 2; i <= k; i += 1) factorial *= i;
  return (Math.exp(-lambda) * Math.pow(lambda, k)) / factorial;
}

export function DistributionCurve({ lambda = 2.7 }: { lambda?: number }) {
  const bars = Array.from({ length: 8 }, (_, goals) => ({
    goals,
    probability: poisson(goals, lambda),
  }));
  const peak = Math.max(...bars.map((bar) => bar.probability));

  return (
    <figure className="rounded-card border border-line bg-white p-6 shadow-card">
      <figcaption className="mb-5">
        <div className="text-sm font-medium text-ink">
          Total goals distribution
        </div>
        <div className="mt-0.5 text-xs text-muted">
          Every goals market is a sum over this curve, so no two can contradict
          each other.
        </div>
      </figcaption>

      <div className="flex h-32 items-end gap-2" aria-hidden>
        {bars.map((bar) => (
          <div key={bar.goals} className="flex flex-1 flex-col items-center gap-2">
            <div
              className="w-full rounded-t-md bg-gradient-to-t from-emerald-deep to-emerald-bright"
              style={{ height: `${(bar.probability / peak) * 100}%` }}
            />
            <span className="tabular text-[10px] text-muted">{bar.goals}</span>
          </div>
        ))}
      </div>

      <p className="mt-4 text-xs text-muted">
        <span className="tabular">λ = {lambda.toFixed(2)}</span> expected goals ·
        Poisson mass function
      </p>
    </figure>
  );
}
