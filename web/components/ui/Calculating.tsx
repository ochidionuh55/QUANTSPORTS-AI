import { Mark } from "./Mark";

/**
 * The loading state.
 *
 * Section 13 asks for the Q mark as a calculating motif rather than a generic
 * spinner. The ring becomes a probability arc while work is in flight, which
 * makes waiting feel like the engine thinking rather than the page hanging.
 */
export function Calculating({ label = "Calculating" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col items-center gap-4 py-16"
    >
      <Mark size={40} calculating />
      <p className="mono-label">{label}</p>
    </div>
  );
}
