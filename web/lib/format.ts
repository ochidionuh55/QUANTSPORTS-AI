/**
 * Deterministic formatting.
 *
 * Every value rendered during server-side rendering must produce byte-identical
 * output when React hydrates it in the browser. Anything that consults the
 * machine's locale, timezone or clock will differ between a server in one
 * region and a reader in another, and React reports that as a hydration
 * mismatch — correctly, because the page the reader received is not the page
 * the browser then built.
 *
 * These helpers are the only formatting the interface uses.
 */

/**
 * Format a number with thin separators, independent of locale.
 *
 * `toLocaleString()` produces "113,029" on one machine and "113 029" or
 * "113.029" on another. Grouping is applied explicitly instead.
 */
export function formatCount(value: number): string {
  return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * Format a kickoff time in UTC.
 *
 * Kickoffs are stored in UTC and the Telegram bot displays them in UTC, so the
 * website does the same. Rendering in the reader's local timezone would be
 * friendlier but would differ from what the server rendered, and would also
 * disagree with the bot about when a match starts.
 */
export function formatKickoff(iso: string): string {
  const date = new Date(iso);
  const hours = date.getUTCHours().toString().padStart(2, "0");
  const minutes = date.getUTCMinutes().toString().padStart(2, "0");
  return `${hours}:${minutes} UTC`;
}

/** Format a date as a stable, locale-independent label. */
export function formatDay(iso: string): string {
  const date = new Date(iso);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  return `${days[date.getUTCDay()]} ${date.getUTCDate()} ${months[date.getUTCMonth()]}`;
}

/**
 * Round a float to a fixed precision for use in a style value.
 *
 * An unrounded float such as `0.3492957746478873` can serialise differently
 * between environments, which is exactly the mismatch React flags. Three
 * decimal places is far finer than any display can resolve and is stable.
 */
export function stableAlpha(value: number): number {
  return Number(value.toFixed(3));
}
