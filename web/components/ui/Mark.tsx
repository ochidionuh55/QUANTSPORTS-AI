/**
 * The Q mark.
 *
 * Drawn rather than imported so it can carry state: the ring becomes a
 * probability arc while the engine is working, which is the logo doing a job
 * rather than sitting in a corner.
 */
export function Mark({
  size = 32,
  calculating = false,
}: {
  size?: number;
  calculating?: boolean;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      role="img"
      aria-label="QUANTSPORT"
    >
      <defs>
        <linearGradient id="q-mark" x1="6" y1="6" x2="42" y2="42">
          <stop offset="0%" stopColor="#006B45" />
          <stop offset="55%" stopColor="#05B85C" />
          <stop offset="100%" stopColor="#A9FF3F" />
        </linearGradient>
      </defs>

      <circle
        cx="24"
        cy="24"
        r="17"
        stroke="url(#q-mark)"
        strokeWidth="4.5"
        strokeLinecap="round"
        strokeDasharray={calculating ? "34 74" : undefined}
        className={calculating ? "origin-center animate-spin" : undefined}
        style={calculating ? { animationDuration: "1.4s" } : undefined}
      />
      {/* The tail: a rising curve, not a serif. Growth, measured. */}
      <path
        d="M27 27 L41 41"
        stroke="url(#q-mark)"
        strokeWidth="4.5"
        strokeLinecap="round"
      />
      <circle cx="24" cy="24" r="5.5" fill="url(#q-mark)" opacity="0.9" />
    </svg>
  );
}
