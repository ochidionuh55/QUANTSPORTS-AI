"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Wordmark } from "./ui/Wordmark";

const LINKS = [
  { href: "/today", label: "Today" },
  { href: "/markets", label: "Markets" },
  { href: "/teams", label: "Teams" },
  { href: "/history", label: "History" },
  { href: "/track-record", label: "Track Record" },
];

/**
 * Navigation.
 *
 * Transparent over the hero, then a glass capsule once the reader has scrolled
 * past it — marking the move from the statement into the product.
 *
 * On narrow screens the links become a full sheet rather than disappearing.
 * Hiding navigation below a breakpoint leaves a phone user with no way through
 * the site at all, which is the most common way a desktop-first design fails
 * the majority of its traffic.
 */
export function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Navigating should close the sheet, or the reader lands on a new page with
  // the menu still covering it.
  useEffect(() => setOpen(false), [pathname]);

  // A menu that scrolls the page behind it feels broken on touch.
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <header className="pointer-events-none fixed inset-x-0 top-0 z-50 px-3 pt-3 sm:px-4 sm:pt-4">
        <nav
          className={[
            "pointer-events-auto mx-auto flex max-w-shell items-center justify-between rounded-pill",
            "px-4 py-2.5 transition-all duration-slow ease-quant sm:px-5 sm:py-3",
            scrolled || open
              ? "glass lg:max-w-5xl"
              : "border border-transparent bg-white/0",
          ].join(" ")}
        >
          <Wordmark />

          <ul className="hidden items-center gap-8 lg:flex">
            {LINKS.map((link) => {
              const active = pathname === link.href;
              return (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    aria-current={active ? "page" : undefined}
                    className={[
                      "group relative text-sm transition-colors duration-fast",
                      active ? "text-ink" : "text-ink-muted hover:text-ink",
                    ].join(" ")}
                  >
                    {link.label}
                    <span
                      className={[
                        "absolute -bottom-1.5 left-0 h-px bg-emerald",
                        "transition-all duration-base ease-quant",
                        active ? "w-full" : "w-0 group-hover:w-full",
                      ].join(" ")}
                    />
                  </Link>
                </li>
              );
            })}
          </ul>

          <div className="flex items-center gap-2">
            <Link
              href="/today"
              className="hidden rounded-pill bg-ink px-4 py-2 text-sm font-medium text-white transition-all duration-base ease-quant hover:shadow-float sm:inline-flex"
            >
              Open QUANTSPORT
            </Link>

            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              aria-controls="mobile-nav"
              aria-label={open ? "Close menu" : "Open menu"}
              className="grid h-9 w-9 place-items-center rounded-pill text-ink transition-colors duration-fast hover:bg-surface lg:hidden"
            >
              <span className="relative block h-3 w-4">
                <span
                  className={[
                    "absolute left-0 h-px w-full bg-current",
                    "transition-all duration-base ease-quant",
                    open ? "top-1.5 rotate-45" : "top-0",
                  ].join(" ")}
                />
                <span
                  className={[
                    "absolute left-0 h-px w-full bg-current",
                    "transition-all duration-base ease-quant",
                    open ? "top-1.5 -rotate-45" : "top-3",
                  ].join(" ")}
                />
              </span>
            </button>
          </div>
        </nav>
      </header>

      {/* Mobile sheet */}
      <div
        id="mobile-nav"
        aria-hidden={!open}
        className={[
          "fixed inset-0 z-40 lg:hidden",
          "transition-opacity duration-base ease-quant",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        ].join(" ")}
      >
        <button
          type="button"
          tabIndex={-1}
          aria-label="Close menu"
          onClick={() => setOpen(false)}
          className="absolute inset-0 bg-ink/20 backdrop-blur-sm"
        />

        <div
          className={[
            "light-field absolute inset-x-0 top-0 rounded-b-2xl px-6 pb-10 pt-24",
            "shadow-overlay transition-transform duration-slow ease-quant",
            open ? "translate-y-0" : "-translate-y-4",
          ].join(" ")}
        >
          <ul className="space-y-1">
            {LINKS.map((link, index) => (
              <li key={link.href}>
                <Link
                  href={link.href}
                  className="flex items-baseline justify-between border-b border-line py-4 text-2xl font-semibold tracking-[-0.03em] text-ink transition-colors duration-fast hover:text-emerald-deep"
                >
                  {link.label}
                  <span className="tabular font-mono text-[11px] text-ink-faint">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                </Link>
              </li>
            ))}
          </ul>

          <div className="mt-8 flex flex-col gap-3">
            <Link
              href="/today"
              className="rounded-pill bg-ink px-6 py-3.5 text-center text-sm font-medium text-white"
            >
              Open QUANTSPORT
            </Link>
            <a
              href="https://t.me/quantpredictzbot"
              className="rounded-pill border border-line bg-white px-6 py-3.5 text-center text-sm font-medium text-ink"
            >
              Continue on Telegram
            </a>
          </div>

          <p className="mt-8 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-faint">
            Ask the Data.
          </p>
        </div>
      </div>
    </>
  );
}
