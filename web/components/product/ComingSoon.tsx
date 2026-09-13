import Link from "next/link";
import { PageHeader } from "./PageHeader";
import { Button } from "@/components/ui/Button";

/**
 * A designed holding page.
 *
 * A navigation item that resolves to a 404 tells a reader the product is
 * broken. One that explains what is coming and why tells them it is being
 * built carefully — and costs nothing but honesty about the state of things.
 */
export function ComingSoon({
  label,
  title,
  lead,
  building,
  available,
}: {
  label: string;
  title: string;
  lead: string;
  building: string[];
  available?: { href: string; label: string };
}) {
  return (
    <>
      <PageHeader label={label} title={title} lead={lead} />

      <section className="mx-auto max-w-shell px-6 py-20">
        <div className="grid gap-14 lg:grid-cols-[1fr_1fr]">
          <div>
            <p className="mono-label">In build</p>
            <ul className="mt-6 space-y-4">
              {building.map((item) => (
                <li key={item} className="flex gap-4">
                  <span
                    aria-hidden
                    className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald"
                  />
                  <span className="leading-relaxed text-ink-muted">{item}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded-lg border border-line bg-warm p-8">
            <h2 className="text-[17px] font-semibold tracking-[-0.02em] text-ink">
              Available now on Telegram
            </h2>
            <p className="mt-3 leading-relaxed text-ink-muted">
              This feature already runs in the QUANTSPORT bot. The website
              version is being built to the same standard as the rest of the
              site rather than shipped early.
            </p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Button
                href="https://t.me/quantpredictzbot"
                variant="secondary"
                external
              >
                Open Telegram
              </Button>
              {available ? (
                <Link
                  href={available.href}
                  className="inline-flex items-center px-2 py-3 text-sm text-ink-muted transition-colors duration-fast hover:text-ink"
                >
                  {available.label}
                </Link>
              ) : null}
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
