import Link from "next/link";

const LINKS = [
  { href: "/today", label: "Today" },
  { href: "/markets", label: "Markets" },
  { href: "/teams", label: "Teams" },
  { href: "/track-record", label: "Track Record" },
  { href: "/methodology", label: "Methodology" },
  { href: "/pricing", label: "Pricing" },
];

/** The site header. */
export function Nav() {
  return (
    <header className="sticky top-0 z-50 border-b border-line bg-white/85 backdrop-blur">
      <nav className="mx-auto flex max-w-6xl items-center justify-between px-5 py-4">
        <Link href="/" className="flex items-center gap-2.5">
          <span
            aria-hidden
            className="grid h-8 w-8 place-items-center rounded-xl bg-gradient-to-br from-emerald-deep via-emerald to-emerald-bright text-sm font-bold text-white"
          >
            Q
          </span>
          <span className="text-[15px] font-semibold tracking-tight">
            QUANTSPORT <span className="text-emerald">AI</span>
          </span>
        </Link>

        <ul className="hidden items-center gap-7 md:flex">
          {LINKS.map((link) => (
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

        <div className="flex items-center gap-3">
          <Link
            href="/login"
            className="hidden text-sm text-muted transition-colors hover:text-ink sm:block"
          >
            Sign in
          </Link>
          <Link
            href="/today"
            className="rounded-pill bg-ink px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
          >
            Open QUANTSPORT
          </Link>
        </div>
      </nav>
    </header>
  );
}
