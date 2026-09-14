import Image from "next/image";

/**
 * The QUANTSPORT mark.
 *
 * The real logo, not a drawn approximation. Rendered through next/image so it
 * is served in a modern format at the exact size requested rather than shipping
 * a 512px asset to a 30px slot.
 *
 * The calculating state wraps it in a rotating probability arc rather than
 * spinning the mark itself — a tumbling logo reads as a broken page, while a
 * ring around a still mark reads as the engine working.
 */
export function Mark({
  size = 32,
  calculating = false,
}: {
  size?: number;
  calculating?: boolean;
}) {
  const logo = (
    <Image
      src="/icon-192.png"
      alt="QUANTSPORT"
      width={size}
      height={size}
      priority={size >= 30}
      className="select-none"
    />
  );

  if (!calculating) return logo;

  return (
    <span
      className="relative inline-grid place-items-center"
      style={{ width: size * 1.5, height: size * 1.5 }}
    >
      <svg
        viewBox="0 0 48 48"
        className="absolute inset-0 h-full w-full animate-spin"
        style={{ animationDuration: "1.4s" }}
        aria-hidden
      >
        <circle
          cx="24"
          cy="24"
          r="21"
          fill="none"
          stroke="#05B85C"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray="30 102"
        />
      </svg>
      {logo}
    </span>
  );
}
