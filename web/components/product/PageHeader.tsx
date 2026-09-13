/** A consistent page opening, so every screen starts the same way. */
export function PageHeader({
  label,
  title,
  lead,
}: {
  label: string;
  title: string;
  lead?: string;
}) {
  return (
    <header className="light-field grain relative overflow-hidden border-b border-line">
      <div className="mx-auto max-w-shell px-6 pb-16 pt-36">
        <p className="mono-label animate-rise">{label}</p>
        <h1
          className="mt-6 animate-rise text-display font-semibold text-ink"
          style={{ animationDelay: "60ms" }}
        >
          {title}
        </h1>
        {lead ? (
          <p
            className="mt-5 max-w-prose animate-rise text-lead text-ink-muted"
            style={{ animationDelay: "120ms" }}
          >
            {lead}
          </p>
        ) : null}
      </div>
    </header>
  );
}
