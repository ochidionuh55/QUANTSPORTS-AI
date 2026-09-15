import Link from "next/link";
import { Mark } from "./ui/Mark";

const COLUMNS = [
  {
    title: "Product",
    links: [
      { href: "/today", label: "Today" },
      { href: "/markets", label: "Market Explorer" },
      { href: "/teams", label: "Team Intelligence" },
      { href: "/track-record", label: "Track Record" },
    ],
  },
  {
    title: "Research",
    links: [
      { href: "/methodology", label: "Methodology" },
      { href: "/data-sources", label: "Data Sources" },
      { href: "/history", label: "History" },
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

/** The footer. Ends on the brand line, set as a poster. */
export function Footer() {
  return (
    <footer className="relative z-0 border-t border-line bg-warm">
      <div className="mx-auto max-w-shell px-6 py-20">
        <div className="grid gap-12 md:grid-cols-[1.4fr_repeat(3,1fr)]">
          <div>
            <Mark size={38} />
            <p className="mt-4 text-sm font-medium text-ink">QUANTSPORT AI</p>
            <p className="mt-1 text-sm text-ink-muted">
              Football Intelligence, Quantified.
            </p>
            <div className="mt-5 flex flex-col gap-2">
              <a
                href="https://t.me/quantpredictzbot"
                target="_blank"
                rel="noreferrer"
                className="text-sm text-ink-muted transition-colors duration-fast hover:text-emerald"
              >
                Open the bot →
              </a>
              <a
                href="https://t.me/PITCHIQ2"
                target="_blank"
                rel="noreferrer"
                className="text-sm text-ink-muted transition-colors duration-fast hover:text-emerald"
              >
                Join PitchIQ channel →
              </a>
            </div>
          </div>

          {COLUMNS.map((column) => (
            <div key={column.title}>
              <h3 className="mono-label">{column.title}</h3>
              <ul className="mt-5 space-y-3">
                {column.links.map((link) => (
                  <li key={link.href}>
                    <Link
                      href={link.href}
                      className="text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
                    >
                      {link.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <p className="mt-16 max-w-prose text-xs leading-relaxed text-ink-faint">
          QUANTSPORT AI provides calibrated football intelligence, not a claim
          of proven bookmaker-beating edge. Our models are measured against
          outcomes and market benchmarks where valid data exists. We publish
          what the evidence supports. High probability does not mean certainty.
          Analytical information only. 18+.
        </p>

        <div
          aria-hidden
          className="mt-12 select-none border-t border-line pt-10 text-[clamp(2.5rem,11vw,9rem)] font-semibold leading-none tracking-[-0.05em] text-emerald/12"
        >
          ASK THE DATA.
        </div>
      </div>
    </footer>
  );
}
