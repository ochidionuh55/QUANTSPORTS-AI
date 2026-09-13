/**
 * One headline figure.
 *
 * Values come from the database rather than the design, so the site cannot
 * advertise a corpus that is no longer there.
 */
export function Stat({
  value,
  label,
}: {
  value: string;
  label: string;
}) {
  return (
    <div>
      <div className="tabular text-2xl font-semibold tracking-tight text-ink sm:text-3xl">
        {value}
      </div>
      <div className="mt-1 text-xs uppercase tracking-wider text-muted">
        {label}
      </div>
    </div>
  );
}
