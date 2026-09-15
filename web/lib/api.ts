/**
 * The QUANTSPORT intelligence layer.
 *
 * Every figure the website shows comes from here. Nothing is computed in the
 * frontend — not a probability, not a rate, not a count — so the site and the
 * Telegram bot can never disagree about what was published.
 */

const API = process.env.QUANTSPORT_API ?? "http://localhost:8000";

export type PlatformSummary = {
  matches: number;
  competitions: number;
  teams: number;
  services: number;
  fixtures_today: number;
  fixtures_modelled_today: number;
  selections_today: number;
  updated_at: string;
};

export type ServiceStatus = {
  key: string;
  label: string;
  market: string;
  outcome: string;
  status: "validated" | "observation" | "withheld";
  published: boolean;
  selections_today: number;
};

export type SelectionCard = {
  id: number;
  service_key: string;
  service_label: string;
  home_name: string;
  away_name: string;
  competition: string | null;
  kickoff: string;
  market: string;
  outcome: string;
  probability: number;
  coverage: string;
  status: string;
  home_goals: number | null;
  away_goals: number | null;
  published_at: string;
  model_version: string;
  rationale: string;
  /** Present only on API versions that publish the ranking evidence.
   *  Optional deliberately: the website may be newer than the deployed API,
   *  and a type that promises a field the server does not send turns a rolling
   *  deploy into a runtime crash. */
  factors?: Record<string, number> | null;
  sample_size?: number | null;
  components_used?: string[] | null;
};

export type FixtureCard = {
  fixture_id: string;
  home_name: string;
  away_name: string;
  competition: string | null;
  kickoff: string;
  coverage: string;
  strongest_market: string | null;
  strongest_probability: number | null;
  services?: string[] | null;
};

/**
 * Fetch from the intelligence layer.
 *
 * Failures return null rather than throwing. A page that cannot reach the API
 * should say the data is unavailable, not collapse into an error screen — the
 * rest of the page is still worth reading.
 */
const TIMEOUT_MS = 4000;

async function get<T>(path: string, revalidate = 300): Promise<T | null> {
  try {
    const response = await fetch(`${API}${path}`, {
      next: { revalidate },
      headers: { accept: "application/json" },
      // A page renders only once its data resolves, so an unreachable API
      // would otherwise leave a visitor staring at a blank screen for as long
      // as the network takes to give up. Four seconds, then render without it.
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export const getSummary = () => get<PlatformSummary>("/api/v1/summary", 900);
export const getServices = () => get<ServiceStatus[]>("/api/v1/services", 300);
export const getToday = () => get<FixtureCard[]>("/api/v1/today", 300);
export const getBestOfToday = () =>
  get<SelectionCard[]>("/api/v1/best-of-today", 300);

export type ServiceRecord = {
  key: string;
  label: string;
  status: "validated" | "observation";
  total: number;
  won: number;
  lost: number;
  pending: number;
  actual_rate: number | null;
  expected_rate: number | null;
  gap: number | null;
  meaningful: boolean;
};

export const getTrackRecord = () =>
  get<ServiceRecord[]>("/api/v1/track-record", 600);

export type MarketOption = {
  key: string;
  label: string;
  market: string;
  outcome: string;
  available: number;
};

export const getMarkets = () => get<MarketOption[]>("/api/v1/markets", 300);

export type Split = {
  played: number;
  won: number;
  drawn: number;
  lost: number;
  scored: number;
  conceded: number;
  points_per_game: number | null;
  goals_per_game: number | null;
  over_1_5: number | null;
  over_2_5: number | null;
  over_3_5: number | null;
  both_scored: number | null;
  clean_sheets: number | null;
  meaningful: boolean;
};

export type TeamCard = {
  team_id: number;
  name: string;
  country: string | null;
  overall: Split;
  home: Split;
  away: Split;
  form?: string | null;
  competitions?: string[] | null;
  first_match: string | null;
  last_match: string | null;
};

export type TeamMatch = {
  team_id: number;
  name: string;
  country: string | null;
};

export const getTeams = (q = "") =>
  get<TeamMatch[]>(`/api/v1/teams?q=${encodeURIComponent(q)}`, 300);

export const getTeam = (id: number) => get<TeamCard>(`/api/v1/teams/${id}`, 600);

export type CompetitionCard = {
  code: string;
  name: string;
  country: string;
  matches: number;
  goals_per_game: number | null;
  home_rate: number | null;
  draw_rate: number | null;
  away_rate: number | null;
  over_2_5: number | null;
  both_scored: number | null;
  fixtures_today: number;
  meaningful: boolean;
};

export const getCompetitions = () =>
  get<CompetitionCard[]>("/api/v1/competitions", 3600);

export type DayServiceTally = {
  key: string;
  label: string;
  won: number;
  lost: number;
  void: number;
  pending: number;
};

export type DayRecord = {
  day: string;
  fixtures_available: number;
  fixtures_modelled: number;
  services_qualified: number;
  total: number;
  won: number;
  lost: number;
  void: number;
  pending: number;
  tallies?: DayServiceTally[] | null;
  selections?: SelectionCard[] | null;
};

/** One day's published record. Settled days are permanent, so they cache hard;
 *  today is still moving, so it does not. */
export const getDayRecord = (day: string) =>
  get<DayRecord>(`/api/v1/record/${day}`, 600);

export const getHistoryDays = () => get<string[]>("/api/v1/history", 600);

export type DivergenceCard = {
  fixture_id: string;
  home_name: string;
  away_name: string;
  competition: string | null;
  kickoff: string;
  outcome: string;
  model_probability: number;
  market_probability: number;
  model_odds: number;
  market_odds: number;
  gap: number;
  coverage: string;
  sample: number;
};

export const getDivergence = () =>
  get<DivergenceCard[]>("/api/v1/divergence", 300);

export type CommunityStats = {
  members: number;
  active_this_week: number;
  selections_published: number;
  days_on_record: number;
  channel_url: string;
};

export const getCommunity = () =>
  get<CommunityStats>("/api/v1/community", 600);

/** The community channel. Falls back to the known URL if the API is
 *  unreachable, so the join link never breaks on a page that still renders. */
export const CHANNEL_URL = "https://t.me/PITCHIQ2";
export const BOT_URL = "https://t.me/quantpredictzbot";

/** Search today's card. Filters are applied server-side by the query service
 *  the Telegram bot also uses, so both interfaces answer identically. */
export async function searchFixtures(params: {
  market?: string;
  minProbability?: number;
  coverage?: string;
}): Promise<FixtureCard[] | null> {
  const query = new URLSearchParams();
  if (params.market) query.set("market", params.market);
  if (params.minProbability !== undefined)
    query.set("min_probability", String(params.minProbability));
  if (params.coverage) query.set("coverage", params.coverage);
  return get<FixtureCard[]>(`/api/v1/search?${query.toString()}`, 300);
}

/** Format a probability the way the product speaks about one. */
export function asPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

/** Coverage grades, shared with the bot so the two never diverge. */
export const COVERAGE: Record<string, { badge: string; label: string }> = {
  fully_modelled: { badge: "🟢", label: "Full model" },
  partially_modelled: { badge: "🟡", label: "Partial" },
  data_only: { badge: "🔵", label: "Market view" },
  unsupported: { badge: "⚪", label: "Insufficient data" },
};
