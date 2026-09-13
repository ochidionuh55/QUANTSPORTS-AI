import Link from "next/link";
import type { ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-ink text-white hover:shadow-float hover:-translate-y-0.5 active:translate-y-0",
  secondary:
    "bg-white text-ink border border-line hover:border-emerald hover:-translate-y-0.5 active:translate-y-0",
  ghost: "text-ink-muted hover:text-ink",
};

/**
 * The product's only button.
 *
 * One component means hover weight, radius and timing stay identical
 * everywhere — the kind of consistency that reads as craft rather than as any
 * single visible decision.
 */
export function Button({
  href,
  children,
  variant = "primary",
  external = false,
}: {
  href: string;
  children: ReactNode;
  variant?: Variant;
  external?: boolean;
}) {
  const className = [
    "group inline-flex items-center gap-2 rounded-pill px-6 py-3",
    "text-sm font-medium transition-all duration-base ease-quant",
    "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald",
    VARIANTS[variant],
  ].join(" ");

  const content = (
    <>
      {children}
      <span
        aria-hidden
        className="transition-transform duration-base ease-quant group-hover:translate-x-[3px]"
      >
        →
      </span>
    </>
  );

  if (external) {
    return (
      <a href={href} className={className} target="_blank" rel="noreferrer">
        {content}
      </a>
    );
  }
  return (
    <Link href={href} className={className}>
      {content}
    </Link>
  );
}
