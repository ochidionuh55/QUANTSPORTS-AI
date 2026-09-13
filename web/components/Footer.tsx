import Link from "next/link";

const COLUMNS = [
  {
    title: "Product",
    links: [
      { href: "/today", label: "Today" },
      { href: "/markets", label: "Markets" },
      { href: "/teams", label: "Teams" },
      { href: "/track-record", label: "Track Record" },
    ],
  },
  {
    title: "Company",
    links: [
      { href: "/methodology", label: "Methodology" },
      { href: "/data-sources", label: "Data Sources" },
      { href: "/pricing", label: "Pricing" },
      { href: "/contact", label: "Contact" },
    ],
  },
  {
    title: "Legal",
    links: [
      { href: "/privacy", label: "Privacy" },
      { href: "/terms", label: "Terms" },
      { href: "/responsible-use", label: "Responsible Use" },
    ],
  },
];

/** The site footer, carrying the standing disclosure. */
export function Footer() {
  return (
    <footer className="mt-24 border-t border-line bg-surface">
      <div className="mx-auto max-w-6xl px-5 py-14">
        <div className="grid gap-10 md:grid-cols-4">
          <div>
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden
                className="grid h-8 w-8 place-items-center rounded-xl bg-gradient-to-br from-emerald-deep via-emerald to-emerald-bright text-sm font-bold text-white"
              >
                Q
              </span>
              <span className="text-[15px] font-semibold tracking-tight">
                QUANTSPORT <span className="text-emerald">AI</span>
              </span>
            </div>
            <p className="mt-3 text-sm text-muted">
              Football Intelligence, Quantified.
            </p>
            <p className="mt-1 text-sm italic text-emerald">Ask the Data.</p>
          </div>

          {COLUMNS.map((column) => (
            <div key={column.title}>
              <h3 className="text-xs font-semibold uppercase tracking-wider text-ink">
                {column.title}
              </h3>
              <ul className="mt-4 space-y-2.5">
                {column.links.map((link) => (
                  <li key={link.href}>
                    <Link
                      href={link.href}
                      className="text-sm text-muted transition-colors hover:text-ink"
                    >
                      {link.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div className="mt-12 border-t border-line pt-6">
          <p className="text-xs leading-relaxed text-muted">
            QUANTSPORT AI provides statistical analysis of football data. High
            probability does not mean certainty, and historical performance does
            not guarantee future results. Our models are calibrated but have not
            been shown to beat bookmaker prices; we publish that finding rather
            than obscure it. Analytical information only. 18+.
          </p>
        </div>
      </div>
    </footer>
  );
}
